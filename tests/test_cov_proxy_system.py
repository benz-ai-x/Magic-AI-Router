"""Coverage tests for proxy + system layer.

Targets the specific uncovered lines in:
- proxy.py (limited_client timeout/happy path in run_proxy,
  is_host_key_changed, ProxyRuntime start/stop/running)
- async_runtime.py (stop_event before worker, superseded generation,
  post-factory stop_event)
- subprocess_monitor.py (stderr reader crash, _wait_log_thread join,
  SIGKILL timeout warning, _reap_process body, _probe_ready NotImplementedError)
- sys_proxy_controller.py (sync branches: invalid port, empty snapshot,
  apply success, apply fail with desired, release success/fail, quit warning)
- system_proxy.py (_run nonzero returncode, recover_stale_transaction
  valid journal path)
- sleep_blocker.py (release kill after SIGTERM timeout)
- retry_scheduler.py (_mark_due stale-generation early return)
- host_key_flow.py (begin_replacement inner thread body, stale
  _finish_replacement)
- host_key.py (accept/replace os.close OSError handlers)
"""
import asyncio
import json
import os
import socket
import subprocess
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tunnel import proxy
from tunnel import async_runtime
from tunnel import subprocess_monitor
from sysctl import sys_proxy_controller
from sysctl import system_proxy
from sysctl import sleep_blocker
from tunnel import retry_scheduler
from tunnel import host_key_flow
from tunnel import host_key
# ── proxy.py: limited_client (402-412) ──────────────────────────────


def _free_port():
    """Return an unused loopback port."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestRunProxyLimitedClientHappyPath(unittest.IsolatedAsyncioTestCase):
    """Exercise limited_client's acquire → handle_client → release flow."""

    async def test_request_through_run_proxy(self):
        http_port = _free_port()
        control = {}
        config = {"socks5_port": 1080, "http_listen_port": http_port}
        task = asyncio.create_task(
            proxy.run_proxy(config, proxy.Stats(), control))
        try:
            for _ in range(50):
                if "server" in control:
                    break
                await asyncio.sleep(0.02)
            self.assertIn("server", control)
            # Send a malformed single-token request — handle_client catches
            # the error and closes; limited_client's finally releases the
            # semaphore.
            _reader, writer = await asyncio.open_connection(
                "127.0.0.1", http_port)
            writer.write(b"BADREQUEST\r\n\r\n")
            await writer.drain()
            await asyncio.sleep(0.2)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
        finally:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


class TestRunProxyLimitedClient503(unittest.IsolatedAsyncioTestCase):
    """When MAX_CLIENT_CONNECTIONS=0, semaphore never grants —
    limited_client times out and writes 503."""

    async def test_semaphore_timeout_returns_503(self):
        http_port = _free_port()
        with patch.object(proxy, "MAX_CLIENT_CONNECTIONS", 0):
            control = {}
            config = {"socks5_port": 1080, "http_listen_port": http_port}
            task = asyncio.create_task(
                proxy.run_proxy(config, proxy.Stats(), control))
            try:
                for _ in range(50):
                    if "server" in control:
                        break
                    await asyncio.sleep(0.02)
                self.assertIn("server", control)
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", http_port)
                writer.write(b"GET http://example.com/ HTTP/1.1\r\n\r\n")
                await writer.drain()
                data = await asyncio.wait_for(reader.read(200), timeout=5)
                self.assertIn(b"503", data)
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
            finally:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass


# ── proxy.py: is_host_key_changed (318) ─────────────────────────────


class TestSSHMonitorHostKeyChanged(unittest.TestCase):
    """Line 318: is_host_key_changed property."""

    def test_returns_true_on_host_key_changed_error(self):
        m = proxy.SSHMonitor(line_sink=lambda _: None)
        m.set_status("error")
        m.set_error("REMOTE HOST IDENTIFICATION HAS CHANGED")
        self.assertTrue(m.is_host_key_changed)

    def test_returns_false_on_other_error(self):
        m = proxy.SSHMonitor(line_sink=lambda _: None)
        m.set_status("error")
        m.set_error("connection refused")
        self.assertFalse(m.is_host_key_changed)

    def test_returns_false_when_not_error(self):
        m = proxy.SSHMonitor(line_sink=lambda _: None)
        m.set_status("connected")
        self.assertFalse(m.is_host_key_changed)


