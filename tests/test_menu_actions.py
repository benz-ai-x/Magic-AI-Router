"""Tests for MagicProxyApp menu callbacks — UI callback logic.

The callbacks live directly on MagicProxyApp.  Since MagicProxyApp extends
rumps.App (heavy ObjC setup), tests create the instance via __new__
(skipping __init__) and hand-set attributes.  This keeps internal method
dispatch (self._dirty, self.reconnect, etc.) working through real bound
methods.

Candidate-1 refactor: MagicProxyApp 直接持有 _suanpan / _capture_ctrl /
_sys_proxy；ServiceCoordinator 不再暴露这些子模块的直通属性。测试中把
_svc 退化成只负责 tick/sync_sleep/stop_all 的 MagicMock，子模块改成 App
的直属属性。
"""
import unittest
from unittest.mock import MagicMock, PropertyMock, patch

import app
from app import MagicProxyApp


def _make_app(config=None):
    """Build a MagicProxyApp without rumps.App.__init__ for callback testing."""
    a = MagicProxyApp.__new__(MagicProxyApp)
    a._conn = MagicMock()
    a._mounts = MagicMock()
    a._lifecycle = MagicMock()
    a._suanpan = MagicMock()
    a._capture_ctrl = MagicMock()
    a._sys_proxy = MagicMock()
    a._config = config if config is not None else {
        "http_listen_port": 8888,
        "capture_port": 8080,
        "capture_dir": "~/captures",
        "current_tunnel": 0,
    }
    a._menu_builder = MagicMock()
    a.VERSION_DISPLAY = "0.4.2"
    a._log_path = "/tmp/log.txt"
    a._log_buffer = MagicMock()
    # #46：菜单开关写径走真事务 store（conftest 会话沙箱重定向 PATHS，
    # 不会触碰真实配置文件）
    from mpconf.config_state import ConfigStateStore
    a._config_store = ConfigStateStore(keychain=None)
    # ADR-009：配置服务持有者（_sync_config_server 读；_lifecycle 已是
    # MagicMock，收敛调用被吸收）
    a._config_window_open = False
    a._copy_api_latch = False
    return a


class TestConnectionActions(unittest.TestCase):
    def test_cancel_connection_delegates(self):
        a = _make_app()
        a.cancel_connection(None)
        a._conn.cancel.assert_called_once()

    def test_reconnect_restarts_and_dirties(self):
        a = _make_app()
        with patch.object(app, "load_config", return_value=None):
            a.reconnect(None)
        a._conn.restart.assert_called_once()
        self.assertIsNone(a._menu_builder.last_struct_key)

    def test_reconnect_merges_loaded_config(self):
        a = _make_app()
        # Make restart actually invoke the reload callback
        a._conn.restart.side_effect = lambda fn: fn()
        with patch.object(app, "load_config", return_value={"tunnels": []}), \
             patch.object(app, "merge_config", return_value={"merged": True}):
            a.reconnect(None)
        self.assertEqual(a._config, {"merged": True})

    def test_toggle_pause_syncs_proxy_and_sleep(self):
        a = _make_app()
        a._conn.ssh.status = "connected"
        a._conn.paused = False
        a.toggle_pause(None)
        a._conn.toggle_pause.assert_called_once()
        a._sys_proxy.sync.assert_called_once()
        a._lifecycle.sync_sleep.assert_called_once()

    def test_toggle_system_proxy_delegates(self):
        a = _make_app()
        a.toggle_system_proxy(None)
        a._sys_proxy.toggle.assert_called_once()

    def test_switch_tunnel_noop_when_current_and_connected(self):
        a = _make_app()
        a._conn.ssh.status = "connected"
        switch = a.make_switch_tunnel(0)  # already current (current_tunnel=0)
        with patch.object(a, "_update_mp_config") as upd:
            switch(None)
        upd.assert_not_called()

    def test_switch_tunnel_persists_and_reconnects(self):
        # #46：切换隧道经事务写径落盘（磁盘可见），再重连。写径写前
        # 读新——种子须先落沙箱磁盘，内存 _config 会被磁盘真相刷新
        tunnels = [
            {"name": "t1", "ssh_user": "u", "ssh_host": "h1",
             "ssh_port": 22, "auth_type": "key"},
            {"name": "t2", "ssh_user": "u", "ssh_host": "h2",
             "ssh_port": 22, "auth_type": "key"}]
        import json as _json_seed
        from shared import config_store as _cs
        with open(_cs.PATHS["mp"], "w") as f:
            _json_seed.dump({"current_tunnel": 0, "tunnels": tunnels}, f)
        a = _make_app({"current_tunnel": 0, "tunnels": tunnels})
        a._conn.ssh.status = "stopped"
        switch = a.make_switch_tunnel(1)
        switch(None)
        self.assertEqual(a._config["current_tunnel"], 1)
        import json as _json
        from shared import config_store
        disk = _json.loads(open(config_store.PATHS["mp"]).read())
        self.assertEqual(disk.get("current_tunnel"), 1)
        a._conn.restart.assert_called_once()


