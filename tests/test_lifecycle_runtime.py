"""Tests for lifecycle_runtime.py — LifecycleRuntime.

Candidate-1 refactor: ServiceCoordinator 被瘦身成只负责
tick / sync_sleep / stop_all 的薄外壳。suanpan toggle / reload / restart
元组格式化已上提到 app.py；SuanpanRuntime / SystemProxyController /
CaptureController 子模块接口不变，但不再通过 ServiceCoordinator 暴露，
而是由 MagicProxyApp 直接持有。

这些测试只打公开接口（构造器入参 + tick/sync_sleep/stop_all），
不再穿透 ``svc._suanpan._rt._thread`` 或 ``svc._capture_ctrl._enabled``
这种内部属性。
"""
import unittest
from unittest.mock import MagicMock, patch

from services.lifecycle_runtime import LifecycleRuntime, _should_prevent_sleep


def _make_coordinator():
    return LifecycleRuntime(
        config_fn=lambda: {"prevent_sleep": False},
        ssh_monitor=MagicMock(),
        paused_fn=lambda: False,
        on_menu_dirty=lambda: None,
    )


class TestShouldPreventSleep(unittest.TestCase):
    def test_connected_not_paused_with_flag(self):
        self.assertTrue(_should_prevent_sleep("connected", False, True))

    def test_paused_blocks(self):
        self.assertFalse(_should_prevent_sleep("connected", True, True))

    def test_disconnected_blocks(self):
        self.assertFalse(_should_prevent_sleep("stopped", False, True))

    def test_flag_off_blocks(self):
        self.assertFalse(_should_prevent_sleep("connected", False, False))


class TestTick(unittest.TestCase):
    """tick 只看 capture_ctrl.enabled 公开属性；不碰 _enabled 内部字段。"""

    def test_checks_capture_when_enabled(self):
        svc = _make_coordinator()
        with patch.object(type(svc._capture_ctrl), "enabled", True), \
             patch.object(svc._capture, "check") as check, \
             patch.object(svc._sys_proxy, "sync") as sync:
            svc.tick(8080)
        check.assert_called_once_with(8080)
        sync.assert_called_once()

    def test_skips_capture_check_when_disabled(self):
        svc = _make_coordinator()
        with patch.object(type(svc._capture_ctrl), "enabled", False), \
             patch.object(svc._capture, "check") as check, \
             patch.object(svc._sys_proxy, "sync") as sync:
            svc.tick(8080)
        check.assert_not_called()
        sync.assert_called_once()


class TestSyncSleep(unittest.TestCase):
    """sync_sleep 通过 blocker.is_running 公开属性判断是否成功 acquire，
    不再伪造 ``_proc.poll()`` 这种内部状态。
    """

    def test_acquires_when_connected(self):
        svc = _make_coordinator()
        with patch.object(svc._blocker, "acquire"), \
             patch.object(type(svc._blocker), "is_running", True):
            svc.sync_sleep("connected", False, True)
        self.assertTrue(svc._caffeinate_on)

    def test_releases_when_disconnected(self):
        svc = _make_coordinator()
        svc._caffeinate_on = True
        with patch.object(svc._blocker, "release") as mock_release:
            svc.sync_sleep("stopped", False, True)
        mock_release.assert_called_once()
        self.assertFalse(svc._caffeinate_on)


class TestStopAll(unittest.TestCase):
    def test_stops_capture_releases_sleep_and_stops_suanpan(self):
        svc = _make_coordinator()
        svc._caffeinate_on = True
        with patch.object(type(svc._suanpan), "running", True), \
             patch.object(svc._suanpan, "stop") as mock_sp_stop, \
             patch.object(svc._capture, "stop") as mock_cap_stop, \
             patch.object(svc._blocker, "release") as mock_release:
            svc.stop_all()
        mock_sp_stop.assert_called_once()
        mock_cap_stop.assert_called_once_with(blocking=False)
        mock_release.assert_called_once()

    def test_suanpan_not_running_skips_stop(self):
        svc = _make_coordinator()
        with patch.object(type(svc._suanpan), "running", False), \
             patch.object(svc._suanpan, "stop") as mock_sp_stop:
            svc.stop_all()
        mock_sp_stop.assert_not_called()


class TestCaptureStateSingleProjection(unittest.TestCase):
    """「抓包正在运行」单一投影：构造期直连 capture_ctrl，两个消费者内部适配。"""

    def test_tuple_projection_for_sys_proxy(self):
        svc = _make_coordinator()
        with patch.object(type(svc._capture_ctrl), "enabled", True), \
             patch.object(type(svc._capture_ctrl), "status", "running"):
            self.assertEqual(svc._capture_state_tuple(), (True, "running"))

    def test_bool_projection_for_config_server(self):
        svc = _make_coordinator()
        with patch.object(type(svc._capture_ctrl), "enabled", True), \
             patch.object(type(svc._capture_ctrl), "status", "starting"):
            self.assertIs(svc._capture_state_bool(), False)
        with patch.object(type(svc._capture_ctrl), "enabled", True), \
             patch.object(type(svc._capture_ctrl), "status", "running"):
            self.assertIs(svc._capture_state_bool(), True)


class TestQuitOrder(unittest.TestCase):
    """quit 顺序契约钉死：系统代理恢复 → SSH 停止 → 服务线 → 配置服务。
    此前这条顺序只活在 app.py 的注释里（'Match original close order'）。"""

    def test_quit_calls_in_contract_order(self):
        svc = _make_coordinator()
        order = []
        with patch.object(svc._sys_proxy, "quit_cleanup",
                          side_effect=lambda: order.append("sys_proxy")), \
             patch.object(type(svc._suanpan), "running", False), \
             patch.object(svc._capture, "stop",
                          side_effect=lambda **kw: order.append("capture")), \
             patch.object(svc._blocker, "release",
                          side_effect=lambda: order.append("caffeinate")), \
             patch.object(svc._config_server, "stop",
                          side_effect=lambda: order.append("config_server")):
            svc.quit(lambda: order.append("ssh"))
        self.assertEqual(order, ["sys_proxy", "ssh", "caffeinate", "capture",
                                 "config_server"])

    def test_start_all_clears_ports_then_starts_services(self):
        svc = _make_coordinator()
        order = []
        with patch("services.lifecycle_runtime._clear_stale_ports",
                   side_effect=lambda *a: order.append("clear_ports")), \
             patch("services.lifecycle_runtime._read_suanpan_port",
                   return_value=9527), \
             patch.object(svc._config_server, "start",
                          side_effect=lambda: order.append("config_server") or True), \
             patch.object(svc._suanpan, "start",
                          side_effect=lambda: order.append("suanpan") or True):
            svc.start_all()
        self.assertEqual(order, ["clear_ports", "config_server", "suanpan"])


if __name__ == "__main__":
    unittest.main()
