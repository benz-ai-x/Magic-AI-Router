"""Tests for app.py's capture-mode integration (ADR-001 Task 4).

The pure capture logic (state machine, menu titles, mitmdump resolution) now
lives on CaptureController and is tested directly in test_capture_controller.py.
What remains here is the app-level orchestration: the _toggle_capture UI gating
(port check → CA trust), the file-opener menu actions, the quit cleanup, and the
SystemProxyController target-port behaviour (which reads capture state).

MagicProxyApp.__init__ needs a live NSRunLoop setup, so tests instantiate via
__new__ and hand-set attributes. CaptureMonitor is mocked (its own behaviour is
covered by test_capture.py).

Candidate-1 refactor: MagicProxyApp 直接持有 _suanpan / _capture_ctrl /
_sys_proxy / _capture；不再经 ``inst._svc.xxx`` 访问子模块。_svc 只剩
tick / sync_sleep / stop_all 三个薄编排方法。
"""
import os
import unittest
from unittest.mock import MagicMock, patch

import app
from capture import capture
from capture import capture_controller
from capture.capture_controller import CaptureController
from capture.resources import CaptureResources


from sysctl import system_proxy


def _stub_resources():
    return CaptureResources("/bin/mitmdump", "/x/addon.py", "/tmp/cap")
def _new_app(**attrs):
    inst = app.MagicProxyApp.__new__(app.MagicProxyApp)
    monitor = attrs.pop("_capture", MagicMock())
    config = attrs.pop("_config", {"http_listen_port": 8888, "capture_port": 8080,
                                   "capture_dir": capture.DEFAULT_CAPTURE_DIR})
    capture_ctrl = CaptureController(monitor, config_fn=lambda: inst._config)
    capture_ctrl._enabled = attrs.pop("_capture_enabled", False)

    # Build mock coordinators so compat properties on App resolve correctly
    ssh_mock = attrs.pop("_ssh", MagicMock(status="stopped", error_msg="", log=""))
    conn = MagicMock()
    conn.ssh = ssh_mock
    conn.paused = attrs.pop("_paused", False)
    conn.proxy_running = attrs.pop("_proxy_running", False)

    svc = MagicMock()
    sys_proxy = attrs.pop("_sys_proxy", MagicMock())
    suanpan = attrs.pop("_suanpan", MagicMock(running=False, error=""))

    defaults = {
        "_config": config,
        "_conn": conn,
        "_lifecycle": svc,
        "_suanpan": suanpan,
        "_capture_ctrl": capture_ctrl,
        "_capture": monitor,
        "_sys_proxy": sys_proxy,
        "_config_server": MagicMock(),
        "_caffeinate_on": False,
        "_last_struct_key": None,
        "_menu_builder": MagicMock(),
    }
    defaults.update(attrs)
    for k, v in defaults.items():
        setattr(inst, k, v)
    return inst


class TestDefaultConfigCaptureFields(unittest.TestCase):
    def test_default_config_has_capture_fields(self):
        self.assertIn("capture_port", app.DEFAULT_CONFIG)
        self.assertIn("capture_dir", app.DEFAULT_CONFIG)
        self.assertIn("retention_days", app.DEFAULT_CONFIG)
        self.assertEqual(app.DEFAULT_CONFIG["capture_port"], capture.DEFAULT_CAPTURE_PORT)
        self.assertEqual(app.DEFAULT_CONFIG["capture_dir"], capture.DEFAULT_CAPTURE_DIR)

    def test_capture_enabled_never_persisted(self):
        """Hard constraint 1: capture on/off is never read from config --
        the key must not even exist in the schema."""
        self.assertNotIn("capture_enabled", app.DEFAULT_CONFIG)

    def test_merge_config_carries_capture_fields_with_defaults(self):
        merged = app.merge_config({})
        self.assertEqual(merged["capture_port"], capture.DEFAULT_CAPTURE_PORT)
        self.assertEqual(merged["capture_dir"], capture.DEFAULT_CAPTURE_DIR)
        self.assertEqual(merged["retention_days"], 7)