# ── proxy.py: ProxyRuntime running/start/stop (435, 442-454, 457) ───


class TestProxyRuntimeLifecycle(unittest.TestCase):
    """Cover ProxyRuntime.running, start(), stop()."""

    def test_running_false_on_init(self):
        rt = proxy.ProxyRuntime(proxy.Stats())
        self.assertFalse(rt.running)

    def test_start_and_stop(self):
        http_port = _free_port()
        rt = proxy.ProxyRuntime(proxy.Stats())
        config = {"socks5_port": 1080, "http_listen_port": http_port}
        self.assertTrue(rt.start(config))
        # Wait for the server to come up.
        for _ in range(30):
            if rt.running:
                break
            time.sleep(0.05)
        self.assertTrue(rt.running)
        self.assertTrue(rt.stop(timeout=3))
        self.assertFalse(rt.running)


# ── async_runtime.py: stop_event set before worker runs (68) ────────


class TestAsyncRuntimeStopEventBeforeWorker(unittest.TestCase):
    """Line 68: worker sees stop_event set before doing any work."""

    def test_stop_event_set_before_factory(self):
        rt = async_runtime.AsyncRuntime("test-pre-stop", stop_timeout=2)
        real_new_loop = asyncio.new_event_loop

        def patched_new_loop():
            # Runs inside the worker thread before the stop_event check.
            rt._stop_event.set()
            return real_new_loop()

        def factory(loop):
            async def main():
                await asyncio.sleep(100)
            task = loop.create_task(main())

            def stop_fn():
                loop.call_soon_threadsafe(task.cancel)

            return task, stop_fn

        with patch("asyncio.new_event_loop", patched_new_loop):
            rt.start(factory)
        time.sleep(0.3)
        # Worker returned at line 68 without setting error or running factory.
        self.assertEqual(rt.error, "")
        self.assertFalse(rt.running)


# ── async_runtime.py: superseded generation (74-76) ─────────────────


class TestAsyncRuntimeSupersededGeneration(unittest.TestCase):
    """Lines 74-76: factory triggers start() recursively, bumping
    generation before the old worker checks it."""

    def test_superseded_generation_closes_coroutine(self):
        rt = async_runtime.AsyncRuntime("test-supersede", stop_timeout=2)

        def inner_factory(loop):
            async def main():
                await asyncio.sleep(100)
            task = loop.create_task(main())

            def stop_fn():
                loop.call_soon_threadsafe(task.cancel)

            return task, stop_fn

        def outer_factory(loop):
            async def main():
                await asyncio.sleep(100)

            coro = main()

            def stop_fn():
                pass

            # Recursively start — bumps generation. Old worker sees
            # generation != self._generation and closes the coroutine.
            rt.start(inner_factory)
            return coro, stop_fn

        rt.start(outer_factory)
        time.sleep(0.5)
        # Clean up the surviving inner generation.
        rt.stop()
        # No "coroutine was never awaited" warning means close path ran.


# ── async_runtime.py: stop_event set after factory (80-84) ──────────


