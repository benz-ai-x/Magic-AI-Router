"""UserIntents 直面单测（架构评审 R5 候选 1）。

意图的执行纪律在此钉住：guard 分派真值表 / 默认线程纪律（真 daemon
Thread）/ 通知文案 / 状态推导翻转。app.py 侧的 adapter 翻译（菜单闭包
与 _bridge_action）在 test_menu_actions 经 _make_app 的同步执行器组合
覆盖。
"""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services import intents as intents_mod
from services.intents import UserIntents

_SYNC = lambda target, name=None: target()  # noqa: E731


def _intents(**over):
    """最小依赖装配：conn/mounts 为 MagicMock，通知与 dirty 收集进 list。"""
    conn = MagicMock()
    conn.proxy_tunnel_id = "t-proxy"
    conn.proxy_connected = True
    conn.forward_sessions.return_value = []
    mounts = MagicMock()
    mounts.mount_states.return_value = []
    notes = []
    dirties = []
    deps = dict(
        conn=conn,
        mounts=mounts,
        notify=lambda s, m="": notes.append((s, m)),
        mark_dirty=lambda: dirties.append(1),
        update_mp=lambda mut: True,
        reload_config=MagicMock(),
        spawn=_SYNC,
    )
    deps.update(over)
    return UserIntents(**deps), conn, mounts, notes, dirties


class TestReconnectDispatch(unittest.TestCase):
    """reconnect_proxy_or_forward 真值表：转发会话按 id / 代理按守卫。"""

    def test_forward_session_id_dispatches_guarded_rebuild(self):
        ui, conn, _, notes, dirties = _intents()
        ui.reconnect_proxy_or_forward("t-fw", guarded=True)
        conn.restart_forward_async.assert_called_once_with(
            "t-fw", ui._reload_config, guarded=True,
            thread_name="BridgeReconnectForward")
        self.assertEqual(dirties, [1])

    def test_proxy_tunnel_id_falls_back_to_proxy_restart(self):
        ui, conn, _, _, _ = _intents()
        ui.reconnect_proxy_or_forward("t-proxy")
        conn.restart.assert_called_once()

    def test_guarded_proxy_skip_when_not_connected(self):
        """保存流守卫：未连接的代理绝不因保存配置被拉起。"""
        ui, conn, _, _, dirties = _intents()
        conn.proxy_connected = False
        ui.reconnect_proxy_or_forward(guarded=True)
        conn.restart.assert_not_called()
        self.assertEqual(dirties, [])

    def test_unguarded_reconnects_even_when_not_connected(self):
        """显式重连（Spec-A）：未连接也允许重建。"""
        ui, conn, _, _, _ = _intents()
        conn.proxy_connected = False
        ui.reconnect_proxy_or_forward(guarded=False)
        conn.restart.assert_called_once()

    def test_reconnect_marks_dirty_after_restart(self):
        ui, conn, _, _, dirties = _intents()
        ui.reconnect()
        conn.restart.assert_called_once_with(ui._reload_config)
        self.assertEqual(dirties, [1])

    def test_default_spawn_is_daemon_thread(self):
        """默认线程纪律：慢操作走 daemon Thread（不卡菜单/窗口主线程）。"""
        ui, conn, _, _, _ = _intents(spawn=None)  # 缺省执行器
        ui._reload_config = reload_mock = MagicMock()
        with patch.object(intents_mod.threading, "Thread") as thread:
            thread.return_value.start = MagicMock()
            ui.reconnect()
        thread.assert_called_once()
        self.assertIs(thread.call_args[1].get("daemon"), True)
        # start() 被 stub —— 核心未真跑（接线断言，不起真线程）
        reload_mock.assert_not_called()
        thread.return_value.start.assert_called_once()