class TestSystemProxyTargetPort(unittest.TestCase):
    """SystemProxyController._target_port: capture running -> capture_port, else http_listen_port."""
    def _ctrl(self, cap_en=False, cap_st="stopped", port=8888, cap_port=8080):
        from sysctl.sys_proxy_controller import SystemProxyController
        return SystemProxyController(
            ssh_monitor=MagicMock(), capture_state=lambda: (cap_en, cap_st),
            config_fn=lambda: {"http_listen_port": port, "capture_port": cap_port},
            paused_fn=lambda: False)

    def test_capture_disabled_returns_http_listen_port(self):
        self.assertEqual(self._ctrl(cap_en=False, cap_st="running")._target_port(), 8888)

    def test_capture_starting_stays_on_http_listen(self):
        self.assertEqual(self._ctrl(cap_en=True, cap_st="starting")._target_port(), 8888)

    def test_capture_running_returns_capture_port(self):
        self.assertEqual(self._ctrl(cap_en=True, cap_st="running")._target_port(), 8080)

    def test_capture_errored_falls_back(self):
        self.assertEqual(self._ctrl(cap_en=True, cap_st="error")._target_port(), 8888)

    def test_invalid_http_listen_port_returns_none(self):
        self.assertIsNone(self._ctrl(port="not-a-port")._target_port())


class TestSystemProxyTargetHost(unittest.TestCase):
    """SystemProxyController._target_host always returns 127.0.0.1 (http proxy
    is loopback-only; the host field is gone from the config)."""
    def _ctrl(self, cap_en=False, cap_st="stopped"):
        from sysctl.sys_proxy_controller import SystemProxyController
        return SystemProxyController(
            ssh_monitor=MagicMock(), capture_state=lambda: (cap_en, cap_st),
            config_fn=lambda: {"http_listen_port": 8888, "capture_port": 8080},
            paused_fn=lambda: False)

    def test_capture_disabled_returns_loopback(self):
        self.assertEqual(self._ctrl(cap_en=False, cap_st="running")._target_host(), "127.0.0.1")

    def test_capture_running_returns_loopback(self):
        self.assertEqual(self._ctrl(cap_en=True, cap_st="running")._target_host(), "127.0.0.1")

    def test_capture_errored_returns_loopback(self):
        self.assertEqual(self._ctrl(cap_en=True, cap_st="error")._target_host(), "127.0.0.1")


class TestSystemProxySyncAppliesCorrectHost(unittest.TestCase):
    """SystemProxyController.sync passes resolved host into system_proxy.apply_transaction."""
    def test_capture_off_applies_loopback_http_port(self):
        from sysctl.sys_proxy_controller import SystemProxyController
        ctrl = SystemProxyController(
            ssh_monitor=MagicMock(status="connected"),
            capture_state=lambda: (False, "stopped"),
            config_fn=lambda: {"http_listen_port": 8888, "capture_port": 8080},
            paused_fn=lambda: False, initial_on=True)
        original = {"Wi-Fi": {}}
        desired = {"Wi-Fi": {}}
        with patch("sysctl.system_proxy.recover_stale_transaction", return_value=(True, "")),              patch("sysctl.system_proxy.snapshot", return_value=original),              patch("sysctl.system_proxy.apply_transaction", return_value=(True, "", desired)) as apply:
            ctrl.sync()
        apply.assert_called_once_with("127.0.0.1", 8888, system_proxy.DEFAULT_BYPASS, original)

    def test_capture_on_applies_loopback_capture_port(self):
        from sysctl.sys_proxy_controller import SystemProxyController
        ctrl = SystemProxyController(
            ssh_monitor=MagicMock(status="connected"),
            capture_state=lambda: (True, "running"),
            config_fn=lambda: {"http_listen_port": 8888, "capture_port": 8080},
            paused_fn=lambda: False, initial_on=True)
        original = {"Wi-Fi": {}}
        desired = {"Wi-Fi": {}}
        with patch("sysctl.system_proxy.recover_stale_transaction", return_value=(True, "")),              patch("sysctl.system_proxy.snapshot", return_value=original),              patch("sysctl.system_proxy.apply_transaction", return_value=(True, "", desired)) as apply:
            ctrl.sync()
        apply.assert_called_once_with("127.0.0.1", 8080, system_proxy.DEFAULT_BYPASS, original)