class TestSuanpanActions(unittest.TestCase):
    """toggle/reload/restart 现在内联在 app.py 里，直接调用
    SuanpanRuntime 的公开方法（running / start / stop / reload /
    listen_address / error）并组装通知文案。
    """

    def test_toggle_suanpan_running_notifies_started(self):
        a = _make_app()
        a._suanpan.running = False
        a._suanpan.start.return_value = True
        a._suanpan.listen_address.return_value = "127.0.0.1:9527"
        with patch.object(app.rumps, "notification") as notif:
            a.toggle_suanpan(None)
        notif.assert_called_once()
        self.assertIn("已启动", notif.call_args[0][1])
        a._menu_builder.build.assert_called_once()

    def test_toggle_suanpan_error_notifies_failure(self):
        a = _make_app()
        a._suanpan.running = False
        a._suanpan.start.return_value = False
        type(a._suanpan).error = PropertyMock(return_value="missing deps")
        with patch.object(app.rumps, "notification") as notif:
            a.toggle_suanpan(None)
        self.assertIn("启动失败", notif.call_args[0][1])

    def test_toggle_suanpan_stopped_notifies(self):
        a = _make_app()
        a._suanpan.running = True
        with patch.object(app.rumps, "notification") as notif:
            a.toggle_suanpan(None)
        self.assertIn("已停止", notif.call_args[0][1])

    def test_reload_suanpan_success(self):
        a = _make_app()
        a._suanpan.reload.return_value = True
        with patch.object(app.rumps, "notification") as notif:
            a.reload_suanpan(None)
        self.assertIn("已重载", notif.call_args[0][1])
        a._menu_builder.build.assert_called_once()

    def test_reload_suanpan_failure(self):
        a = _make_app()
        a._suanpan.reload.return_value = False
        type(a._suanpan).error = PropertyMock(return_value="bad config")
        with patch.object(app.rumps, "notification") as notif:
            a.reload_suanpan(None)
        self.assertIn("重载失败", notif.call_args[0][1])

    def test_restart_suanpan_success_notifies_restarted(self):
        a = _make_app()
        a._suanpan.running = True
        a._suanpan.start.return_value = True
        a._suanpan.listen_address.return_value = "127.0.0.1:9527"
        with patch.object(app.rumps, "notification") as notif:
            a.restart_suanpan(None)
        notif.assert_called_once()
        self.assertIn("已重启", notif.call_args[0][1])
        a._menu_builder.build.assert_called_once()

    def test_restart_suanpan_failure_notifies(self):
        a = _make_app()
        a._suanpan.running = True
        a._suanpan.start.return_value = False
        type(a._suanpan).error = PropertyMock(return_value="missing deps")
        with patch.object(app.rumps, "notification") as notif:
            a.restart_suanpan(None)
        self.assertIn("重启失败", notif.call_args[0][1])

    def test_copy_suanpan_url_uses_pbcopy(self):
        a = _make_app()
        a._suanpan.listen_address.return_value = "127.0.0.1:9527"
        with patch.object(app.subprocess, "Popen") as popen, \
             patch.object(app.rumps, "notification"):
            a.copy_suanpan_url(None)
        popen.assert_called_once()
        self.assertEqual(popen.call_args[0][0], ["pbcopy"])

    def test_copy_suanpan_example_missing_file(self):
        a = _make_app()
        with patch.object(app, "resource_path", return_value="/nope.yaml"), \
             patch.object(app.os.path, "exists", return_value=False), \
             patch.object(app.rumps, "notification") as notif:
            a.copy_suanpan_example(None)
        self.assertIn("文件未找到", notif.call_args[0][2])

    def test_copy_suanpan_example_reads_file(self):
        a = _make_app()
        m = unittest.mock.mock_open(read_data="listen: x")
        with patch.object(app, "resource_path", return_value="/ex.yaml"), \
             patch.object(app.os.path, "exists", return_value=True), \
             patch.object(app, "open", m), \
             patch.object(app.subprocess, "Popen"), \
             patch.object(app.rumps, "notification") as notif:
            a.copy_suanpan_example(None)
        self.assertIn("字节", notif.call_args[0][2])


