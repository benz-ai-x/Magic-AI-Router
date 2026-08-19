"""Tests for port_check module (lsof + kill wrapper)."""
import itertools
import signal
import subprocess
import unittest
from unittest.mock import patch, MagicMock

import port_check


def _completed(stdout="", stderr="", returncode=0):
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.stdout = stdout
    cp.stderr = stderr
    cp.returncode = returncode
    return cp


class TestWhoOwns(unittest.TestCase):
    def test_parses_lsof_pcn_output(self):
        # `lsof -F pcn` machine-readable format: each field prefixed by single char.
        # Real output sample (compacted):
        lsof_out = (
            "p12345\n"
            "cssh\n"
            "f10\n"
            "n127.0.0.1:1080\n"
        )
        ps_out = "ssh -D 1080 -N -o ExitOnForwardFailure=yes user@host\n"

        def fake_run(args, **kw):
            if args[0] == "lsof":
                return _completed(stdout=lsof_out)
            if args[0] == "ps":
                return _completed(stdout=ps_out)
            return _completed(returncode=1)

        with patch("subprocess.run", side_effect=fake_run):
            owner = port_check.who_owns(1080)
        self.assertIsNotNone(owner)
        self.assertEqual(owner.pid, 12345)
        self.assertEqual(owner.name, "ssh")
        self.assertEqual(owner.cmd, ps_out.strip())

    def test_returns_none_when_lsof_exit_one(self):
        # lsof exits 1 when no matching processes — that means port is free.
        with patch("subprocess.run", return_value=_completed(returncode=1)):
            self.assertIsNone(port_check.who_owns(1080))

    def test_returns_none_on_timeout(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("lsof", 5)):
            self.assertIsNone(port_check.who_owns(1080))

    def test_returns_none_on_oserror(self):
        # lsof not on PATH (shouldn't happen on macOS, but defensive).
        with patch("subprocess.run", side_effect=OSError("not found")):
            self.assertIsNone(port_check.who_owns(1080))

    def test_cmd_empty_when_ps_fails(self):
        lsof_out = "p999\ncpython3\nf7\nn127.0.0.1:8888\n"

        def fake_run(args, **kw):
            if args[0] == "lsof":
                return _completed(stdout=lsof_out)
            return _completed(returncode=1)  # ps fails

        with patch("subprocess.run", side_effect=fake_run):
            owner = port_check.who_owns(8888)
        self.assertEqual(owner.pid, 999)
        self.assertEqual(owner.name, "python3")
        self.assertEqual(owner.cmd, "")


class TestKill(unittest.TestCase):
    def test_sigterm_success_when_process_exits(self):
        # First os.kill (SIGTERM) succeeds; probing kill(pid, 0) raises ProcessLookupError.
        calls = []

        def fake_kill(pid, sig):
            calls.append(sig)
            if sig == 0:
                raise ProcessLookupError()
            return None  # SIGTERM accepted

        with patch("os.kill", side_effect=fake_kill), \
             patch("time.sleep") as sleep:
            ok, err = port_check.kill(12345, timeout=0.5)
        self.assertTrue(ok)
        self.assertEqual(err, "")
        self.assertIn(signal.SIGTERM, calls)
        sleep.assert_called()  # we slept during the poll loop

    def test_escalates_to_sigkill_when_sigterm_ignored(self):
        # Process stays alive: kill(pid, 0) keeps succeeding until after SIGKILL.
        state = {"alive": True}

        def fake_kill(pid, sig):
            if sig == signal.SIGKILL:
                state["alive"] = False
                return None
            if sig == 0:
                if state["alive"]:
                    return None  # still alive
                raise ProcessLookupError()
            return None  # SIGTERM accepted but ignored

        with patch("os.kill", side_effect=fake_kill), \
             patch("time.sleep"):
            ok, err = port_check.kill(12345, timeout=0.0)
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_permission_error_returns_false(self):
        with patch("os.kill", side_effect=PermissionError()), \
             patch("time.sleep"):
            ok, err = port_check.kill(12345)
        self.assertFalse(ok)
        self.assertIn("permission", err.lower())

    def test_initial_sigterm_finds_process_already_dead(self):
        # ProcessLookupError on the initial SIGTERM = already gone = success.
        with patch("os.kill", side_effect=ProcessLookupError()), \
             patch("time.sleep"):
            ok, err = port_check.kill(12345)
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_sigkill_also_ignored_returns_false(self):
        # Both SIGTERM and SIGKILL accepted, but poll always shows alive.
        def fake_kill(pid, sig):
            return None  # everything "succeeds" but process never dies

        clock = itertools.count(start=0, step=100)
        with patch("os.kill", side_effect=fake_kill), \
             patch("time.sleep"), \
             patch("time.monotonic", side_effect=lambda: next(clock)):
            ok, err = port_check.kill(12345, timeout=0.0)
        self.assertFalse(ok)
        self.assertIn("still alive", err.lower())

    def test_permission_error_on_sigkill_returns_false(self):
        call_seen = []

        def fake_kill(pid, sig):
            call_seen.append(sig)
            if sig == signal.SIGTERM:
                return None
            if sig == 0:
                return None  # always alive
            if sig == signal.SIGKILL:
                raise PermissionError()

        clock = itertools.count(start=0, step=100)
        with patch("os.kill", side_effect=fake_kill), \
             patch("time.sleep"), \
             patch("time.monotonic", side_effect=lambda: next(clock)):
            ok, err = port_check.kill(12345, timeout=0.0)
        self.assertFalse(ok)
        self.assertIn("permission", err.lower())
        self.assertIn("sigkill", err.lower())

    def test_sigkill_finds_process_already_dead(self):
        # SIGTERM accepted, poll shows alive, then SIGKILL raises ProcessLookupError
        # (process died between the poll and the kill) -> success.
        def fake_kill(pid, sig):
            if sig == signal.SIGKILL:
                raise ProcessLookupError()
            return None  # SIGTERM and sig-0 probe succeed (alive)

        clock = itertools.count(start=0, step=100)
        with patch("os.kill", side_effect=fake_kill), \
             patch("time.sleep"), \
             patch("time.monotonic", side_effect=lambda: next(clock)):
            ok, err = port_check.kill(12345, timeout=0.0)
        self.assertTrue(ok)
        self.assertEqual(err, "")


