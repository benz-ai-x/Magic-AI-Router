"""Tests for connection_coordinator.py — ConnectionCoordinator lifecycle."""
import unittest
from unittest.mock import MagicMock, patch

from tunnel.connection_coordinator import ConnectionCoordinator


def _make_config():
    return {
        "socks5_port": 1080,
        "http_listen_port": 8888,
        "current_tunnel": 0,
        "tunnels": [{"ssh_host": "test", "ssh_user": "user", "ssh_port": 22, "auth_type": "key"}],
    }


def _make_coordinator():
    stats = MagicMock()
    return ConnectionCoordinator(
        stats=stats,
        ssh_log_sink=lambda line: None,
        get_config=lambda: _make_config(),
        get_tunnel_password=lambda t: "",
    )


class TestInitialProperties(unittest.TestCase):
    def test_not_paused_on_init(self):
        conn = _make_coordinator()
        self.assertFalse(conn.paused)

    def test_proxy_not_running_on_init(self):
        conn = _make_coordinator()
        self.assertFalse(conn.proxy_running)

    def test_ssh_accessible(self):
        conn = _make_coordinator()
        self.assertIsNotNone(conn.ssh)

    def test_current_tunnel(self):
        conn = _make_coordinator()
        self.assertIsNotNone(conn.current_tunnel)
        self.assertEqual(conn.current_tunnel["ssh_host"], "test")

    def test_socks5_port(self):
        conn = _make_coordinator()
        self.assertEqual(conn.socks5_port, 1080)


class TestTogglePause(unittest.TestCase):
    def test_pause_sets_paused_true(self):
        conn = _make_coordinator()
        conn._proxy_runtime._rt._thread = None  # running → False
        with patch.object(conn._proxy_runtime, "stop", return_value=True) as m:
            paused = conn.toggle_pause()
        self.assertTrue(paused)
        self.assertTrue(conn.paused)
        # #40: pausing must not join the worker on the menu thread.
        m.assert_called_once_with(timeout=0)

    def test_resume_clears_paused(self):
        conn = _make_coordinator()
        conn._paused = True
        conn._proxy_runtime._rt._thread = None
        with patch.object(conn._proxy_runtime, "start", return_value=True), \
             patch.object(conn._host_key, "start_check"):
            paused = conn.toggle_pause()
        self.assertFalse(paused)
        self.assertFalse(conn.paused)


class TestCancel(unittest.TestCase):
    def test_cancel_stops_everything(self):
        conn = _make_coordinator()
        with patch.object(conn._ssh, "stop") as mock_ssh_stop, \
             patch.object(conn._proxy_runtime, "stop") as mock_proxy_stop:
            conn.cancel()
        mock_ssh_stop.assert_called_once()
        mock_proxy_stop.assert_called_once()
        self.assertFalse(conn.proxy_running)


class TestTickSplit(unittest.TestCase):
    def test_check_ssh_noop_when_paused(self):
        conn = _make_coordinator()
        conn._paused = True
        with patch.object(conn._ssh, "check") as mock_check:
            conn.check_ssh()
        mock_check.assert_not_called()

    def test_check_ssh_checks_when_connecting(self):
        conn = _make_coordinator()
        conn._ssh._status = "connecting"
        with patch.object(conn._ssh, "check") as mock_check:
            conn.check_ssh()
        mock_check.assert_called_once_with(1080)

    def test_handle_retry_calls_retry_connect(self):
        conn = _make_coordinator()
        conn._retry._due = True
        with patch.object(conn, "_retry_connect") as mock_connect:
            conn.handle_retry()
        mock_connect.assert_called_once()


class TestStopAll(unittest.TestCase):
    def test_stop_all_non_blocking(self):
        conn = _make_coordinator()
        with patch.object(conn._ssh, "stop") as mock_ssh_stop, \
             patch.object(conn._proxy_runtime, "stop") as mock_proxy_stop:
            conn.stop_all()
        mock_ssh_stop.assert_called_once_with(blocking=False)
        mock_proxy_stop.assert_called_once()
        self.assertFalse(conn.proxy_running)


class TestStart(unittest.TestCase):
    def test_start_launches_background_and_ssh(self):
        conn = _make_coordinator()
        conn._proxy_runtime._rt._thread = None
        with patch.object(conn, "_start_background") as bg, \
             patch.object(conn, "start_ssh") as ssh:
            conn.start()
        bg.assert_called_once()
        ssh.assert_called_once()

    def test_start_ssh_cancels_retry_and_checks_host_key(self):
        conn = _make_coordinator()
        with patch.object(conn._retry, "cancel") as cancel, \
             patch.object(conn._host_key, "start_check") as check:
            conn.start_ssh()
        cancel.assert_called_once()
        check.assert_called_once()
        self.assertFalse(conn.paused)