class TestSleepLoginActions(unittest.TestCase):
    def test_toggle_prevent_sleep_flips_and_persists(self):
        a = _make_app({"prevent_sleep": False})
        a.toggle_prevent_sleep(None)
        self.assertTrue(a._config["prevent_sleep"])
        import json as _json
        from shared import config_store
        disk = _json.loads(open(config_store.PATHS["mp"]).read())
        self.assertIs(disk.get("prevent_sleep"), True)
        a._lifecycle.sync_sleep.assert_called_once()

    def test_toggle_launch_at_login_success(self):
        a = _make_app({"launch_at_login": False})
        with patch.object(app.login_item, "set_launch_at_login",
                          return_value=(True, "")), \
             patch.object(app.rumps, "notification") as notif:
            a.toggle_launch_at_login(None)
        self.assertTrue(a._config["launch_at_login"])
        import json as _json
        from shared import config_store
        disk = _json.loads(open(config_store.PATHS["mp"]).read())
        self.assertIs(disk.get("launch_at_login"), True)
        self.assertIn("登录启动：开", notif.call_args[0][1])

    def test_toggle_launch_at_login_failure_alerts(self):
        a = _make_app({"launch_at_login": False})
        with patch.object(app.login_item, "set_launch_at_login",
                          return_value=(False, "denied")), \
             patch.object(a, "_update_mp_config") as upd, \
             patch.object(app.rumps, "alert") as alert:
            a.toggle_launch_at_login(None)
        alert.assert_called_once()
        # On failure the flag is left unchanged and config not saved
        self.assertFalse(a._config["launch_at_login"])
        upd.assert_not_called()


class TestCaptureActions(unittest.TestCase):
    def test_toggle_capture_off_disables(self):
        a = _make_app()
        a._capture_ctrl.enabled = True
        a.toggle_capture(None)
        a._capture_ctrl.disable.assert_called_once()

    def test_toggle_capture_on_trusted_enables(self):
        a = _make_app()
        a._capture_ctrl.enabled = False
        a._capture_ctrl.enable.return_value = True
        with patch.object(app.port_check, "who_owns", return_value=None), \
             patch.object(app.ca_trust, "is_trusted", return_value=True):
            a.toggle_capture(None)
        a._capture_ctrl.enable.assert_called_once()

    def test_toggle_capture_port_occupied_declined(self):
        a = _make_app()
        a._capture_ctrl.enabled = False
        owner = MagicMock(name="proc", pid=99, cmd="some cmd")
        with patch.object(app.port_check, "who_owns", return_value=owner), \
             patch.object(app.rumps, "alert", return_value=0), \
             patch.object(app.ca_trust, "is_trusted") as trusted:
            a.toggle_capture(None)
        trusted.assert_not_called()

    def test_enable_capture_alerts_when_mitmdump_missing(self):
        a = _make_app()
        a._capture_ctrl.enable.return_value = False
        with patch.object(app.rumps, "alert") as alert:
            a._enable_capture_or_alert()
        alert.assert_called_once()

    def test_open_capture_dir(self):
        a = _make_app()
        with patch("capture.capture_store.prepare", return_value="/tmp/cap"), \
             patch.object(app.subprocess, "Popen") as popen:
            a.open_capture_dir(None)
        popen.assert_called_once()

    def test_open_today_jsonl_existing(self):
        a = _make_app()
        with patch("capture.capture_store.prepare", return_value="/tmp/cap"), \
             patch.object(app.os.path, "exists", return_value=True), \
             patch.object(app.time, "strftime", return_value="2026-08-10"), \
             patch.object(app.subprocess, "Popen") as popen:
            a.open_today_jsonl(None)
        self.assertEqual(popen.call_args[0][0][1], "-t")