class TestToggleCapture(unittest.TestCase):
    def test_toggle_off_does_not_check_ca_trust(self):
        inst = _new_app(_capture_enabled=True)
        inst._capture.status = "running"
        with patch("capture.ca_trust.is_trusted") as is_trusted:
            inst.toggle_capture(None)
        is_trusted.assert_not_called()
        self.assertFalse(inst._capture_ctrl.enabled)
        inst._capture.stop.assert_called_once()

    def test_toggle_off_does_not_check_port(self):
        inst = _new_app(_capture_enabled=True)
        inst._capture.status = "running"
        with patch.object(app.MagicProxyApp, "_check_port") as check_port:
            inst.toggle_capture(None)
        check_port.assert_not_called()

    def test_toggle_on_checks_capture_port_before_ca_trust(self):
        # AC-2(b): fail fast on a local/technical issue (port) before the
        # CA-trust UX flow (which may pop a modal guide window).
        inst = _new_app(_capture_enabled=False)
        inst._capture.status = "stopped"
        with patch.object(app.MagicProxyApp, "_check_port", return_value=True) as check_port, \
             patch("capture.ca_trust.is_trusted", return_value=True), \
             patch.object(capture_controller, "resolve_capture_resources",
                         return_value=_stub_resources()):
            inst.toggle_capture(None)
        check_port.assert_called_once_with(8080, "抓包")
        self.assertTrue(inst._capture_ctrl.enabled)

    def test_toggle_on_aborts_when_port_occupied_and_user_declines(self):
        inst = _new_app(_capture_enabled=False)
        inst._capture.status = "stopped"
        with patch.object(app.MagicProxyApp, "_check_port", return_value=False), \
             patch("capture.ca_trust.is_trusted") as is_trusted:
            inst.toggle_capture(None)
        is_trusted.assert_not_called()
        self.assertFalse(inst._capture_ctrl.enabled)
        inst._capture.start.assert_not_called()

    def test_toggle_on_when_already_trusted_enables_directly(self):
        inst = _new_app(_capture_enabled=False)
        inst._capture.status = "stopped"
        with patch.object(app.MagicProxyApp, "_check_port", return_value=True), \
             patch("capture.ca_trust.is_trusted", return_value=True), \
             patch("capture.ca_trust.show_ca_trust_guide") as guide, \
             patch.object(capture_controller, "resolve_capture_resources",
                         return_value=_stub_resources()):
            inst.toggle_capture(None)
        guide.assert_not_called()
        self.assertTrue(inst._capture_ctrl.enabled)
        inst._capture.start.assert_called_once()

    def test_toggle_on_when_not_trusted_shows_guide_and_waits_for_result(self):
        inst = _new_app(_capture_enabled=False)
        inst._capture.status = "stopped"
        with patch.object(app.MagicProxyApp, "_check_port", return_value=True), \
             patch("capture.ca_trust.is_trusted", return_value=False), \
             patch("capture.ca_trust.show_ca_trust_guide") as guide:
            inst.toggle_capture(None)
        guide.assert_called_once()
        # Must not flip on until the guide reports success via its callback.
        self.assertFalse(inst._capture_ctrl.enabled)
        inst._capture.start.assert_not_called()

    def test_guide_result_true_enables_capture(self):
        inst = _new_app(_capture_enabled=False)
        inst._capture.status = "stopped"
        captured_cb = {}

        def fake_guide(on_result=None):
            captured_cb["cb"] = on_result

        with patch.object(app.MagicProxyApp, "_check_port", return_value=True), \
             patch("capture.ca_trust.is_trusted", return_value=False), \
             patch("capture.ca_trust.show_ca_trust_guide", side_effect=fake_guide), \
             patch.object(capture_controller, "resolve_capture_resources",
                         return_value=_stub_resources()):
            inst.toggle_capture(None)
            captured_cb["cb"](True)
        self.assertTrue(inst._capture_ctrl.enabled)
        inst._capture.start.assert_called_once()

    def test_guide_result_false_leaves_capture_off(self):
        inst = _new_app(_capture_enabled=False)
        inst._capture.status = "stopped"
        captured_cb = {}

        def fake_guide(on_result=None):
            captured_cb["cb"] = on_result

        with patch.object(app.MagicProxyApp, "_check_port", return_value=True), \
             patch("capture.ca_trust.is_trusted", return_value=False), \
             patch("capture.ca_trust.show_ca_trust_guide", side_effect=fake_guide):
            inst.toggle_capture(None)
            captured_cb["cb"](False)
        self.assertFalse(inst._capture_ctrl.enabled)
        inst._capture.start.assert_not_called()


