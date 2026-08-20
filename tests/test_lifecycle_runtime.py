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
        with patch("services.lifecycle_runtime.report_port_occupancy",
                   side_effect=lambda *a: order.append("clear_ports")), \
             patch("services.lifecycle_runtime._read_suanpan_port",
                   return_value=9527), \
             patch.object(svc._config_server, "start",
                          side_effect=lambda: order.append("config_server") or True), \
             patch.object(svc._suanpan, "start",
                          side_effect=lambda: order.append("suanpan") or True):
            svc.start_all()
        self.assertEqual(order, ["clear_ports", "config_server", "suanpan"])


class TestPortHelpers(unittest.TestCase):
    def test_read_suanpan_port_falls_back_on_error(self):
        from services import lifecycle_runtime as lr
        with patch.object(lr.config_store, "suanpan_listen",
                          side_effect=RuntimeError("boom")):
            self.assertEqual(lr._read_suanpan_port(), 9527)
        with patch.object(lr.config_store, "suanpan_listen",
                          return_value="127.0.0.1:9530"):
            self.assertEqual(lr._read_suanpan_port(), 9530)


class TestExposedProperties(unittest.TestCase):
    """属性面是 app.py 的合法引用通道——钉死五个 getter 的身份等同。"""

    def test_properties_return_the_constructed_submodules(self):
        svc = _make_coordinator()
        self.assertIs(svc.suanpan, svc._suanpan)
        self.assertIs(svc.capture_ctrl, svc._capture_ctrl)
        self.assertIs(svc.sys_proxy, svc._sys_proxy)
        self.assertIs(svc.capture, svc._capture)
        self.assertIs(svc.config_server, svc._config_server)


class TestInternalizedReload(unittest.TestCase):
    """reload 链内化：_on_sp_saved 直达 suanpan.reload，不出模块。"""

    def test_on_sp_saved_reloads_suanpan(self):
        svc = _make_coordinator()
        with patch.object(svc._suanpan, "reload") as mock_reload:
            svc._on_sp_saved()
        mock_reload.assert_called_once_with()


class TestStartAllFailureBranches(unittest.TestCase):
    def test_config_server_start_failure_warns_but_gateway_still_starts(self):
        svc = _make_coordinator()
        with patch.object(svc._config_server, "start", return_value=False), \
             patch.object(svc._suanpan, "start", return_value=True) as sp_start, \
             patch("services.lifecycle_runtime.report_port_occupancy"), \
             patch("services.lifecycle_runtime._read_suanpan_port",
                   return_value=9527), \
             self.assertLogs("magic-proxy.lifecycle", level="WARNING") as logs:
            svc.start_all()
        sp_start.assert_called_once()
        self.assertTrue(any("Config server failed" in m for m in logs.output))

    def test_suanpan_start_failure_warns(self):
        svc = _make_coordinator()
        with patch.object(svc._config_server, "start", return_value=True), \
             patch.object(svc._suanpan, "start", return_value=False), \
             patch("services.lifecycle_runtime.report_port_occupancy"), \
             patch("services.lifecycle_runtime._read_suanpan_port",
                   return_value=9527), \
             self.assertLogs("magic-proxy.lifecycle", level="WARNING") as logs:
            svc.start_all()
        self.assertTrue(any("Suanpan gateway auto-start failed" in m
                            for m in logs.output))

    def _lifecycle(self, owner):
        svc = _make_coordinator()
        svc._owner = owner
        return svc

    def test_start_all_aborts_when_sibling_holds_lock(self):
        from sysctl import instance_owner as io
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            holder = io.InstanceOwner(lock_path=str(Path(d) / "i.json"),
                                      pid_info=lambda p: ("S_A", "/exe"), pid=1)
            holder.acquire()
            sibling = io.InstanceOwner(lock_path=holder.lock_path,
                                       pid_info=lambda p: ("S_A", "/exe"), pid=2)
            svc = _make_coordinator()
            svc._owner = sibling  # acquire 将返回 None（活锁冲突）
            with patch.object(svc._config_server, "start") as cs_start, \
                 patch.object(svc._suanpan, "start") as sp_start, \
                 self.assertLogs("magic-proxy.lifecycle", level="ERROR"):
                ok = svc.start_all()
            self.assertFalse(ok)
            cs_start.assert_not_called()
            sp_start.assert_not_called()

    def test_quit_releases_instance_lock(self):
        from sysctl import instance_owner as io
        import tempfile, os as _os
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            owner = io.InstanceOwner(lock_path=str(Path(d) / "i.json"),
                                     pid_info=lambda p: ("S_A", "/exe"), pid=1)
            owner.acquire()
            svc = _make_coordinator()
            svc._owner = owner
            with patch.object(svc._sys_proxy, "quit_cleanup"), \
                 patch.object(type(svc._suanpan), "running", False), \
                 patch.object(svc._capture, "stop"), \
                 patch.object(svc._config_server, "stop"):
                svc.quit(lambda: None)
            self.assertFalse(_os.path.exists(owner.lock_path))