class TestForwardSession(unittest.TestCase):

    def test_start_spawns_and_notifies_on_failure(self):
        ui, conn, _, notes, dirties = _intents()
        spawned = []
        ui._spawn = lambda target, name=None: spawned.append(target)
        conn.start_forward.return_value = (False, "端口被占")
        ui.forward_session("t-fw", "start")
        self.assertEqual(len(spawned), 1)
        spawned[0]()  # 执行后台核心
        self.assertEqual(notes, [("无法启动端口转发", "端口被占")])
        self.assertEqual(dirties, [1])

    def test_stop_is_synchronous(self):
        ui, conn, _, _, dirties = _intents()
        ui.forward_session("t-fw", "stop")
        conn.stop_forward.assert_called_once_with("t-fw")
        conn.start_forward.assert_not_called()
        self.assertEqual(dirties, [1])

    def test_toggle_derives_from_running_sessions(self):
        ui, conn, _, _, _ = _intents()
        conn.forward_sessions.return_value = [
            SimpleNamespace(tunnel_id="t-fw")]
        with patch.object(ui, "forward_session") as fs:
            ui.toggle_forward_session("t-fw")
            fs.assert_called_once_with("t-fw", "stop")
        conn.forward_sessions.return_value = []
        with patch.object(ui, "forward_session") as fs:
            ui.toggle_forward_session("t-fw")
            fs.assert_called_once_with("t-fw", "start")


class TestMount(unittest.TestCase):

    def test_explicit_mount_and_unmount(self):
        ui, conn, mounts, _, dirties = _intents()
        ui.mount("t", "data", "mount")
        mounts.start_mount.assert_called_once_with("t", "data")
        ui.mount("t", "data", "unmount")
        mounts.stop_mount.assert_called_once_with("t", "data")
        self.assertEqual(dirties, [1, 1])

    def test_toggle_derives_from_mount_states(self):
        ui, _, mounts, _, _ = _intents()
        mounts.mount_states.return_value = [SimpleNamespace(
            tunnel_id="t", name="data", status="mounted")]
        with patch.object(ui, "mount") as m:
            ui.toggle_mount("t", "data")
            m.assert_called_once_with("t", "data", "unmount")
        mounts.mount_states.return_value = [SimpleNamespace(
            tunnel_id="t", name="data", status="error")]
        with patch.object(ui, "mount") as m:
            ui.toggle_mount("t", "data")
            m.assert_called_once_with("t", "data", "mount")


class TestSetCapture(unittest.TestCase):

    def test_disable_is_immediate(self):
        ctrl = MagicMock()
        ctrl.enabled = True
        ui, _, _, _, dirties = _intents(capture_ctrl=ctrl)
        ui.set_capture(False)
        ctrl.disable.assert_called_once()
        self.assertEqual(dirties, [1])

    def test_enable_failure_alerts(self):
        ctrl = MagicMock()
        ctrl.enable.return_value = False
        alerts = []
        ui, _, _, _, dirties = _intents(
            capture_ctrl=ctrl, alert=alerts.append)
        ui.set_capture(True)
        ctrl.enable.assert_called_once()
        self.assertEqual(len(alerts), 1)
        self.assertIn("mitmdump", alerts[0])
        self.assertEqual(dirties, [1])

    def test_enable_success_no_alert(self):
        ctrl = MagicMock()
        ctrl.enable.return_value = True
        alerts = []
        ui, _, _, _, _ = _intents(capture_ctrl=ctrl, alert=alerts.append)
        ui.set_capture(True)
        self.assertEqual(alerts, [])


class TestCopyAgentInstructions(unittest.TestCase):

    def test_latch_then_clipboard_then_notify(self):
        calls = []
        ui, _, _, notes, _ = _intents(
            hold_copy_latch=lambda: calls.append("latch"),
            get_agent_instructions=lambda: "curl ...",
        )
        with patch.object(intents_mod.subprocess, "Popen") as popen:
            ui.copy_agent_instructions()
        self.assertEqual(calls, ["latch"])  # 先闩锁——curl 立即可用
        popen.assert_called_once()
        self.assertEqual(notes, [("已复制 AI 助手指令",
                                  "含 token 的 curl 已就绪；配置 API 已开启供助手访问")])


if __name__ == "__main__":
    unittest.main()