class TestAsyncRuntimeStopEventAfterFactory(unittest.TestCase):
    """Lines 80-84: stop_event set between factory returning and
    run_until_complete — worker calls stop_fn() and returns."""

    def test_stop_event_set_in_factory_calls_stop_fn(self):
        rt = async_runtime.AsyncRuntime("test-stop-after", stop_timeout=2)
        stop_fn_called = threading.Event()

        def factory(loop):
            async def main():
                await asyncio.sleep(100)
            task = loop.create_task(main())

            def stop_fn():
                stop_fn_called.set()
                loop.call_soon_threadsafe(task.cancel)

            # Set stop_event before returning so the worker enters the
            # post-factory stop_event check (line 79-84).
            rt._stop_event.set()
            return task, stop_fn

        rt.start(factory)
        time.sleep(0.5)
        self.assertTrue(stop_fn_called.is_set())

    def test_stop_fn_exception_in_post_factory_check_swallowed(self):
        """Lines 82-83: stop_fn raises a non-RuntimeError Exception —
        the bare except catches and swallows it."""
        rt = async_runtime.AsyncRuntime("test-stop-exn", stop_timeout=2)

        def factory(loop):
            async def main():
                await asyncio.sleep(100)
            task = loop.create_task(main())

            def stop_fn():
                raise ValueError("stop_fn broke")

            rt._stop_event.set()
            return task, stop_fn

        rt.start(factory)
        time.sleep(0.5)
        # Worker swallowed the exception and returned cleanly.
        self.assertFalse(rt.running)


# ── subprocess_monitor.py: stderr reader crash (128-129) ────────────


class TestSubprocessMonitorStderrCrash(unittest.TestCase):
    """Lines 128-129: exception handler in _read_stderr."""

    def test_stderr_reader_crash_is_caught(self):
        from tunnel.subprocess_monitor import SubprocessMonitor

        class _M(SubprocessMonitor):
            _PROCESS_NAME = "test"

            def _probe_ready(self, port):
                return True

        m = _M()

        class _BadStderr:
            def __iter__(self):
                raise RuntimeError("stderr I/O broke")

        class _FakeProc:
            stderr = _BadStderr()

        # Should not raise.
        m._read_stderr(_FakeProc())


# ── subprocess_monitor.py: _wait_log_thread join (174) ─────────────


class TestSubprocessMonitorWaitLogThreadJoin(unittest.TestCase):
    """Line 174: _wait_log_thread.join(timeout=1) when thread is alive."""

    def test_wait_log_thread_joins_live_thread(self):
        from tunnel.subprocess_monitor import SubprocessMonitor

        class _M(SubprocessMonitor):
            _PROCESS_NAME = "test"

            def _probe_ready(self, port):
                return True

        m = _M()
        block = threading.Event()

        def blocker():
            time.sleep(3)

        t = threading.Thread(target=blocker, daemon=True)
        t.start()
        m._log_thread = t
        m._wait_log_thread()  # join(timeout=1) — returns after 1s


# ── subprocess_monitor.py: SIGKILL timeout warning (166-167) ────────


class TestSubprocessMonitorSigkillTimeout(unittest.TestCase):
    """Lines 166-167: blocking stop where SIGKILL + wait also times out."""

    def test_sigkill_timeout_logs_warning(self):
        from tunnel.subprocess_monitor import SubprocessMonitor

        class _M(SubprocessMonitor):
            _PROCESS_NAME = "test"

            def _probe_ready(self, port):
                return True

        m = _M()
        mock_proc = MagicMock()
        # First wait (after SIGTERM) times out, second wait (after SIGKILL)
        # also times out → logger.warning line 167.
        mock_proc.wait.side_effect = subprocess.TimeoutExpired(
            cmd="x", timeout=5)
        m.process = mock_proc
        m._log_thread = MagicMock(is_alive=lambda: False)
        with self.assertLogs("magic-proxy.subprocess", level="WARNING") as cm:
            m.stop(blocking=True)
        self.assertTrue(any("SIGKILL" in msg for msg in cm.output))


# ── subprocess_monitor.py: _reap_process (180-185, 187) ─────────────


