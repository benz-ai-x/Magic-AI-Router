"""Tests for capture module (mitmdump subprocess manager, ADR-022 Task 2).

CaptureMonitor intentionally mirrors proxy.py's SSHMonitor lifecycle pattern
(start/stop/status/log/crash-detection), so this suite mixes two styles:
  - mocked subprocess.Popen for pure logic (cmd construction, env contract,
    stop/status transitions) -- consistent with this repo's existing
    subprocess-boundary mocking convention (see test_port_check.py).
  - real Popen against tiny throwaway stub scripts for the parts that are
    hard to trust via mocking alone: the background stderr-reader thread,
    real crash detection, and real process termination.
"""
import os
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

from capture import capture
@pytest.fixture(autouse=True)
def _avoid_real_capture_store(monkeypatch):
    monkeypatch.setattr(
        capture, "prepare_capture_dir",
        lambda path: os.path.abspath(os.path.expanduser(path)),
    )


def _free_port() -> int:
    """Reserve then release an ephemeral port (best-effort race, same trick
    used to hand a port to a spawned child in the real-subprocess tests)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _make_stub(body: str) -> str:
    """Write an executable python stub script; caller must os.remove() it."""
    fd, path = tempfile.mkstemp(suffix=".py")
    with os.fdopen(fd, "w") as f:
        f.write("#!/usr/bin/env python3\n")
        f.write(body)
    os.chmod(path, 0o755)
    return path


STUB_LISTEN = """\
import socket
import sys
import time