class TestCaptureDirMenuActions(unittest.TestCase):
    def test_open_capture_dir_creates_dir_and_opens_it(self):
        inst = _new_app()
        with patch("os.makedirs") as makedirs, \
             patch("subprocess.Popen") as popen:
            inst.open_capture_dir(None)
        makedirs.assert_called_once_with(capture.DEFAULT_CAPTURE_DIR, exist_ok=True)
        popen.assert_called_once_with(["open", capture.DEFAULT_CAPTURE_DIR])

    def test_open_today_jsonl_opens_file_when_present(self):
        inst = _new_app()
        today_path = os.path.join(capture.DEFAULT_CAPTURE_DIR, "2026-07-08.jsonl")
        with patch("os.makedirs"), \
             patch("time.strftime", return_value="2026-07-08"), \
             patch("os.path.exists", return_value=True), \
             patch("subprocess.Popen") as popen:
            inst.open_today_jsonl(None)
        popen.assert_called_once_with(["open", "-t", today_path])

    def test_open_today_jsonl_falls_back_to_dir_when_file_missing(self):
        inst = _new_app()
        with patch("os.makedirs"), \
             patch("time.strftime", return_value="2026-07-08"), \
             patch("os.path.exists", return_value=False), \
             patch("subprocess.Popen") as popen:
            inst.open_today_jsonl(None)
        popen.assert_called_once_with(["open", capture.DEFAULT_CAPTURE_DIR])


class TestQuitDualCleanup(unittest.TestCase):
    def test_quit_stops_via_coordinators(self):
        inst = _new_app()
        with patch("rumps.quit_application"):
            inst.quit_app(None)
        # 顺序契约（sys_proxy→ssh→服务线→config_server）在
        # test_lifecycle_runtime.TestQuitOrder 钉死；此处断言 app 侧 seam。
        inst._lifecycle.quit.assert_called_once_with(inst._conn.stop_all)


class TestStructKeyIncludesCaptureState(unittest.TestCase):
    def test_struct_key_changes_when_capture_status_changes(self):
        from shellui.menu_builder import MenuBuilder
        inst = _new_app(_capture_enabled=True)
        inst._conn.ssh = MagicMock(status="stopped", error_msg="", log="")
        inst._conn.paused = False
        inst._config = {"tunnels": []}
        inst._stats = MagicMock()
        inst._stats.snapshot.return_value = {"active_connections": 0}
        mb = MenuBuilder(inst, inst._make_menu_state)

        inst._capture.status = "starting"
        key1 = mb.struct_key()
        inst._capture.status = "running"
        key2 = mb.struct_key()
        self.assertNotEqual(key1, key2)


if __name__ == "__main__":
    unittest.main()