class TestSubprocessMonitorReapProcess(unittest.TestCase):

    def test_reap_clean_exit(self):
        """proc.wait() succeeds — no kill needed."""
        proc = MagicMock()
        proc.wait.return_value = 0
        log_thread = MagicMock(is_alive=lambda: False)
        subprocess_monitor.SubprocessMonitor._reap_process(proc, log_thread)
        proc.wait.assert_called_once()
        proc.kill.assert_not_called()

    def test_reap_kill_on_timeout(self):
        """proc.wait() times out, kill + second wait succeed."""
        proc = MagicMock()
        proc.wait.side_effect = [
            subprocess.TimeoutExpired(cmd="x", timeout=5),
            0,
        ]
        log_thread = MagicMock(is_alive=lambda: False)
        subprocess_monitor.SubprocessMonitor._reap_process(proc, log_thread)
        proc.kill.assert_called_once()

    def test_reap_kill_and_wait_both_timeout(self):
        """Lines 184-185: both wait calls time out — inner except
        (OSError, TimeoutExpired) swallows."""
        proc = MagicMock()
        proc.wait.side_effect = subprocess.TimeoutExpired(
            cmd="x", timeout=5)
        proc.kill.return_value = None
        log_thread = MagicMock(is_alive=lambda: False)
        subprocess_monitor.SubprocessMonitor._reap_process(proc, log_thread)
        proc.kill.assert_called_once()

    def test_reap_log_thread_join(self):
        """Line 187: log_thread.is_alive() True — join(timeout=1)."""
        proc = MagicMock()
        proc.wait.return_value = 0

        def blocker():
            time.sleep(3)

        t = threading.Thread(target=blocker, daemon=True)
        t.start()
        subprocess_monitor.SubprocessMonitor._reap_process(proc, t)


# ── subprocess_monitor.py: _probe_ready NotImplementedError (216) ──


class TestSubprocessMonitorProbeNotImplemented(unittest.TestCase):

    def test_base_probe_ready_raises(self):
        from tunnel.subprocess_monitor import SubprocessMonitor
        m = SubprocessMonitor()
        with self.assertRaises(NotImplementedError):
            m._probe_ready(8080)


# ── subprocess_monitor: non-blocking stop with real subprocess ─────