port = 0
for i, a in enumerate(sys.argv):
    if a == "--listen-port":
        port = int(sys.argv[i + 1])
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port))
s.listen(1)
time.sleep(30)
"""

STUB_CRASH = """\
import sys
print("fatal: addon import failed", file=sys.stderr)
sys.exit(1)
"""


def _mock_popen(poll_returns=None, stderr_lines=None):
    """Build a MagicMock standing in for subprocess.Popen(...)."""
    proc = MagicMock()
    proc.poll.side_effect = poll_returns if poll_returns is not None else [None]
    proc.stderr = iter(stderr_lines or [])
    proc.wait.return_value = 0
    return proc


class TestStartCmdConstruction(unittest.TestCase):
    def setUp(self):
        self.mon = capture.CaptureMonitor()

    def tearDown(self):
        self.mon.stop(blocking=False)

    def test_default_port_and_upstream(self):
        with patch("subprocess.Popen", return_value=_mock_popen()):
            self.mon.start(mitmdump_bin="mitmdump", addon_path="/x/addon.py")
        self.assertEqual(
            self.mon.cmd_str,
            "mitmdump --mode upstream:http://127.0.0.1:8888 --listen-host 127.0.0.1 --listen-port 8080 -s /x/addon.py",
        )

    def test_custom_port_and_upstream(self):
        with patch("subprocess.Popen", return_value=_mock_popen()):
            self.mon.start(
                mitmdump_bin="mitmdump",
                addon_path="/x/addon.py",
                capture_port=9090,
                upstream="http://127.0.0.1:9999",
            )
        self.assertEqual(
            self.mon.cmd_str,
            "mitmdump --mode upstream:http://127.0.0.1:9999 --listen-host 127.0.0.1 --listen-port 9090 -s /x/addon.py",
        )

    def test_capture_explicitly_binds_loopback(self):
        with patch("subprocess.Popen", return_value=_mock_popen()) as popen:
            self.mon.start(mitmdump_bin="mitmdump", addon_path="/x/addon.py")
        cmd = popen.call_args.args[0]
        self.assertEqual(cmd[cmd.index("--listen-host") + 1], "127.0.0.1")

    def test_start_sets_status_starting(self):
        with patch("subprocess.Popen", return_value=_mock_popen()):
            self.mon.start(mitmdump_bin="mitmdump", addon_path="/x/addon.py")
        self.assertEqual(self.mon.status, "starting")

    def test_start_clears_previous_error(self):
        with patch("subprocess.Popen", return_value=_mock_popen(poll_returns=[1])):
            self.mon.start(mitmdump_bin="mitmdump", addon_path="/x/addon.py")
            self.mon.check(8080)  # -> status "error"
        self.assertEqual(self.mon.status, "error")
        with patch("subprocess.Popen", return_value=_mock_popen()):
            self.mon.start(mitmdump_bin="mitmdump", addon_path="/x/addon.py")
        self.assertEqual(self.mon.status, "starting")
        self.assertEqual(self.mon.error_msg, "")


class TestEnvContract(unittest.TestCase):
    """Pins the one-way env-var contract product-lead specified for the
    addon (capture.py never talks back to the addon -- env injection only)."""

    def setUp(self):
        self.mon = capture.CaptureMonitor()

    def tearDown(self):
        self.mon.stop(blocking=False)

    def test_env_defaults(self):
        with patch("subprocess.Popen", return_value=_mock_popen()) as popen:
            self.mon.start(mitmdump_bin="mitmdump", addon_path="/x/addon.py")
        env = popen.call_args.kwargs["env"]
        self.assertEqual(
            env["MAGIC_PROXY_CAPTURE_DIR"], os.path.expanduser("~/.magic-proxy-captures")
        )
        self.assertEqual(env["MAGIC_PROXY_CAPTURE_RAW_SSE"], "0")
        self.assertEqual(env["MAGIC_PROXY_PRESERVE_STREAMING"], "0")

    def test_env_overrides(self):
        with patch("subprocess.Popen", return_value=_mock_popen()) as popen:
            self.mon.start(
                mitmdump_bin="mitmdump",
                addon_path="/x/addon.py",
                capture_dir="/tmp/custom-captures",
                raw_sse=True,
                preserve_streaming=True,
            )
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env["MAGIC_PROXY_CAPTURE_DIR"], "/tmp/custom-captures")
        self.assertEqual(env["MAGIC_PROXY_CAPTURE_RAW_SSE"], "1")
        self.assertEqual(env["MAGIC_PROXY_PRESERVE_STREAMING"], "1")

    def test_env_preserves_parent_environment(self):
        # Child must inherit the parent's env (e.g. PATH), not a stripped one.
        with patch.dict(os.environ, {"MAGIC_PROXY_TEST_MARKER": "present"}), \
             patch("subprocess.Popen", return_value=_mock_popen()) as popen:
            self.mon.start(mitmdump_bin="mitmdump", addon_path="/x/addon.py")
        env = popen.call_args.kwargs["env"]
        self.assertEqual(env.get("MAGIC_PROXY_TEST_MARKER"), "present")


class TestStopLifecycle(unittest.TestCase):
    def setUp(self):
        self.mon = capture.CaptureMonitor()

    def tearDown(self):
        self.mon.stop(blocking=False)

    def test_stop_on_never_started_is_noop(self):
        self.mon.stop()  # must not raise
        self.assertEqual(self.mon.status, "stopped")

    def test_stop_blocking_terminates_and_waits(self):
        proc = _mock_popen()
        with patch("subprocess.Popen", return_value=proc):
            self.mon.start(mitmdump_bin="mitmdump", addon_path="/x/addon.py")
        self.mon.stop()
        proc.terminate.assert_called_once()
        proc.wait.assert_called_once()
        self.assertIsNone(self.mon.process)
        self.assertEqual(self.mon.status, "stopped")

    def test_stop_kills_on_timeout(self):
        proc = _mock_popen()
        proc.wait.side_effect = subprocess.TimeoutExpired(cmd="mitmdump", timeout=5)
        with patch("subprocess.Popen", return_value=proc):
            self.mon.start(mitmdump_bin="mitmdump", addon_path="/x/addon.py")
        self.mon.stop()
        proc.kill.assert_called_once()
        self.assertEqual(self.mon.status, "stopped")

    def test_stop_nonblocking_schedules_reaper(self):
        proc = _mock_popen()
        with patch("subprocess.Popen", return_value=proc):
            self.mon.start(mitmdump_bin="mitmdump", addon_path="/x/addon.py")
        self.mon.stop(blocking=False)
        proc.terminate.assert_called_once()
        # The caller does not wait; a daemon reaper performs wait() so the
        # child cannot become a zombie. MagicMock returns immediately here.
        proc.wait.assert_called_with(timeout=5)
        self.assertIsNone(self.mon.process)
        self.assertEqual(self.mon.status, "stopped")

    def test_stop_after_crash_resets_status_to_stopped(self):
        # check() already nulls self.process when it detects a crash (see
        # TestCheckCrashDetection); stop() must still be able to bring a
        # monitor sitting in "error" back to a clean "stopped" state (e.g.
        # app.py calling stop() on toggle-off after a crash), not silently
        # no-op just because self.process happens to already be None.
        proc = _mock_popen(poll_returns=[1])
        with patch("subprocess.Popen", return_value=proc):
            self.mon.start(mitmdump_bin="mitmdump", addon_path="/x/addon.py")
        self.mon.check(8080)
        self.assertEqual(self.mon.status, "error")
        self.assertIsNone(self.mon.process)

        self.mon.stop()
        self.assertEqual(self.mon.status, "stopped")


class TestLogTail(unittest.TestCase):
    def test_log_returns_last_five_lines_only(self):
        mon = capture.CaptureMonitor()
        for i in range(8):
            with mon._log_lock:
                mon._log_lines.append(f"line{i}")
        self.assertEqual(mon.log, "\n".join(f"line{i}" for i in range(3, 8)))

    def test_log_empty_when_no_output(self):
        mon = capture.CaptureMonitor()
        self.assertEqual(mon.log, "")


class TestCheckCrashDetection(unittest.TestCase):
    def setUp(self):
        self.mon = capture.CaptureMonitor()

    def tearDown(self):
        self.mon.stop(blocking=False)

    def test_check_noop_when_never_started(self):
        self.mon.check(8080)  # must not raise
        self.assertEqual(self.mon.status, "stopped")

    def test_check_detects_exit_and_sets_error(self):
        proc = _mock_popen(poll_returns=[1])  # already exited with code 1
        with patch("subprocess.Popen", return_value=proc):
            self.mon.start(mitmdump_bin="mitmdump", addon_path="/x/addon.py")
        with self.mon._log_lock:
            self.mon._log_lines.append("Error: address already in use")
        self.mon.check(8080)
        self.assertEqual(self.mon.status, "error")
        self.assertIn("address already in use", self.mon.error_msg)
        self.assertIsNone(self.mon.process)

    def test_check_falls_back_to_exit_code_when_no_stderr(self):
        proc = _mock_popen(poll_returns=[137])
        with patch("subprocess.Popen", return_value=proc):
            self.mon.start(mitmdump_bin="mitmdump", addon_path="/x/addon.py")
        self.mon.check(8080)
        self.assertIn("137", self.mon.error_msg)

    def test_check_marks_running_once_port_is_open(self):
        proc = _mock_popen(poll_returns=[None, None])
        port = _free_port()
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        try:
            with patch("subprocess.Popen", return_value=proc):
                self.mon.start(mitmdump_bin="mitmdump", addon_path="/x/addon.py")
            self.mon.check(port)
            self.assertEqual(self.mon.status, "running")
        finally:
            srv.close()

    def test_check_stays_starting_when_port_not_open(self):
        proc = _mock_popen(poll_returns=[None])
        with patch("subprocess.Popen", return_value=proc):
            self.mon.start(mitmdump_bin="mitmdump", addon_path="/x/addon.py")
        self.mon.check(_free_port())  # nothing listens on it (just freed)
        self.assertEqual(self.mon.status, "starting")

    def test_check_does_not_reprobe_once_running(self):
        # Once status is past "starting", check() must skip the port probe
        # entirely (mirrors SSHMonitor's connected-state optimization).
        proc = _mock_popen(poll_returns=[None, None])
        with patch("subprocess.Popen", return_value=proc):
            self.mon.start(mitmdump_bin="mitmdump", addon_path="/x/addon.py")
        self.mon._status = "running"
        with patch("socket.socket") as sock_cls:
            self.mon.check(8080)
        sock_cls.assert_not_called()


class TestRealSubprocessLifecycle(unittest.TestCase):
    """Exercises the real Popen + background stderr-reader thread path (no
    mocking) against tiny stand-in scripts -- the part that's hard to trust
    via mocking alone, mirroring proxy.py's SSHMonitor design intent."""

    def setUp(self):
        self.mon = capture.CaptureMonitor()
        self._stub_paths = []

    def tearDown(self):
        self.mon.stop(blocking=False)
        for p in self._stub_paths:
            try:
                os.remove(p)
            except OSError:
                pass

    def _stub(self, body):
        path = _make_stub(body)
        self._stub_paths.append(path)
        return path

    def test_real_process_reaches_running_when_port_binds(self):
        stub = self._stub(STUB_LISTEN)
        port = _free_port()

        self.mon.start(mitmdump_bin=stub, addon_path="/x/addon.py", capture_port=port)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.mon.status != "running":
            self.mon.check(port)
            time.sleep(0.05)
        self.assertEqual(self.mon.status, "running")

    def test_real_process_crash_captures_stderr_and_sets_error(self):
        stub = self._stub(STUB_CRASH)
        self.mon.start(mitmdump_bin=stub, addon_path="/x/addon.py", capture_port=_free_port())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.mon.status != "error":
            self.mon.check(18080)
            time.sleep(0.05)
        self.assertEqual(self.mon.status, "error")
        self.assertIn("addon import failed", self.mon.error_msg)

    def test_real_stop_terminates_the_os_process(self):
        stub = self._stub(STUB_LISTEN)
        port = _free_port()

        self.mon.start(mitmdump_bin=stub, addon_path="/x/addon.py", capture_port=port)
        pid = self.mon.process.pid
        self.mon.stop()
        # Signalling a reaped PID raises ProcessLookupError -- proves the OS
        # process is actually gone, not just detached from our object.
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


