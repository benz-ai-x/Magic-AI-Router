"""Tests for capture_controller.py — 抓包模式 state machine + menu labels.

Migrated from test_capture_integration.py: the logic moved from app.py private
methods (_sync_capture / _capture_menu_title / _capture_error_hint /
_resolve_mitmdump_bin) onto CaptureController's interface, so these now cross
the controller's seam directly — no rumps/app scaffolding needed.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

from capture import capture
from capture import capture_controller
from capture.capture_controller import CaptureController
from capture import resources as capture_resources


_RES = None  # 模块级 fixture：已验证资源三元组（S2 新缝的桩）

def _res():
    from capture.resources import CaptureResources
    return CaptureResources("/bin/mitmdump", "/repo/ai_capture_addon.py",
                            capture.DEFAULT_CAPTURE_DIR)

def _ctrl(status="stopped", error_msg="", config=None):
    """Build a controller over a mock monitor for direct testing."""
    monitor = MagicMock()
    monitor.status = status
    monitor.error_msg = error_msg
    cfg = config or {"http_listen_port": 8888, "capture_port": 8080,
                     "capture_dir": capture.DEFAULT_CAPTURE_DIR}
    return CaptureController(monitor, config_fn=lambda: cfg)


class TestResolveMitmdumpBin(unittest.TestCase):
    def test_env_override_wins(self):
        with patch.dict(os.environ, {"MAGIC_PROXY_MITMDUMP_BIN": "/custom/mitmdump"}):
            self.assertEqual(capture_resources.resolve_mitmdump_bin(), "/custom/mitmdump")

    def test_frozen_uses_bundled_path_when_present(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("sys._MEIPASS", "/App/Contents/Resources", create=True), \
             patch("os.path.exists", return_value=True):
            self.assertTrue(capture_resources.resolve_mitmdump_bin().endswith("mitmdump/mitmdump"))

    def test_frozen_returns_none_when_bundled_binary_missing(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("sys._MEIPASS", "/App/Contents/Resources", create=True), \
             patch("os.path.exists", return_value=False):
            self.assertIsNone(capture_resources.resolve_mitmdump_bin())

    def test_dev_mode_uses_path_lookup(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("shutil.which", return_value="/opt/homebrew/bin/mitmdump"):
            had = hasattr(sys, "_MEIPASS")
            saved = getattr(sys, "_MEIPASS", None)
            if had:
                del sys._MEIPASS
            try:
                self.assertEqual(capture_resources.resolve_mitmdump_bin(), "/opt/homebrew/bin/mitmdump")
            finally:
                if had:
                    sys._MEIPASS = saved


class TestFreshController(unittest.TestCase):
    def test_fresh_controller_is_disabled(self):
        self.assertFalse(_ctrl().enabled)


class TestEnableDisable(unittest.TestCase):
    def test_enable_starts_mitmdump_with_correct_args(self):
        ctrl = _ctrl(status="stopped")
        with patch.object(capture_controller, "resolve_capture_resources",
                          return_value=_res()):
            self.assertTrue(ctrl.enable())
        kwargs = ctrl._monitor.start.call_args.kwargs
        self.assertEqual(kwargs["mitmdump_bin"], "/bin/mitmdump")
        self.assertEqual(kwargs["addon_path"], "/repo/ai_capture_addon.py")
        self.assertEqual(kwargs["capture_port"], 8080)
        self.assertEqual(kwargs["upstream"], "http://127.0.0.1:8888")
        self.assertEqual(kwargs["capture_dir"], capture.DEFAULT_CAPTURE_DIR)

    def test_enable_passes_configured_retention_days(self):
        cfg = {"http_listen_port": 8888, "capture_port": 8080,
               "capture_dir": capture.DEFAULT_CAPTURE_DIR, "retention_days": 14}
        ctrl = _ctrl(status="stopped", config=cfg)
        with patch.object(capture_controller, "resolve_capture_resources",
                          return_value=_res()):
            ctrl.enable()
        self.assertEqual(ctrl._monitor.start.call_args.kwargs["retention_days"], 14)

    def test_enable_defaults_retention_days_to_7(self):
        ctrl = _ctrl(status="stopped")
        with patch.object(capture_controller, "resolve_capture_resources",
                          return_value=_res()):
            ctrl.enable()
        self.assertEqual(ctrl._monitor.start.call_args.kwargs["retention_days"], 7)

    def test_enable_while_running_is_noop(self):
        ctrl = _ctrl(status="running")
        ctrl.enable()
        ctrl._monitor.start.assert_not_called()
        self.assertTrue(ctrl.enabled)

    def test_disable_stops_mitmdump(self):
        ctrl = _ctrl(status="running")
        ctrl.disable()
        ctrl._monitor.stop.assert_called_once()
        self.assertFalse(ctrl.enabled)

    def test_disable_when_stopped_is_noop(self):
        ctrl = _ctrl(status="stopped")
        ctrl.disable()
        ctrl._monitor.stop.assert_not_called()

    def test_enable_returns_false_when_bin_missing(self):
        ctrl = _ctrl(status="stopped")
        from capture.resources import CaptureResourcesError
        with patch.object(capture_controller, "resolve_capture_resources",
                          side_effect=CaptureResourcesError("未找到 mitmdump 可执行文件")):
            self.assertFalse(ctrl.enable())
        ctrl._monitor.start.assert_not_called()

    def test_enable_returns_false_when_start_fails(self):
        """#40: monitor.start() returning False (subprocess failed to spawn)
        must propagate — the caller shows the error UI on False."""
        ctrl = _ctrl(status="stopped")
        ctrl._monitor.start.return_value = False
        with patch.object(capture_controller, "resolve_capture_resources",
                          return_value=_res()):
            self.assertFalse(ctrl.enable())
        ctrl._monitor.start.assert_called_once()
        self.assertFalse(ctrl.enabled)
        self.assertFalse(ctrl.enabled)


class TestMenuTitle(unittest.TestCase):
    def test_running_shows_on(self):
        ctrl = _ctrl(status="running"); ctrl._enabled = True
        self.assertEqual(ctrl.menu_title(), "抓包模式：开")

    def test_starting_shows_transitional(self):
        ctrl = _ctrl(status="starting"); ctrl._enabled = True
        self.assertEqual(ctrl.menu_title(), "抓包模式：启动中…")

    def test_error_shows_warning_even_if_enabled(self):
        ctrl = _ctrl(status="error"); ctrl._enabled = True
        self.assertEqual(ctrl.menu_title(), "抓包模式：异常")

    def test_off_and_ca_trusted_shows_plain_off(self):
        with patch("capture.ca_trust.is_trusted", return_value=True):
            self.assertEqual(_ctrl(status="stopped").menu_title(), "抓包模式：关")

    def test_off_and_ca_not_trusted_shows_hint(self):
        with patch("capture.ca_trust.is_trusted", return_value=False):
            self.assertEqual(_ctrl(status="stopped").menu_title(), "抓包模式：关（需信任证书）")

    def test_does_not_check_ca_trust_while_enabled(self):
        ctrl = _ctrl(status="running"); ctrl._enabled = True
        with patch("capture.ca_trust.is_trusted") as is_trusted:
            ctrl.menu_title()
        is_trusted.assert_not_called()


class TestTrustCaching(unittest.TestCase):
    """menu_title() runs once per UI tick — the CA-trust subprocess check
    behind it must be TTL-cached so idle state doesn't spawn `security
    verify-cert` every second."""

    def test_repeated_menu_title_checks_trust_once(self):
        ctrl = _ctrl(status="stopped")
        with patch("capture.ca_trust.is_trusted", return_value=False) as is_trusted:
            ctrl.menu_title()
            ctrl.menu_title()
            ctrl.menu_title()
        self.assertEqual(is_trusted.call_count, 1)

    def test_cache_expires_after_ttl(self):
        ctrl = _ctrl(status="stopped")
        with patch("capture.ca_trust.is_trusted", return_value=False) as is_trusted, \
             patch.object(capture_controller.time, "monotonic") as mono:
            mono.return_value = 1000.0
            ctrl.menu_title()
            mono.return_value = 1000.0 + capture_controller.TRUST_CACHE_TTL + 1
            ctrl.menu_title()
        self.assertEqual(is_trusted.call_count, 2)

    def test_trust_result_change_reflected_after_expiry(self):
        ctrl = _ctrl(status="stopped")
        with patch("capture.ca_trust.is_trusted", side_effect=[False, True]) as is_trusted, \
             patch.object(capture_controller.time, "monotonic") as mono:
            mono.return_value = 0.0
            self.assertEqual(ctrl.menu_title(), "抓包模式：关（需信任证书）")
            mono.return_value = capture_controller.TRUST_CACHE_TTL + 1
            self.assertEqual(ctrl.menu_title(), "抓包模式：关")
        self.assertEqual(is_trusted.call_count, 2)

    def test_enable_invalidates_cache(self):
        ctrl = _ctrl(status="stopped")
        with patch("capture.ca_trust.is_trusted", return_value=False) as is_trusted, \
             patch.object(capture_controller, "resolve_capture_resources",
                          return_value=_res()):
            ctrl.menu_title()  # populates cache
            ctrl.enable()
            ctrl._enabled = False
            ctrl._monitor.status = "stopped"
            ctrl.menu_title()  # must re-check, not reuse pre-enable cache
        self.assertEqual(is_trusted.call_count, 2)


class TestErrorHint(unittest.TestCase):
    def test_error_with_message_returns_hint(self):
        ctrl = _ctrl(status="error", error_msg="mitmdump exited with code 1")
        hint = ctrl.error_hint()
        self.assertIsNotNone(hint)
        self.assertIn("mitmdump exited with code 1", hint)

    def test_non_error_returns_none(self):
        self.assertIsNone(_ctrl(status="stopped", error_msg="").error_hint())

    def test_error_empty_message_returns_none(self):
        self.assertIsNone(_ctrl(status="error", error_msg="").error_hint())

    def test_long_message_truncated(self):
        ctrl = _ctrl(status="error", error_msg="x" * 200)
        self.assertLessEqual(len(ctrl.error_hint()), 90)


if __name__ == "__main__":
    unittest.main()


class TestEnableConsumesResourceContract(unittest.TestCase):
    """Seam S2（issue #2）：enable 只消费已验证的 CaptureResources；
    preflight 失败不进 enabled 且错误直达菜单文案。"""

    def test_preflight_failure_keeps_disabled_and_surfaces_actionable_error(self):
        from capture.resources import CaptureResourcesError
        c = _ctrl()
        with patch("capture.capture_controller.resolve_capture_resources",
                   side_effect=CaptureResourcesError("未找到 mitmdump 可执行文件")):
            ok = c.enable()
        self.assertFalse(ok)
        self.assertFalse(c.enabled)
        self.assertIn("mitmdump", c.error_msg)
        c._monitor.start.assert_not_called()

    def test_enable_passes_validated_resources_to_monitor(self):
        from capture.resources import CaptureResources
        c = _ctrl()
        c._monitor.start.return_value = True
        res = CaptureResources("/usr/bin/true", "/x/ai_capture_addon.py", "/tmp/cap")
        with patch("capture.capture_controller.resolve_capture_resources",
                   return_value=res):
            self.assertTrue(c.enable())
        kw = c._monitor.start.call_args.kwargs
        self.assertEqual(kw["mitmdump_bin"], "/usr/bin/true")
        self.assertEqual(kw["addon_path"], "/x/ai_capture_addon.py")
        self.assertEqual(kw["capture_dir"], "/tmp/cap")
        self.assertEqual(c.error_msg, "")  # 预检错误已清除，回落 monitor 文案