class TestSubprocessMonitorNonBlockingStopRealProc(unittest.TestCase):
    """stop(blocking=False) with a real subprocess — the spawned reaper
    thread runs _reap_process to completion."""

    def test_non_blocking_stop_terminates_real_subprocess(self):
        from tunnel.subprocess_monitor import SubprocessMonitor

        class _M(SubprocessMonitor):
            _PROCESS_NAME = "test"

            def _probe_ready(self, port):
                return True

        m = _M()
        proc = subprocess.Popen(
            ["sleep", "30"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            m.process = proc
            m._log_thread = None
            m._status = "running"
            m.stop(blocking=False)
            for _ in range(30):
                if proc.poll() is not None:
                    break
                time.sleep(0.1)
            self.assertIsNotNone(proc.poll())
            self.assertEqual(m.status, "stopped")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


# ── sys_proxy_controller.py: comprehensive sync() branch coverage ──


def _make_ssh(connected=True):
    ssh = MagicMock()
    ssh.status = "connected" if connected else "stopped"
    return ssh


class TestSyncInvalidPortOn(unittest.TestCase):
    """Lines 85-86: invalid http_listen_port + _on=True."""

    @patch("sysctl.sys_proxy_controller.system_proxy")
    def test_invalid_port_when_on_sets_error(self, mock_sp):
        mock_sp.recover_stale_transaction.return_value = (True, "")
        ssh = _make_ssh()
        ctrl = sys_proxy_controller.SystemProxyController(
            ssh_monitor=ssh,
            capture_state=lambda: (False, ""),
            config_fn=lambda: {"http_listen_port": "not-a-port"},
            paused_fn=lambda: False,
            initial_on=True,
        )
        ctrl.sync()
        self.assertIn("invalid", ctrl.error)


class TestSyncApplySuccess(unittest.TestCase):
    """Lines 76-83, 89-103, 111-113: happy-path apply."""

    @patch("sysctl.sys_proxy_controller.system_proxy")
    def test_apply_success(self, mock_sp):
        mock_sp.recover_stale_transaction.return_value = (True, "")
        mock_sp.snapshot.return_value = {"Wi-Fi": "snapshot"}
        mock_sp.apply_transaction.return_value = (
            True, "", {"Wi-Fi": "desired"})
        ssh = _make_ssh()
        ctrl = sys_proxy_controller.SystemProxyController(
            ssh_monitor=ssh,
            capture_state=lambda: (False, ""),
            config_fn=lambda: {"http_listen_port": 8888},
            paused_fn=lambda: False,
            initial_on=True,
        )
        with self.assertLogs("magic-proxy.sys_proxy_ctrl", level="INFO"):
            ctrl.sync()
        self.assertTrue(ctrl._applied)
        mock_sp.apply_transaction.assert_called_once()


class TestSyncEmptySnapshot(unittest.TestCase):
    """Lines 94-96: snapshot empty → error + return."""

    @patch("sysctl.sys_proxy_controller.system_proxy")
    def test_empty_snapshot_sets_error(self, mock_sp):
        mock_sp.recover_stale_transaction.return_value = (True, "")
        mock_sp.snapshot.return_value = {}
        ssh = _make_ssh()
        ctrl = sys_proxy_controller.SystemProxyController(
            ssh_monitor=ssh,
            capture_state=lambda: (False, ""),
            config_fn=lambda: {"http_listen_port": 8888},
            paused_fn=lambda: False,
            initial_on=True,
        )
        with self.assertLogs("magic-proxy.sys_proxy_ctrl", level="WARNING"):
            ctrl.sync()
        self.assertIn("could not safely snapshot", ctrl.error)
        mock_sp.apply_transaction.assert_not_called()


class TestSyncApplyFailDesiredNone(unittest.TestCase):
    """Lines 104-107: apply fails with desired_state=None."""

    @patch("sysctl.sys_proxy_controller.system_proxy")
    def test_apply_fail_desired_none_clears_snapshot(self, mock_sp):
        mock_sp.recover_stale_transaction.return_value = (True, "")
        mock_sp.snapshot.return_value = {"Wi-Fi": "snapshot"}
        mock_sp.apply_transaction.return_value = (
            False, "apply boom", None)
        ssh = _make_ssh()
        ctrl = sys_proxy_controller.SystemProxyController(
            ssh_monitor=ssh,
            capture_state=lambda: (False, ""),
            config_fn=lambda: {"http_listen_port": 8888},
            paused_fn=lambda: False,
            initial_on=True,
        )
        with self.assertLogs("magic-proxy.sys_proxy_ctrl", level="WARNING"):
            ctrl.sync()
        self.assertIsNone(ctrl._snapshot)
        self.assertIsNone(ctrl._desired)
        self.assertIn("apply boom", ctrl.error)


class TestSyncApplyFailDesiredNotNone(unittest.TestCase):
    """Lines 108-110: apply fails with desired_state not None → _on=False."""

    @patch("sysctl.sys_proxy_controller.system_proxy")
    def test_apply_fail_with_desired_turns_off(self, mock_sp):
        mock_sp.recover_stale_transaction.return_value = (True, "")
        mock_sp.snapshot.return_value = {"Wi-Fi": "snapshot"}
        mock_sp.apply_transaction.return_value = (
            False, "rollback failed: boom", {"Wi-Fi": "desired"})
        ssh = _make_ssh()
        ctrl = sys_proxy_controller.SystemProxyController(
            ssh_monitor=ssh,
            capture_state=lambda: (False, ""),
            config_fn=lambda: {"http_listen_port": 8888},
            paused_fn=lambda: False,
            initial_on=True,
        )
        with self.assertLogs("magic-proxy.sys_proxy_ctrl", level="WARNING"):
            ctrl.sync()
        self.assertFalse(ctrl.on)


class TestSyncReapplyDifferentTarget(unittest.TestCase):
    """Line 90: already applied with a different target → re-apply."""

    @patch("sysctl.sys_proxy_controller.system_proxy")
    def test_reapply_on_target_change(self, mock_sp):
        mock_sp.recover_stale_transaction.return_value = (True, "")
        mock_sp.apply_transaction.return_value = (
            True, "", {"Wi-Fi": "desired"})
        ssh = _make_ssh()
        ctrl = sys_proxy_controller.SystemProxyController(
            ssh_monitor=ssh,
            capture_state=lambda: (False, ""),
            config_fn=lambda: {"http_listen_port": 9999},  # different port
            paused_fn=lambda: False,
            initial_on=True,
        )
        ctrl._applied = True
        ctrl._applied_target = ("127.0.0.1", 8888)  # old target
        ctrl._snapshot = {"Wi-Fi": "snapshot"}
        with self.assertLogs("magic-proxy.sys_proxy_ctrl", level="INFO"):
            ctrl.sync()
        mock_sp.apply_transaction.assert_called_once()
        self.assertEqual(ctrl._applied_target, ("127.0.0.1", 9999))


class TestSyncNoOpWhenAppliedSameTarget(unittest.TestCase):
    """Line 90: desired=True, applied=True, same target → neither branch."""

    @patch("sysctl.sys_proxy_controller.system_proxy")
    def test_noop_on_same_target(self, mock_sp):
        mock_sp.recover_stale_transaction.return_value = (True, "")
        ssh = _make_ssh()
        ctrl = sys_proxy_controller.SystemProxyController(
            ssh_monitor=ssh,
            capture_state=lambda: (False, ""),
            config_fn=lambda: {"http_listen_port": 8888},
            paused_fn=lambda: False,
            initial_on=True,
        )
        ctrl._applied = True
        ctrl._applied_target = ("127.0.0.1", 8888)
        ctrl._snapshot = {"Wi-Fi": "snapshot"}
        ctrl.sync()
        mock_sp.apply_transaction.assert_not_called()
        mock_sp.release_transaction.assert_not_called()


class TestSyncReleaseSuccess(unittest.TestCase):
    """Lines 116-127: not desired, snapshot exists → release succeeds."""

    @patch("sysctl.sys_proxy_controller.system_proxy")
    def test_release_success_clears_state(self, mock_sp):
        mock_sp.recover_stale_transaction.return_value = (True, "")
        mock_sp.release_transaction.return_value = (True, "")
        ssh = _make_ssh(connected=False)  # not connected → desired=False
        ctrl = sys_proxy_controller.SystemProxyController(
            ssh_monitor=ssh,
            capture_state=lambda: (False, ""),
            config_fn=lambda: {"http_listen_port": 8888},
            paused_fn=lambda: False,
            initial_on=True,
        )
        ctrl._snapshot = {"Wi-Fi": "snapshot"}
        ctrl._desired = {"Wi-Fi": "desired"}
        ctrl._applied = True
        with self.assertLogs("magic-proxy.sys_proxy_ctrl", level="INFO"):
            ctrl.sync()
        mock_sp.release_transaction.assert_called_once()
        self.assertIsNone(ctrl._snapshot)
        self.assertFalse(ctrl._applied)


class TestSyncReleaseFail(unittest.TestCase):
    """Lines 125, 128-129: not desired, snapshot exists → release fails."""

    @patch("sysctl.sys_proxy_controller.system_proxy")
    def test_release_fail_logs_warning(self, mock_sp):
        mock_sp.recover_stale_transaction.return_value = (True, "")
        mock_sp.release_transaction.return_value = (False, "release denied")
        ssh = _make_ssh(connected=False)
        ctrl = sys_proxy_controller.SystemProxyController(
            ssh_monitor=ssh,
            capture_state=lambda: (False, ""),
            config_fn=lambda: {"http_listen_port": 8888},
            paused_fn=lambda: False,
            initial_on=True,
        )
        ctrl._snapshot = {"Wi-Fi": "snapshot"}
        ctrl._desired = {"Wi-Fi": "desired"}
        ctrl._applied = True
        with self.assertLogs("magic-proxy.sys_proxy_ctrl", level="WARNING"):
            ctrl.sync()
        self.assertIn("release denied", ctrl.error)


class TestQuitCleanupReleaseFail(unittest.TestCase):
    """Line 137: quit_cleanup release fails → logger.warning."""

    @patch("sysctl.sys_proxy_controller.system_proxy")
    def test_quit_cleanup_release_fail_logs(self, mock_sp):
        mock_sp.recover_stale_transaction.return_value = (True, "")
        mock_sp.release_transaction.return_value = (False, "restore failed")
        ssh = _make_ssh()
        ctrl = sys_proxy_controller.SystemProxyController(
            ssh_monitor=ssh,
            capture_state=lambda: (False, ""),
            config_fn=lambda: {"http_listen_port": 8888},
            paused_fn=lambda: False,
            initial_on=False,
        )
        ctrl._snapshot = {"Wi-Fi": "snapshot"}
        ctrl._desired = {"Wi-Fi": "desired"}
        with self.assertLogs("magic-proxy.sys_proxy_ctrl",
                             level="WARNING") as cm:
            ctrl.quit_cleanup()
        self.assertTrue(any("restore on quit failed" in m for m in cm.output))


# ── system_proxy.py: _run nonzero returncode (31) ───────────────────


class TestSystemProxyRunNonZero(unittest.TestCase):

    def test_nonzero_returncode_returns_false_stderr(self):
        cp = MagicMock()
        cp.returncode = 1
        cp.stderr = "permission denied"
        with patch("subprocess.run", return_value=cp):
            ok, err = system_proxy._run(["networksetup", "-x"])
        self.assertFalse(ok)
        self.assertIn("permission denied", err)


# ── system_proxy.py: recover_stale_transaction valid journal (232-236)


class TestSystemProxyRecoverValidJournal(unittest.TestCase):
    """Lines 232-233, 236: valid journal → extract → release_transaction."""

    def test_valid_journal_calls_release_transaction(self):
        import tempfile
        journal_data = {
            "version": 1,
            "original": {"Wi-Fi": {"web": {"enabled": False}}},
            "desired": {"Wi-Fi": {"web": {"enabled": True}}},
        }
        with tempfile.TemporaryDirectory() as d, \
             patch.object(system_proxy, "JOURNAL_PATH",
                          os.path.join(d, "journal.json")), \
             patch.object(system_proxy, "release_transaction",
                          return_value=(True, "")) as mock_release:
            with open(system_proxy.JOURNAL_PATH, "w") as fh:
                json.dump(journal_data, fh)
            ok, err = system_proxy.recover_stale_transaction()
        self.assertTrue(ok)
        self.assertEqual(err, "")
        mock_release.assert_called_once_with(
            journal_data["original"], journal_data["desired"])


# ── sleep_blocker.py: SIGKILL after SIGTERM timeout (73-75) ─────────


class TestSleepBlockerReleaseKill(unittest.TestCase):

    def test_release_kills_on_sigterm_timeout(self):
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired(
            cmd="caffeinate", timeout=2)
        blocker = sleep_blocker.CaffeinateBlocker()
        blocker._proc = proc
        with self.assertLogs("magic-proxy.sleep_blocker",
                             level="WARNING") as cm:
            blocker.release()
        proc.terminate.assert_called_once()
        proc.kill.assert_called_once()
        self.assertTrue(any("SIGKILL" in m for m in cm.output))


# ── retry_scheduler.py: _mark_due stale generation (51) ─────────────


class TestRetrySchedulerMarkDueStaleGeneration(unittest.TestCase):

    def test_mark_due_stale_generation_returns_early(self):
        """Line 51: _mark_due with stale generation returns without
        setting _due or clearing _timer."""
        rs = retry_scheduler.RetryScheduler(delays=(0.01,))
        rs.handle_error()
        timer_before = rs._timer
        # Call _mark_due with a wrong generation.
        rs._mark_due(rs._generation + 999)
        # _due should still be False (early return before setting it).
        self.assertFalse(rs._due)
        # _timer should NOT have been cleared.
        self.assertIs(rs._timer, timer_before)
        rs.cancel()


# ── host_key_flow.py: begin_replacement thread body (103-104) ──────


class TestHostKeyFlowBeginReplacementThreadBody(unittest.TestCase):

    @patch("tunnel.host_key_flow.AppHelper")
    @patch("tunnel.host_key_flow.host_key.inspect")
    def test_begin_replacement_runs_scan_in_thread(self, mock_inspect,
                                                   mock_apphelper):
        """Lines 103-104: don't mock threading.Thread — let it run."""
        mock_inspect.return_value = (False, "keys", "fingerprints", None)
        captured = {}

        def fake_call_after(cb, *args):
            captured["cb"] = cb
            captured["args"] = args

        mock_apphelper.callAfter.side_effect = fake_call_after

        flow = host_key_flow.HostKeyFlow(
            ssh_monitor=MagicMock(),
            get_tunnel=lambda: {"ssh_host": "srv", "ssh_port": 22},
            get_socks5_port=lambda: 1080,
            get_password=lambda: "",
            on_connect=MagicMock(),
            on_reconnect=MagicMock(),
        )
        flow.begin_replacement()
        for _ in range(50):
            if "cb" in captured:
                break
            time.sleep(0.02)
        mock_inspect.assert_called_once_with(
            {"ssh_host": "srv", "ssh_port": 22}, force_scan=True)
        self.assertEqual(captured["cb"], flow._finish_replacement)


# ── host_key_flow.py: stale _finish_replacement (112) ───────────────


class TestHostKeyFlowFinishReplacementStaleGeneration(unittest.TestCase):

    def test_stale_generation_is_noop(self):
        flow = host_key_flow.HostKeyFlow(
            ssh_monitor=MagicMock(),
            get_tunnel=lambda: {"ssh_host": "srv", "ssh_port": 22},
            get_socks5_port=lambda: 1080,
            get_password=lambda: "",
            on_connect=MagicMock(),
            on_reconnect=MagicMock(),
        )
        current_gen = flow._generation
        flow._finish_replacement(
            current_gen + 999,
            {"ssh_host": "srv", "ssh_port": 22},
            (False, "keys", "fps", None),
        )
        flow._on_reconnect.assert_not_called()


# ── host_key.py: accept os.close OSError (123-124) ──────────────────


class TestHostKeyAcceptOsCloseError(unittest.TestCase):

    def test_accept_osclose_error_in_finally_swallowed(self):
        """Lines 123-124: os.close(lock_fd) raises in the finally loop
        but is caught and swallowed."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            known_hosts = os.path.join(d, "known_hosts")
            with patch.object(host_key, "APP_SECURITY_DIR", d), \
                 patch.object(host_key, "KNOWN_HOSTS_PATH", known_hosts), \
                 patch("os.close", side_effect=OSError("fd closed")):
                result = host_key.accept("example.com ssh-ed25519 AAAA")
        # The write succeeds; the finally block's OSError on os.close is
        # swallowed. accept returns True.
        self.assertTrue(result)


# ── host_key.py: replace os.close OSError (160-161) ─────────────────


class TestHostKeyReplaceOsCloseError(unittest.TestCase):

    def test_replace_osclose_lockfd_error_swallowed(self):
        """Lines 160-161: os.close(lock_fd) raises in replace's finally
        but is caught and swallowed."""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            known_hosts = os.path.join(d, "known_hosts")
            with open(known_hosts, "w") as fh:
                fh.write("old.example ssh-ed25519 OLDKEY\n")
            with patch.object(host_key, "APP_SECURITY_DIR", d), \
                 patch.object(host_key, "KNOWN_HOSTS_PATH", known_hosts), \
                 patch("os.close", side_effect=OSError("fd closed")):
                result = host_key.replace(
                    {"ssh_host": "example.com", "ssh_port": 22},
                    "example.com ssh-ed25519 NEWKEY",
                )
        # atomic_write succeeds; the finally block's OSError is swallowed.
        # replace returns True.
        self.assertTrue(result)


if __name__ == "__main__":
    unittest.main()