class TestMiscActions(unittest.TestCase):
    def test_open_log(self):
        a = _make_app()
        with patch.object(app.subprocess, "Popen") as popen:
            a.open_log(None)
        popen.assert_called_once()

    def test_show_log_window(self):
        a = _make_app()
        with patch.object(app, "show_log_window") as slw:
            a.show_log_window(None)
        slw.assert_called_once()

    def test_about_alerts_version(self):
        a = _make_app()
        a.VERSION_DISPLAY = "0.4.3.08102116"
        with patch.object(app.rumps, "alert") as alert:
            a.about(None)
        self.assertIn("0.4.3.08102116", alert.call_args[1]["message"])


class TestLaunchAppProxied(unittest.TestCase):
    def test_missing_app_path_alerts(self):
        a = _make_app()
        with patch.object(app.chromium_proxy, "app_path", return_value=None), \
             patch.object(app.rumps, "alert") as alert:
            a._launch_app_proxied({"name": "Chrome"})
        alert.assert_called_once()

    def test_launch_success_alerts(self):
        a = _make_app()
        with patch.object(app.chromium_proxy, "is_running", return_value=False), \
             patch.object(app.chromium_proxy, "launch", return_value=(True, "")), \
             patch.object(app.rumps, "alert") as alert:
            a._launch_app_proxied({"name": "Chrome", "path": "/App/Chrome.app"})
        alert.assert_called_once()

    def test_launch_failure_alerts(self):
        a = _make_app()
        with patch.object(app.chromium_proxy, "is_running", return_value=False), \
             patch.object(app.chromium_proxy, "launch", return_value=(False, "err")), \
             patch.object(app.rumps, "alert") as alert:
            a._launch_app_proxied({"name": "Chrome", "path": "/App/Chrome.app"})
        self.assertIn("启动失败", alert.call_args[1]["message"])

    def test_toggle_capture_untrusted_guide_trusted_enables(self):
        a = _make_app()
        a._capture_ctrl.enabled = False
        a._capture_ctrl.enable.return_value = True
        captured = {}

        def fake_guide(on_result=None):
            captured["cb"] = on_result

        with patch.object(app.port_check, "who_owns", return_value=None), \
             patch.object(app.ca_trust, "is_trusted", return_value=False), \
             patch.object(app.ca_trust, "show_ca_trust_guide", side_effect=fake_guide):
            a.toggle_capture(None)
        captured["cb"](True)
        a._capture_ctrl.enable.assert_called_once()

    def test_toggle_capture_untrusted_guide_declined_dirties(self):
        a = _make_app()
        a._capture_ctrl.enabled = False
        captured = {}

        def fake_guide(on_result=None):
            captured["cb"] = on_result

        with patch.object(app.port_check, "who_owns", return_value=None), \
             patch.object(app.ca_trust, "is_trusted", return_value=False), \
             patch.object(app.ca_trust, "show_ca_trust_guide", side_effect=fake_guide):
            a.toggle_capture(None)
        captured["cb"](False)
        self.assertIsNone(a._menu_builder.last_struct_key)


class TestOSErrorPaths(unittest.TestCase):
    def test_open_capture_dir_oserror_swallowed(self):
        a = _make_app()
        with patch("capture.capture_store.prepare",
                   side_effect=OSError("denied")):
            a.open_capture_dir(None)  # should not raise

    def test_open_today_jsonl_missing_file_opens_dir(self):
        a = _make_app()
        with patch("capture.capture_store.prepare", return_value="/tmp/cap"), \
             patch.object(app.os.path, "exists", return_value=False), \
             patch.object(app.time, "strftime", return_value="2026-08-10"), \
             patch.object(app.subprocess, "Popen") as popen:
            a.open_today_jsonl(None)
        # Falls back to opening the directory (no -t flag)
        self.assertNotIn("-t", popen.call_args[0][0])

    def test_open_today_jsonl_oserror_swallowed(self):
        a = _make_app()
        with patch.object(app.os, "makedirs", side_effect=OSError("denied")):
            a.open_today_jsonl(None)

    def test_open_log_oserror_swallowed(self):
        a = _make_app()
        with patch.object(app.subprocess, "Popen", side_effect=OSError("no open")):
            a.open_log(None)

    def test_show_log_window_exception_swallowed(self):
        a = _make_app()
        with patch.object(app, "show_log_window", side_effect=RuntimeError("boom")):
            a.show_log_window(None)


