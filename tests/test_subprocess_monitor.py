"""Tests for subprocess_monitor.py — base class lifecycle.

Seams under test (confirmed):
- set_status / set_error / snapshot_log_lines: public mutators
- properties: status, error_msg, cmd_str, log, elapsed
- stop(blocking=True) / stop(blocking=False): process termination
- check(port): crash detection + ready probe dispatch
- _start_process: Popen + stderr thread launch
"""
import subprocess
import time
import unittest
from unittest.mock import MagicMock, patch

from tunnel.subprocess_monitor import SubprocessMonitor


class _FakeMonitor(SubprocessMonitor):
    """Minimal concrete subclass for testing the base class."""
    _PROCESS_NAME = "test-proc"

    def _probe_ready(self, port):
        return True


class TestPublicMutators(unittest.TestCase):
    def test_set_status(self):
        m = _FakeMonitor()
        m.set_status("custom")
        self.assertEqual(m.status, "custom")

    def test_set_error(self):
        m = _FakeMonitor()
        m.set_error("something broke")
        self.assertEqual(m.error_msg, "something broke")

    def test_snapshot_log_lines_empty(self):
        m = _FakeMonitor()
        self.assertEqual(m.snapshot_log_lines(), [])

    def test_snapshot_log_lines_returns_copy(self):
        m = _FakeMonitor()
        with m._log_lock:
            m._log_lines.append("line1")
            m._log_lines.append("line2")
        snapshot = m.snapshot_log_lines()
        self.assertEqual(snapshot, ["line1", "line2"])
        # Mutating snapshot doesn't affect internal state
        snapshot.append("line3")
        self.assertEqual(len(m.snapshot_log_lines()), 2)

    def test_line_sink_defaults_to_none(self):
        self.assertIsNone(_FakeMonitor()._line_sink)

    def test_line_sink_forwards_each_stderr_line(self):
        """Sink forwards each decoded line as read (blank lines skipped)."""
        forwarded = []
        m = _FakeMonitor(line_sink=forwarded.append)

        class _FakeProc:
            stderr = iter([b"line one\n", b"\n", b"line two\n"])

        m._read_stderr(_FakeProc())

        self.assertEqual(forwarded, ["line one", "line two"])
        self.assertEqual(m.snapshot_log_lines(), ["line one", "line two"])