class TestParseLsofPcn(unittest.TestCase):
    def test_empty_line_skipped(self):
        self.assertEqual(port_check._parse_lsof_pcn("\np42\ncssh\n"), (42, "ssh"))

    def test_second_record_stops_at_first(self):
        out = "p1\ncfirst\np2\ncsecond\n"
        self.assertEqual(port_check._parse_lsof_pcn(out), (1, "first"))

    def test_non_numeric_pid_ignored(self):
        self.assertIsNone(port_check._parse_lsof_pcn("pabc\ncssh\n"))

    def test_no_pid_returns_none(self):
        self.assertIsNone(port_check._parse_lsof_pcn("cssh\nf10\n"))


class TestWhoOwnsUnparseable(unittest.TestCase):
    def test_lsof_zero_exit_but_unparseable(self):
        with patch("subprocess.run", return_value=_completed(stdout="garbage-no-pid")):
            self.assertIsNone(port_check.who_owns(1080))


class TestCmdlineOf(unittest.TestCase):
    def test_subprocess_error_returns_empty(self):
        with patch("subprocess.run", side_effect=OSError("ps missing")):
            self.assertEqual(port_check._cmdline_of(123), "")


class TestAliveHelper(unittest.TestCase):
    def test_permission_error_treated_as_alive(self):
        with patch("os.kill", side_effect=PermissionError()):
            self.assertTrue(port_check._alive(123))

    def test_process_lookup_error_treated_as_dead(self):
        with patch("os.kill", side_effect=ProcessLookupError()):
            self.assertFalse(port_check._alive(123))


class TestClearAppPorts(unittest.TestCase):
    """#40: _clear_app_ports must only kill previous instances of THIS app —
    a foreign service that happens to listen on 9527/9528 is spared."""

    def _owner(self, pid, cmd):
        return port_check.PortOwner(pid=pid, name="proc", cmd=cmd)

    def _run(self, owners):
        from app import _clear_app_ports
        self_pid = 999
        # Real _is_stale_instance (dev-mode argv) — the predicate's
        # discrimination is itself under test in TestIsStaleInstance.
        with patch("app.sys.argv", ["/repo/app.py"]), \
             patch("app.os.getpid", return_value=self_pid), \
             patch("app.port_check.who_owns", side_effect=owners), \
             patch("app.port_check.kill", return_value=(True, "")) as kill:
            _clear_app_ports()
        return kill

    def test_self_pid_never_killed(self):
        kill = self._run([self._owner(999, "python3 app.py"), None])
        kill.assert_not_called()

    def test_stale_own_instance_killed(self):
        kill = self._run([self._owner(42, "python3 app.py"), None])
        kill.assert_called_once_with(42)

    def test_foreign_process_spared(self):
        kill = self._run([self._owner(42, "nginx: worker process"), None])
        kill.assert_not_called()

    def test_unidentifiable_owner_spared(self):
        kill = self._run([self._owner(42, ""), None])
        kill.assert_not_called()

    def test_free_port_noop(self):
        kill = self._run([None, None])
        kill.assert_not_called()


class TestIsStaleInstance(unittest.TestCase):
    def _pred(self):
        from app import _is_stale_instance
        return _is_stale_instance

    def test_packaged_binary_path_matches(self):
        with patch("app.sys.argv", ["/Applications/Magic AI Router.app/Contents/MacOS/Magic AI Router"]):
            self.assertTrue(self._pred()(
                "/Applications/Magic AI Router.app/Contents/MacOS/Magic AI Router"))

    def test_dev_mode_script_matches(self):
        with patch("app.sys.argv", ["/repo/app.py"]):
            self.assertTrue(self._pred()("python3 app.py"))

    def test_similar_but_foreign_script_spared(self):
        with patch("app.sys.argv", ["/repo/app.py"]):
            self.assertFalse(self._pred()("python3 my_app.py"))

    def test_foreign_service_spared(self):
        with patch("app.sys.argv", ["/repo/app.py"]):
            self.assertFalse(self._pred()("nginx: worker process"))

    def test_empty_cmd_spared(self):
        with patch("app.sys.argv", ["/repo/app.py"]):
            self.assertFalse(self._pred()(""))


if __name__ == "__main__":
    unittest.main()