class TestLaunchProxiedRunningApp(unittest.TestCase):
    def test_make_launch_proxied_returns_callback(self):
        a = _make_app()
        with patch.object(a, "_launch_app_proxied") as launch:
            cb = a.make_launch_proxied({"name": "X"})
            cb(None)
        launch.assert_called_once_with({"name": "X"})

    def test_running_app_user_confirms_relaunch(self):
        a = _make_app()
        with patch.object(app.chromium_proxy, "is_running", return_value=True), \
             patch.object(app.rumps, "alert", return_value=1), \
             patch.object(app.chromium_proxy, "quit_app") as quit_app, \
             patch.object(app.chromium_proxy, "wait_until_stopped", return_value=True), \
             patch.object(app.chromium_proxy, "launch", return_value=(True, "")):
            a._launch_app_proxied({"name": "Chrome", "path": "/App/Chrome.app"})
        quit_app.assert_called_once()

    def test_running_app_user_cancels(self):
        a = _make_app()
        with patch.object(app.chromium_proxy, "is_running", return_value=True), \
             patch.object(app.rumps, "alert", return_value=0), \
             patch.object(app.chromium_proxy, "launch") as launch:
            a._launch_app_proxied({"name": "Chrome", "path": "/App/Chrome.app"})
        launch.assert_not_called()

    def test_running_app_quit_times_out(self):
        a = _make_app()
        with patch.object(app.chromium_proxy, "is_running", return_value=True), \
             patch.object(app.rumps, "alert", return_value=1), \
             patch.object(app.chromium_proxy, "quit_app"), \
             patch.object(app.chromium_proxy, "wait_until_stopped", return_value=False), \
             patch.object(app.chromium_proxy, "launch") as launch:
            a._launch_app_proxied({"name": "Chrome", "path": "/App/Chrome.app"})
        launch.assert_not_called()


class TestBridgeActions(unittest.TestCase):
    """设置窗 bridge 动作分发（reconnectProxy / openPath captureDir）。"""

    def test_reconnect_action_runs_reconnect_off_thread(self):
        a = _make_app()
        with patch.object(app.threading, "Thread") as thread:
            a._bridge_action({"type": "reconnectProxy"})
        thread.assert_called_once()
        self.assertEqual(thread.call_args[1].get("daemon"), True)
        # The reconnect callback itself is what the thread runs — verify the
        # wiring without actually spawning it.
        self.assertEqual(thread.call_args[1]["target"], a.reconnect)

    def test_open_path_capture_dir_reuses_menu_handler(self):
        a = _make_app()
        with patch.object(a, "open_capture_dir") as ocd:
            a._bridge_action({"type": "openPath", "kind": "captureDir"})
        ocd.assert_called_once_with(None)

    def test_open_path_unknown_kind_ignored(self):
        a = _make_app()
        with patch.object(a, "open_capture_dir") as ocd:
            a._bridge_action({"type": "openPath", "kind": "/etc"})
        ocd.assert_not_called()

    def test_unknown_action_ignored(self):
        a = _make_app()
        a._bridge_action({"type": "bogus"})  # must not raise


class TestCheckPortsEdge(unittest.TestCase):
    def test_check_port_free_returns_true(self):
        a = _make_app()
        with patch.object(app.port_check, "who_owns", return_value=None):
            self.assertTrue(a._check_port(8888, "HTTP"))

    def test_check_port_occupied_kill_confirmed(self):
        a = _make_app()
        owner = MagicMock(name="proc", pid=42, cmd="cmd")
        with patch.object(app.port_check, "who_owns", return_value=owner), \
             patch.object(app.rumps, "alert", return_value=1), \
             patch.object(app.port_check, "kill", return_value=(True, "")):
            self.assertTrue(a._check_port(8888, "HTTP"))

    def test_check_port_kill_fails(self):
        a = _make_app()
        owner = MagicMock(name="proc", pid=42, cmd="cmd")
        with patch.object(app.port_check, "who_owns", return_value=owner), \
             patch.object(app.rumps, "alert", return_value=1), \
             patch.object(app.port_check, "kill", return_value=(False, "err")):
            self.assertFalse(a._check_port(8888, "HTTP"))

    def test_check_both_ports(self):
        a = _make_app()
        with patch.object(a, "_check_port", return_value=True) as cp:
            a.check_both_ports()
        self.assertEqual(cp.call_count, 2)

    def test_check_both_ports_bad_http_listen(self):
        a = _make_app({"http_listen": "no-port-here"})
        with patch.object(a, "_check_port", return_value=True) as cp:
            a.check_both_ports()
        # Only SOCKS5 is checked; HTTP port parse fails silently
        self.assertEqual(cp.call_count, 1)


if __name__ == "__main__":
    unittest.main()