class TestCheckSshOutcomes(unittest.TestCase):
    def test_ignores_status_not_in_watchlist(self):
        conn = _make_coordinator()
        conn._ssh._status = "error"  # not in (connecting, connected, stopped)
        with patch.object(conn._ssh, "check") as mock_check:
            conn.check_ssh()
        mock_check.assert_not_called()

    def test_connected_resets_retry(self):
        conn = _make_coordinator()
        conn._ssh._status = "connecting"
        with patch.object(conn._ssh, "check"):
            # After check(), status becomes connected
            conn._ssh._status = "connected"
            with patch.object(conn._retry, "reset") as reset:
                conn.check_ssh()
        reset.assert_called_once()

    def test_error_with_host_key_change_begins_replacement(self):
        conn = _make_coordinator()
        conn._ssh._status = "connecting"
        conn._ssh._error_msg = "REMOTE HOST IDENTIFICATION HAS CHANGED"
        conn._host_key.change_prompted = False

        def fake_check(port):
            conn._ssh._status = "error"

        with patch.object(conn._ssh, "check", side_effect=fake_check), \
             patch.object(conn._host_key, "begin_replacement") as begin, \
             patch.object(conn._retry, "handle_error") as handle:
            conn.check_ssh()
        begin.assert_called_once()
        handle.assert_not_called()

    def test_error_without_host_key_change_schedules_retry(self):
        conn = _make_coordinator()
        conn._ssh._status = "connecting"
        conn._ssh._error_msg = "connection timed out"

        def fake_check(port):
            conn._ssh._status = "error"

        with patch.object(conn._ssh, "check", side_effect=fake_check), \
             patch.object(conn._retry, "handle_error") as handle:
            conn.check_ssh()
        handle.assert_called_once()


class TestRestart(unittest.TestCase):
    def test_restart_stops_reloads_and_restarts(self):
        conn = _make_coordinator()
        conn._proxy_runtime._rt._thread = None
        reload_fn = MagicMock()
        with patch.object(conn._retry, "cancel") as retry_cancel, \
             patch.object(conn._host_key, "cancel") as hk_cancel, \
             patch.object(conn._ssh, "stop") as ssh_stop, \
             patch.object(conn._proxy_runtime, "stop") as proxy_stop, \
             patch.object(conn, "_start_background") as bg, \
             patch.object(conn, "start_ssh") as start_ssh:
            conn.restart(reload_fn)
        retry_cancel.assert_called_once()
        hk_cancel.assert_called_once()
        ssh_stop.assert_called_once()
        proxy_stop.assert_called_once()
        reload_fn.assert_called_once()
        bg.assert_called_once()
        start_ssh.assert_called_once()


class TestRetryConnect(unittest.TestCase):
    def test_noop_when_already_connected(self):
        conn = _make_coordinator()
        conn._ssh._status = "connected"
        with patch.object(conn._ssh, "start") as start:
            conn._retry_connect()
        start.assert_not_called()

    def test_starts_ssh_when_stopped(self):
        conn = _make_coordinator()
        conn._ssh._status = "stopped"
        with patch.object(conn._ssh, "start") as start:
            conn._retry_connect()
        start.assert_called_once()


class TestStartBackground(unittest.TestCase):
    def test_skips_when_proxy_already_running(self):
        conn = _make_coordinator()
        with patch.object(type(conn._proxy_runtime), "running", True), \
             patch.object(conn._proxy_runtime, "start") as start:
            conn._start_background()
        start.assert_not_called()

    def test_starts_proxy_when_not_running(self):
        conn = _make_coordinator()
        with patch.object(type(conn._proxy_runtime), "running", False), \
             patch.object(conn._proxy_runtime, "start", return_value=True) as start:
            conn._start_background()
        start.assert_called_once()
        self.assertTrue(conn.proxy_running)


if __name__ == "__main__":
    unittest.main()


class TestThreadContract(unittest.TestCase):
    """#68：注释声称「ConnectionCoordinator owns its locking」——让它成真。
    daemon 线程 stop() 与主线程 tick check() 的无保护竞态曾以
    AttributeError 落在 rumps 定时器回调内。"""

    def test_concurrent_restart_and_check_no_crash(self):
        """daemon 线程 restart（桥接重连路径）与主线程 check_ssh 并发——
        锁归状态机所有者后，任何交错都不抛 AttributeError。"""
        import threading
        conn = _make_coordinator()
        # SSH monitor 真对象（subprocess_monitor 的 process 字段是竞态点）
        stop = threading.Event()
        errors = []

        def restarter():
            while not stop.is_set():
                try:
                    conn.restart(lambda: None)
                except Exception as exc:
                    errors.append(exc)
                    return

        def checker():
            while not stop.is_set():
                try:
                    conn.check_ssh()
                except AttributeError as exc:
                    errors.append(exc)
                    return

        t1 = threading.Thread(target=restarter, daemon=True)
        t2 = threading.Thread(target=checker, daemon=True)
        t1.start()
        t2.start()
        stop_after = threading.Timer(0.6, stop.set)
        stop_after.start()
        t1.join(3)
        t2.join(3)
        conn.stop_all()
        self.assertEqual(errors, [],
                         f"并发 restart/check 抛出异常（#68 无锁竞态）: {errors}")

