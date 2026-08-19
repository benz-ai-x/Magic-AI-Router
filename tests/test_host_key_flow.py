"""Tests for host_key_flow.py — HostKeyFlow state machine.

Tests the decision logic directly via _finish_check / _finish_replacement,
bypassing the background thread + AppHelper.callAfter layer. rumps.alert
and host_key functions are mocked.
"""
import unittest
from unittest.mock import MagicMock, patch

from host_key_flow import HostKeyFlow


def _make_flow(get_tunnel=None, on_connect=None, on_reconnect=None):
    return HostKeyFlow(
        ssh_monitor=MagicMock(),
        get_tunnel=get_tunnel or (lambda: {"ssh_host": "h", "ssh_port": 22}),
        get_socks5_port=lambda: 1080,
        get_password=lambda: "",
        on_connect=on_connect or MagicMock(),
        on_reconnect=on_reconnect or MagicMock(),
    )


class TestStartCheckNoTunnel(unittest.TestCase):
    def test_no_tunnel_is_noop(self):
        flow = _make_flow(get_tunnel=lambda: None)
        flow.start_check()
        self.assertFalse(flow.checking)
        flow._ssh.set_status.assert_not_called()


class TestStartCheckStateTransition(unittest.TestCase):
    @patch("host_key_flow.threading.Thread")
    def test_sets_checking_and_connecting(self, _mock_thread):
        flow = _make_flow()
        flow.start_check()
        self.assertTrue(flow.checking)
        flow._ssh.set_status.assert_called_once_with("connecting")


class TestBeginReplacement(unittest.TestCase):
    @patch("host_key_flow.threading.Thread")
    def test_no_tunnel_is_noop(self, _mock_thread):
        flow = _make_flow(get_tunnel=lambda: None)
        flow.begin_replacement()
        self.assertFalse(flow.change_prompted)

    @patch("host_key_flow.threading.Thread")
    def test_sets_change_prompted(self, _mock_thread):
        flow = _make_flow()
        flow.begin_replacement()
        self.assertTrue(flow.change_prompted)


class TestCancel(unittest.TestCase):
    def test_cancel_resets_flags(self):
        flow = _make_flow()
        flow.checking = True
        flow.change_prompted = True
        flow.cancel()
        self.assertFalse(flow.checking)
        self.assertFalse(flow.change_prompted)

    def test_cancel_invalidates_in_flight(self):
        flow = _make_flow()
        flow.start_check()  # generation = 1
        gen_before = flow._generation
        flow.cancel()       # generation = 2
        self.assertEqual(flow._generation, gen_before + 1)


class TestFinishCheck(unittest.TestCase):
    """Test _finish_check decision logic directly."""
    _tunnel = {"ssh_host": "srv", "ssh_port": 22}

    def test_known_key_calls_on_connect(self):
        flow = _make_flow()
        gen = flow._generation
        flow._finish_check(gen, self._tunnel, (True, "keys", "fps", None))
        flow._on_connect.assert_called_once()
        flow._ssh.set_status.assert_not_called()  # no error/stop

    @patch("host_key_flow.host_key.accept", return_value=True)
    @patch("host_key_flow.rumps.alert", return_value=1)
    def test_unknown_key_user_trusts(self, _alert, _accept):
        flow = _make_flow()
        gen = flow._generation
        flow._finish_check(gen, self._tunnel, (False, "keys", "fps", None))
        _accept.assert_called_once_with("keys")
        flow._on_connect.assert_called_once()

    @patch("host_key_flow.rumps.alert", return_value=0)
    def test_unknown_key_user_cancels(self, _alert):
        flow = _make_flow()
        gen = flow._generation
        flow._finish_check(gen, self._tunnel, (False, "keys", "fps", None))
        flow._ssh.set_status.assert_called_once_with("stopped")
        flow._on_connect.assert_not_called()

    @patch("host_key_flow.rumps.alert")
    def test_inspection_error_sets_error_status(self, mock_alert):
        flow = _make_flow()
        gen = flow._generation
        flow._finish_check(gen, self._tunnel, (False, None, None, "scan failed"))
        flow._ssh.set_status.assert_called_once_with("error")
        flow._ssh.set_error.assert_called_once_with("scan failed")
        flow._on_connect.assert_not_called()

    def test_stale_generation_is_noop(self):
        flow = _make_flow()
        flow._finish_check(999, self._tunnel, (True, "keys", "fps", None))
        flow._on_connect.assert_not_called()


class TestInspectThreadGuarded(unittest.TestCase):
    """The background inspect thread must survive exceptions (#40): an
    unhandled crash left SSH stuck in 'connecting' forever."""

    def test_inspect_exception_marks_error(self):
        flow = _make_flow()
        with patch("host_key_flow.host_key.inspect",
                   side_effect=RuntimeError("boom")), \
             patch("host_key_flow.AppHelper.callAfter",
                   side_effect=lambda fn, *a: fn(*a)), \
             patch("host_key_flow.rumps.alert"), \
             patch("host_key_flow.threading.Thread") as mock_thread:
            flow.start_check()
            mock_thread.assert_called_once()
            # Call inside the patch block: the thread target closes over the
            # module-level inspect/callAfter, which the patches replace.
            mock_thread.call_args[1]["target"]()
        flow._ssh.set_status.assert_any_call("error")
        flow._ssh.set_error.assert_any_call("RuntimeError: boom")
        flow._on_connect.assert_not_called()

    def test_inspect_success_dispatches_finish(self):
        """Happy path still hands the inspection result to the main thread."""
        flow = _make_flow()
        with patch("host_key_flow.host_key.inspect",
                   return_value=(True, "k", "f", None)), \
             patch("host_key_flow.AppHelper.callAfter") as call_after, \
             patch("host_key_flow.threading.Thread") as mock_thread:
            flow.start_check()
            mock_thread.call_args[1]["target"]()
        call_after.assert_called_once()
        self.assertEqual(call_after.call_args[0][0].__func__,
                         flow._finish_check.__func__)


class TestFinishReplacement(unittest.TestCase):
    """Test _finish_replacement decision logic directly."""
    _tunnel = {"ssh_host": "srv", "ssh_port": 22}

    @patch("host_key_flow.host_key.replace", return_value=True)
    @patch("host_key_flow.rumps.alert", return_value=1)
    def test_user_confirms_replaces_and_reconnects(self, _alert, _replace):
        flow = _make_flow()
        gen = flow._generation
        flow._finish_replacement(gen, self._tunnel, (False, "keys", "fps", None))
        _replace.assert_called_once_with(self._tunnel, "keys")
        flow._on_reconnect.assert_called_once()
        self.assertFalse(flow.change_prompted)

    @patch("host_key_flow.rumps.alert", return_value=0)
    def test_user_cancels_sets_stopped(self, _alert):
        flow = _make_flow()
        gen = flow._generation
        flow._finish_replacement(gen, self._tunnel, (False, "keys", "fps", None))
        flow._ssh.set_status.assert_called_once_with("stopped")
        flow._on_reconnect.assert_not_called()

    @patch("host_key_flow.rumps.alert")
    def test_scan_error_shows_alert_no_change(self, mock_alert):
        flow = _make_flow()
        gen = flow._generation
        flow._finish_replacement(gen, self._tunnel, (False, None, None, "scan error"))
        mock_alert.assert_called_once()
        flow._on_reconnect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