class TestMpSavedConverge(unittest.TestCase):
    """UI 保存 MP 段后的内存副本收敛（_on_mp_saved）：重读磁盘刷新内存 +
    登录启动与菜单路径对齐（配置外副作用补注册/注销）。"""

    def _make_saved_app(self, current):
        a = _make_app(dict(current))
        return a

    def test_refreshes_in_memory_config_and_marks_menu_dirty(self):
        a = self._make_saved_app({"launch_at_login": False})
        disk = {"launch_at_login": False, "prevent_sleep": True}
        with patch.object(app, "load_config", return_value=disk), \
             patch.object(app, "merge_config", side_effect=lambda c: c), \
             patch.object(app.login_item, "set_launch_at_login") as reg:
            a._on_mp_saved()
        self.assertIs(a._config["prevent_sleep"], True)
        self.assertIsNone(a._menu_builder.last_struct_key)
        reg.assert_not_called()

    def test_launch_at_login_change_syncs_launch_agent(self):
        a = self._make_saved_app({"launch_at_login": False})
        with patch.object(app, "load_config",
                          return_value={"launch_at_login": True}), \
             patch.object(app, "merge_config", side_effect=lambda c: c), \
             patch.object(app.login_item, "set_launch_at_login",
                          return_value=(True, "")) as reg:
            a._on_mp_saved()
        reg.assert_called_once_with(True)

    def test_unchanged_launch_at_login_does_not_re_register(self):
        a = self._make_saved_app({"launch_at_login": True})
        with patch.object(app, "load_config",
                          return_value={"launch_at_login": True}), \
             patch.object(app, "merge_config", side_effect=lambda c: c), \
             patch.object(app.login_item, "set_launch_at_login") as reg:
            a._on_mp_saved()
        reg.assert_not_called()

    def test_identity_migration_error_keeps_old_config(self):
        from shared.identity import IdentityMigrationError
        a = self._make_saved_app({"prevent_sleep": False})
        with patch.object(app, "load_config",
                          side_effect=IdentityMigrationError("dup")), \
             patch.object(app.login_item, "set_launch_at_login") as reg:
            a._on_mp_saved()
        self.assertIs(a._config.get("prevent_sleep"), False)
        reg.assert_not_called()

    def test_missing_config_file_is_noop(self):
        a = self._make_saved_app({"prevent_sleep": False})
        with patch.object(app, "load_config", return_value=None):
            a._on_mp_saved()  # 不得抛
        self.assertIs(a._config.get("prevent_sleep"), False)


class TestSwitchTunnelStableRole(unittest.TestCase):
    """v0.9.2：菜单切换代理角色写稳定 id（current_tunnel_id 真相 +
    current_tunnel 下标投影），删除/调序不再让角色漂移。"""

    def test_switch_tunnel_persists_stable_id_role(self):
        tunnels = [
            {"name": "t1", "ssh_user": "u", "ssh_host": "h1",
             "ssh_port": 22, "auth_type": "key", "id": "t-a"},
            {"name": "t2", "ssh_user": "u", "ssh_host": "h2",
             "ssh_port": 22, "auth_type": "key", "id": "t-b"}]
        import json as _json_seed
        from shared import config_store as _cs
        with open(_cs.PATHS["mp"], "w") as f:
            _json_seed.dump({"current_tunnel": 0, "current_tunnel_id": "t-a",
                             "tunnels": tunnels}, f)
        a = _make_app({"current_tunnel": 0, "current_tunnel_id": "t-a",
                       "tunnels": tunnels})
        a._conn.ssh.status = "stopped"
        a.make_switch_tunnel(1)(None)
        self.assertEqual(a._config["current_tunnel_id"], "t-b")
        self.assertEqual(a._config["current_tunnel"], 1)
        import json as _json
        from shared import config_store
        disk = _json.loads(open(config_store.PATHS["mp"]).read())
        self.assertEqual(disk.get("current_tunnel_id"), "t-b")
        self.assertEqual(disk.get("current_tunnel"), 1)
        a._conn.restart.assert_called_once()