class TestCleanupExpiredCaptures(unittest.TestCase):
    """ADR-022 Task 5 AC-1. Locked semantics: retention_days > 0 -> delete
    daily files whose age >= retention_days (keeps exactly retention_days
    days, today inclusive); retention_days <= 0 -> no-op, keep everything."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _touch(self, date_str):
        path = os.path.join(self.tmpdir, f"{date_str}.jsonl")
        with open(path, "w") as f:
            f.write('{"marker": "test"}\n')
        return path

    def test_retention_days_zero_keeps_everything(self):
        old = self._touch((datetime.now().date() - timedelta(days=30)).isoformat())
        deleted = capture.cleanup_expired_captures(self.tmpdir, 0)
        self.assertEqual(deleted, 0)
        self.assertTrue(os.path.exists(old))

    def test_retention_days_negative_keeps_everything(self):
        old = self._touch((datetime.now().date() - timedelta(days=30)).isoformat())
        deleted = capture.cleanup_expired_captures(self.tmpdir, -1)
        self.assertEqual(deleted, 0)
        self.assertTrue(os.path.exists(old))

    def test_deletes_files_at_or_past_the_retention_boundary_keeps_inside_it(self):
        today = datetime.now().date()
        recent = self._touch((today - timedelta(days=3)).isoformat())
        boundary_kept = self._touch((today - timedelta(days=6)).isoformat())
        boundary_expired = self._touch((today - timedelta(days=7)).isoformat())
        very_old = self._touch((today - timedelta(days=30)).isoformat())

        deleted = capture.cleanup_expired_captures(self.tmpdir, retention_days=7)

        self.assertEqual(deleted, 2)
        self.assertTrue(os.path.exists(recent))
        self.assertTrue(os.path.exists(boundary_kept))
        self.assertFalse(os.path.exists(boundary_expired))
        self.assertFalse(os.path.exists(very_old))

    def test_never_deletes_todays_file(self):
        today_path = self._touch(datetime.now().date().isoformat())
        capture.cleanup_expired_captures(self.tmpdir, retention_days=1)
        self.assertTrue(os.path.exists(today_path))

    def test_ignores_non_matching_filenames(self):
        stray = os.path.join(self.tmpdir, "notes.txt")
        with open(stray, "w") as f:
            f.write("hello")
        bad_date = os.path.join(self.tmpdir, "2026-99-99.jsonl")
        with open(bad_date, "w") as f:
            f.write("{}")
        deleted = capture.cleanup_expired_captures(self.tmpdir, retention_days=1)
        self.assertEqual(deleted, 0)
        self.assertTrue(os.path.exists(stray))
        self.assertTrue(os.path.exists(bad_date))

    def test_missing_dir_is_noop(self):
        deleted = capture.cleanup_expired_captures(
            os.path.join(self.tmpdir, "does-not-exist"), retention_days=7,
        )
        self.assertEqual(deleted, 0)

    def test_returns_deleted_count(self):
        today = datetime.now().date()
        self._touch((today - timedelta(days=10)).isoformat())
        self._touch((today - timedelta(days=11)).isoformat())
        self._touch((today - timedelta(days=1)).isoformat())  # kept
        deleted = capture.cleanup_expired_captures(self.tmpdir, retention_days=7)
        self.assertEqual(deleted, 2)


class TestStartTriggersRetentionCleanup(unittest.TestCase):
    def setUp(self):
        self.mon = capture.CaptureMonitor()

    def tearDown(self):
        self.mon.stop(blocking=False)

    def test_start_calls_cleanup_with_resolved_dir_and_configured_retention(self):
        with patch("subprocess.Popen", return_value=_mock_popen()), \
             patch.object(capture, "cleanup_expired_captures") as cleanup:
            self.mon.start(
                mitmdump_bin="mitmdump", addon_path="/x/addon.py",
                capture_dir="/tmp/xyz-captures", retention_days=7,
            )
        cleanup.assert_called_once_with("/tmp/xyz-captures", 7)

    def test_start_default_retention_is_zero(self):
        with patch("subprocess.Popen", return_value=_mock_popen()), \
             patch.object(capture, "cleanup_expired_captures") as cleanup:
            self.mon.start(mitmdump_bin="mitmdump", addon_path="/x/addon.py")
        cleanup.assert_called_once_with(capture.DEFAULT_CAPTURE_DIR, 0)

    def test_cleanup_failure_does_not_block_start(self):
        # Defense in depth: cleanup_expired_captures itself never raises
        # (internal try/except around each file op), but start() ALSO
        # guards the call site -- if cleanup somehow still raised (a future
        # contract violation), starting mitmdump must not be derailed by it.
        with patch("subprocess.Popen", return_value=_mock_popen()) as popen, \
             patch.object(capture, "cleanup_expired_captures", side_effect=OSError("boom")):
            self.mon.start(mitmdump_bin="mitmdump", addon_path="/x/addon.py")
        popen.assert_called_once()
        self.assertEqual(self.mon.status, "starting")


if __name__ == "__main__":
    unittest.main()
