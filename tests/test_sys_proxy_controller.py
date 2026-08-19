"""Tests for sys_proxy_controller.py — toggle, sync, quit_cleanup."""
import unittest
from unittest.mock import MagicMock, patch

from sysctl.sys_proxy_controller import SystemProxyController


def _make_ctrl(**overrides):
    ssh = MagicMock()
    ssh.status = "stopped"
    with patch("sysctl.sys_proxy_controller.system_proxy") as mock_sp:
        mock_sp.recover_stale_transaction.return_value = (False, "")
        ctrl = SystemProxyController(
            ssh_monitor=ssh,
            capture_state=lambda: (False, ""),
            config_fn=lambda: {"http_listen_port": 8888, "capture_port": 8080},
            paused_fn=lambda: False,
            on_dirty=lambda: None,
            initial_on=False,
        )
    # ctor params are stored underscore-prefixed (e.g. config_fn → _config_fn);
    # route overrides to the real attribute, not a stray public one.
    for k, v in overrides.items():
        attr = k if hasattr(ctrl, k) else f"_{k}"
        setattr(ctrl, attr, v)
    return ctrl


class TestProperties(unittest.TestCase):
    def test_on_false_on_init(self):
        ctrl = _make_ctrl()
        self.assertFalse(ctrl.on)

    def test_error_empty_on_init(self):
        ctrl = _make_ctrl()
        self.assertEqual(ctrl.error, "")


class TestToggle(unittest.TestCase):
    def test_toggle_flips_on(self):
        ctrl = _make_ctrl()
        with patch.object(ctrl, "sync"):
            ctrl.toggle()
        self.assertTrue(ctrl.on)

    def test_toggle_back_off(self):
        ctrl = _make_ctrl()
        ctrl._on = True
        with patch.object(ctrl, "sync"):
            ctrl.toggle()
        self.assertFalse(ctrl.on)


class TestTargetResolution(unittest.TestCase):
    def test_target_port_http_when_no_capture(self):
        ctrl = _make_ctrl()
        self.assertEqual(ctrl._target_port(), 8888)

    def test_target_port_capture_when_running(self):
        ssh = MagicMock()
        ssh.status = "stopped"
        ctrl = SystemProxyController(
            ssh_monitor=ssh,
            capture_state=lambda: (True, "running"),
            config_fn=lambda: {"http_listen_port": 8888, "capture_port": 8080},
            paused_fn=lambda: False,
        )
        self.assertEqual(ctrl._target_port(), 8080)

    def test_target_host_always_loopback(self):
        ctrl = _make_ctrl(config_fn=lambda: {"http_listen_port": 8888})
        self.assertEqual(ctrl._target_host(), "127.0.0.1")

    def test_target_port_invalid_returns_none(self):
        ctrl = _make_ctrl(config_fn=lambda: {"http_listen_port": "bad"})
        self.assertIsNone(ctrl._target_port())


class TestQuitCleanup(unittest.TestCase):
    @patch("sysctl.sys_proxy_controller.system_proxy")
    def test_quit_releases_and_resets(self, mock_sp):
        mock_sp.recover_stale_transaction.return_value = (False, "")
        mock_sp.release_transaction.return_value = (True, "")
        ctrl = _make_ctrl()
        ctrl._snapshot = {"webproxy": "test"}
        ctrl._desired = {"webproxy": "test"}
        ctrl.quit_cleanup()
        mock_sp.release_transaction.assert_called_once()
        self.assertIsNone(ctrl._snapshot)

    @patch("sysctl.sys_proxy_controller.system_proxy")
    def test_quit_no_snapshot_is_noop(self, mock_sp):
        mock_sp.recover_stale_transaction.return_value = (False, "")
        ctrl = _make_ctrl()
        ctrl.quit_cleanup()
        mock_sp.release_transaction.assert_not_called()


class TestApplyFailureExposesOffState(unittest.TestCase):
    """Regression: when apply_transaction fails with a non-None desired_state,
    the controller silently sets _on=False. The error message must now surface
    that the proxy was disabled."""

    @patch("sysctl.sys_proxy_controller.system_proxy")
    def test_apply_failure_with_desired_state_mentions_off(self, mock_sp):
        mock_sp.recover_stale_transaction.return_value = (False, "")
        mock_sp.snapshot.return_value = {"webproxy": "old"}
        # apply returns (ok=False, err, desired_state=not-None)
        mock_sp.apply_transaction.return_value = (
            False, "networksetup failed", {"webproxy": "desired"})
        ssh = MagicMock()
        ssh.status = "connected"
        ctrl = SystemProxyController(
            ssh_monitor=ssh,
            capture_state=lambda: (False, ""),
            config_fn=lambda: {"http_listen_port": 8888, "capture_port": 8080},
            paused_fn=lambda: False,
            initial_on=True,
        )
        ctrl.sync()
        self.assertFalse(ctrl.on)
        self.assertIn("turned off", ctrl.error)
        self.assertIn("networksetup failed", ctrl.error)