class TestToggleForward(unittest.TestCase):
    """菜单「端口映射逐条启停」：翻转磁盘 enabled + 守卫重建。"""

    class _FakeThread:
        """同步执行 target——保断言确定性（daemon 真线程会竞态）。"""
        def __init__(self, target=None, args=(), name=None, daemon=None):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

    def _seed_and_app(self, forwards, tid="t-abc"):
        import json as _json
        from shared import config_store as _cs
        tunnels = [{"name": "fw", "id": tid, "ssh_user": "u",
                    "ssh_host": "h", "ssh_port": 22, "auth_type": "key",
                    "forwards": forwards}]
        with open(_cs.PATHS["mp"], "w") as f:
            _json.dump({"current_tunnel": 0, "tunnels": tunnels}, f)
        a = _make_app({"current_tunnel": 0, "tunnels": tunnels})
        return a, tid

    def test_flips_disk_enabled_and_rebuilds_running_session(self):
        a, tid = self._seed_and_app(
            [{"local_port": 9000, "remote_host": "127.0.0.1",
              "remote_port": 80, "enabled": True}])
        a._conn.proxy_tunnel_id = "t-other"
        a.make_toggle_forward(tid, 0)(None)
        import json as _json
        from shared import config_store
        disk = _json.loads(open(config_store.PATHS["mp"]).read())
        # 磁盘翻转走事务写径；重建分发恒走守卫入口（guarded 默认 True，
        # 未连接绝不拉起的真值表见 coordinator 的 TestForwardGuards）
        self.assertIs(disk["tunnels"][0]["forwards"][0]["enabled"], False)
        a._conn.restart_forward_async.assert_called_once_with(
            tid, a._reload_config_or_alert,
            thread_name="ToggleForwardRebuild")
        a._conn.restart_forward.assert_not_called()

    def test_running_session_gets_rebuild(self):
        a, tid = self._seed_and_app(
            [{"local_port": 9000, "remote_port": 80}])
        a._conn.forward_connected.return_value = True
        a._conn.restart_forward_async.return_value = True
        a._conn.proxy_tunnel_id = "t-other"
        a.make_toggle_forward(tid, 0)(None)
        a._conn.restart_forward_async.assert_called_once_with(
            tid, a._reload_config_or_alert,
            thread_name="ToggleForwardRebuild")

    def test_error_state_session_not_pulled_up(self):
        """review c-1：SSH 失败滞留的 error 态会话——翻转绝不拉起。
        app 只经守卫入口分发（guard 在 coordinator 内判定）；底层
        restart_forward 不被触达。"""
        a, tid = self._seed_and_app(
            [{"local_port": 9000, "remote_port": 80}])
        a._conn.proxy_tunnel_id = "t-other"
        a.make_toggle_forward(tid, 0)(None)
        a._conn.restart_forward_async.assert_called_once_with(
            tid, a._reload_config_or_alert,
            thread_name="ToggleForwardRebuild")
        a._conn.restart_forward.assert_not_called()

    def test_proxy_connecting_not_pulled_up(self):
        """review c-1：connecting 不算在跑——守卫谓词 proxy_connected
        （仅 connected）归 coordinator，app 只消费。"""
        a, tid = self._seed_and_app(
            [{"local_port": 9000, "remote_port": 80}])
        a._conn.proxy_tunnel_id = tid
        a._conn.proxy_connected = False
        with patch.object(a, "reconnect") as rc:
            a.make_toggle_forward(tid, 0)(None)
            rc.assert_not_called()

    def test_disabling_last_enabled_forward_says_stopped(self):
        """review c-3：全停用后 restart 实为收敛停止——通知如实。"""
        a, tid = self._seed_and_app(
            [{"local_port": 9000, "remote_port": 80}])
        a._conn.forward_connected.return_value = True
        a._conn.restart_forward_async.return_value = True
        a._conn.proxy_tunnel_id = "t-other"
        with patch.object(a, "_notify") as notify:
            a.make_toggle_forward(tid, 0)(None)
        a._conn.restart_forward_async.assert_called_once()
        msg = notify.call_args[0][1]
        self.assertIn("已停止", msg)
        self.assertNotIn("重建中", msg)

    def test_proxy_tunnel_rebuild_only_when_connected(self):
        a, tid = self._seed_and_app(
            [{"local_port": 9000, "remote_port": 80}])
        a._conn.proxy_tunnel_id = tid   # 该隧道就是代理隧道
        a._conn.proxy_connected = False
        with patch.object(a, "reconnect") as rc:
            a.make_toggle_forward(tid, 0)(None)
            rc.assert_not_called()   # 代理未跑：只写配置
            a._conn.proxy_connected = True
            a.make_toggle_forward(tid, 0)(None)
            rc.assert_called_once()