class TestProperties(unittest.TestCase):
    def test_initial_state(self):
        m = _FakeMonitor()
        self.assertEqual(m.status, "stopped")
        self.assertEqual(m.error_msg, "")
        self.assertEqual(m.cmd_str, "")
        self.assertEqual(m.log, "")
        self.assertEqual(m.elapsed, 0)

    def test_cmd_str_set_by_start_process(self):
        m = _FakeMonitor()
        with patch("tunnel.subprocess_monitor.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(poll=lambda: None, stderr=iter([]))
            m._start_process(["echo", "hi"])
        self.assertEqual(m.cmd_str, "echo hi")

    def test_cmd_str_uses_display_cmd_when_provided(self):
        m = _FakeMonitor()
        with patch("tunnel.subprocess_monitor.subprocess.Popen") as mock_popen:
            mock_popen.return_value = MagicMock(poll=lambda: None, stderr=iter([]))
            m._start_process(["secret-cmd"], display_cmd="safe-cmd")
        self.assertEqual(m.cmd_str, "safe-cmd")

    def test_log_returns_last_5_lines(self):
        m = _FakeMonitor()
        with m._log_lock:
            for i in range(10):
                m._log_lines.append(f"line{i}")
        self.assertEqual(m.log, "line5\nline6\nline7\nline8\nline9")

    def test_elapsed_zero_when_stopped(self):
        m = _FakeMonitor()
        m.set_status("stopped")
        m._start_time = time.monotonic()
        self.assertEqual(m.elapsed, 0)

    def test_elapsed_positive_when_starting(self):
        m = _FakeMonitor()
        m._start_time = time.monotonic() - 5
        m.set_status(m._STATUS_STARTING)
        self.assertGreaterEqual(m.elapsed, 4)


class TestStartProcess(unittest.TestCase):
    def test_popen_failure_sets_error(self):
        m = _FakeMonitor()
        with patch("tunnel.subprocess_monitor.subprocess.Popen", side_effect=OSError("no such file")):
            ok = m._start_process(["nonexistent-binary"])
        self.assertFalse(ok)
        self.assertEqual(m.status, "error")
        self.assertIn("no such file", m.error_msg)

    def test_successful_start_sets_starting(self):
        m = _FakeMonitor()
        mock_proc = MagicMock(poll=lambda: None, stderr=iter([]))
        with patch("tunnel.subprocess_monitor.subprocess.Popen", return_value=mock_proc):
            ok = m._start_process(["echo", "hi"])
        self.assertTrue(ok)
        self.assertEqual(m.status, "starting")
        self.assertIs(m.process, mock_proc)

    def test_clears_log_on_start(self):
        m = _FakeMonitor()
        with m._log_lock:
            m._log_lines.append("old")
        mock_proc = MagicMock(poll=lambda: None, stderr=iter([]))
        with patch("tunnel.subprocess_monitor.subprocess.Popen", return_value=mock_proc):
            m._start_process(["echo"])
        self.assertEqual(m.snapshot_log_lines(), [])


class TestStop(unittest.TestCase):
    def test_stop_with_no_process_sets_stopped(self):
        m = _FakeMonitor()
        m.process = None
        m.stop()
        self.assertEqual(m.status, "stopped")

    def test_blocking_stop_terminates_and_waits(self):
        m = _FakeMonitor()
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        m.process = mock_proc
        m._log_thread = MagicMock(is_alive=lambda: False)
        m.stop(blocking=True)
        mock_proc.terminate.assert_called_once()
        mock_proc.wait.assert_called()
        self.assertIsNone(m.process)
        self.assertEqual(m.status, "stopped")

    def test_non_blocking_stop_returns_immediately(self):
        m = _FakeMonitor()
        mock_proc = MagicMock()
        mock_proc.wait.return_value = 0
        m.process = mock_proc
        m._log_thread = MagicMock(is_alive=lambda: False)
        m.stop(blocking=False)
        mock_proc.terminate.assert_called_once()
        self.assertIsNone(m.process)
        self.assertEqual(m.status, "stopped")

    def test_blocking_stop_kills_on_timeout(self):
        m = _FakeMonitor()
        mock_proc = MagicMock(
            wait=MagicMock(side_effect=[subprocess.TimeoutExpired(cmd="x", timeout=5), 0]),
            terminate=MagicMock(),
            kill=MagicMock(),
        )
        m.process = mock_proc
        m._log_thread = MagicMock(is_alive=lambda: False)
        m.stop(blocking=True)
        mock_proc.kill.assert_called_once()


class TestCheck(unittest.TestCase):
    def test_no_process_returns_silently(self):
        m = _FakeMonitor()
        m.process = None
        m.check(8080)
        # No crash, status unchanged

    def test_crashed_process_sets_error(self):
        m = _FakeMonitor()
        mock_proc = MagicMock(poll=lambda: 1)
        m.process = mock_proc
        m._log_thread = MagicMock(is_alive=lambda: False)
        with m._log_lock:
            m._log_lines.append("fatal error")
        m.check(8080)
        self.assertEqual(m.status, "error")
        self.assertIn("fatal error", m.error_msg)
        self.assertIsNone(m.process)

    def test_crashed_no_logs_uses_exit_code(self):
        m = _FakeMonitor()
        mock_proc = MagicMock(poll=lambda: 42)
        m.process = mock_proc
        m._log_thread = MagicMock(is_alive=lambda: False)
        m.check(8080)
        self.assertEqual(m.status, "error")
        self.assertIn("42", m.error_msg)

    def test_probe_only_while_starting(self):
        m = _FakeMonitor()
        mock_proc = MagicMock(poll=lambda: None)
        m.process = mock_proc
        m.set_status("running")  # already running, should not probe
        probe_called = []
        original_probe = m._probe_ready
        def tracking_probe(port):
            probe_called.append(port)
            return original_probe(port)
        m._probe_ready = tracking_probe
        m.check(8080)
        self.assertEqual(probe_called, [])  # probe NOT called when already running

    def test_probe_transitions_to_running(self):
        m = _FakeMonitor()
        mock_proc = MagicMock(poll=lambda: None)
        m.process = mock_proc
        m.set_status("starting")
        m._probe_ready = lambda port: True
        m.check(8080)
        self.assertEqual(m.status, "running")

    def test_failed_probe_stays_starting(self):
        m = _FakeMonitor()
        mock_proc = MagicMock(poll=lambda: None)
        m.process = mock_proc
        m.set_status("starting")
        m._probe_ready = lambda port: False
        m.check(8080)
        self.assertEqual(m.status, "starting")


if __name__ == "__main__":
    unittest.main()
