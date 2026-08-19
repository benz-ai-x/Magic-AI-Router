"""Tests for system_proxy module (networksetup wrapper)."""
import subprocess
import os
import unittest
from unittest.mock import patch, MagicMock

import system_proxy


def _completed(stdout="", stderr="", returncode=0):
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.stdout = stdout
    cp.stderr = stderr
    cp.returncode = returncode
    return cp


class TestActiveServices(unittest.TestCase):
    def test_parses_listallnetworkservices_output(self):
        # First line is a notice; lines starting with '*' are disabled.
        output = (
            "An asterisk (*) denotes that a network service is disabled.\n"
            "Wi-Fi\n"
            "*Bluetooth PAN\n"
            "Thunderbolt Bridge\n"
            "\n"
        )
        with patch("subprocess.run", return_value=_completed(stdout=output)) as run:
            services = system_proxy._active_services()
        self.assertEqual(services, ["Wi-Fi", "Thunderbolt Bridge"])
        run.assert_called_once()
        args = run.call_args[0][0]
        self.assertEqual(args, ["networksetup", "-listallnetworkservices"])

    def test_returns_empty_on_subprocess_error(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("x", 5)):
            self.assertEqual(system_proxy._active_services(), [])


class TestSnapshotRestore(unittest.TestCase):
    def test_snapshot_and_restore_preserves_existing_proxy(self):
        outputs = {
            "-getwebproxy": "Enabled: Yes\nServer: corp.proxy\nPort: 3128\n",
            "-getsecurewebproxy": "Enabled: No\nServer: \nPort: 0\n",
            "-getproxybypassdomains": "localhost\n*.internal\n",
        }
        def fake_run(args, **kw):
            if args[1] == "-listallnetworkservices":
                return _completed(stdout="Header\nWi-Fi\n")
            return _completed(stdout=outputs.get(args[1], ""))
        with patch("subprocess.run", side_effect=fake_run):
            saved = system_proxy.snapshot()
        self.assertTrue(saved["Wi-Fi"]["web"]["enabled"])
        with patch("subprocess.run", return_value=_completed()) as run:
            ok, err = system_proxy.restore(saved)
        self.assertTrue(ok, err)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(["networksetup", "-setwebproxy", "Wi-Fi", "corp.proxy", "3128"], commands)
        self.assertIn(["networksetup", "-setsecurewebproxystate", "Wi-Fi", "off"], commands)


class TestTransactionalLease(unittest.TestCase):
    def setUp(self):
        self.original = {
            "Wi-Fi": {
                "web": {"enabled": False, "host": "", "port": "0"},
                "secure": {"enabled": False, "host": "", "port": "0"},
                "bypass": [],
            }
        }

    def test_failure_rolls_back_and_removes_journal(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, \
             patch.object(system_proxy, "JOURNAL_PATH", os.path.join(d, "journal")), \
             patch.object(system_proxy, "_run", side_effect=[(True, ""), (False, "boom")]), \
             patch.object(system_proxy, "restore", return_value=(True, "")) as restore:
            ok, err, desired = system_proxy.apply_transaction(
                "127.0.0.1", 8888, ["localhost"], self.original,
            )
            self.assertFalse(ok)
            self.assertIn("boom", err)
            self.assertIsNone(desired)
            restore.assert_called_once_with(self.original)
            self.assertFalse(os.path.exists(system_proxy.JOURNAL_PATH))

    def test_success_keeps_journal_until_release(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, \
             patch.object(system_proxy, "JOURNAL_PATH", os.path.join(d, "journal")), \
             patch.object(system_proxy, "_run", return_value=(True, "")):
            ok, err, desired = system_proxy.apply_transaction(
                "127.0.0.1", 8888, ["localhost"], self.original,
            )
            self.assertTrue(ok, err)
            self.assertTrue(os.path.exists(system_proxy.JOURNAL_PATH))
            with patch.object(system_proxy, "snapshot_services", return_value=desired), \
                 patch.object(system_proxy, "restore", return_value=(True, "")):
                released, release_err = system_proxy.release_transaction(self.original, desired)
            self.assertTrue(released, release_err)
            self.assertFalse(os.path.exists(system_proxy.JOURNAL_PATH))

    def test_rollback_failure_keeps_recovery_journal(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, \
             patch.object(system_proxy, "JOURNAL_PATH", os.path.join(d, "journal")), \
             patch.object(system_proxy, "_run", side_effect=[(True, ""), (False, "apply")]), \
             patch.object(system_proxy, "restore", return_value=(False, "rollback")):
            ok, err, desired = system_proxy.apply_transaction(
                "127.0.0.1", 8888, ["localhost"], self.original,
            )
            self.assertFalse(ok)
            self.assertIn("rollback failed", err)
            self.assertIsNotNone(desired)
            self.assertTrue(os.path.exists(system_proxy.JOURNAL_PATH))


class TestRunHelper(unittest.TestCase):
    def test_run_subprocess_exception_returns_false(self):
        with patch("subprocess.run", side_effect=OSError("boom")):
            ok, err = system_proxy._run(["networksetup", "-x"])
        self.assertFalse(ok)
        self.assertIn("boom", err)

    def test_active_services_nonzero_returns_empty(self):
        with patch("subprocess.run", return_value=_completed(returncode=1)):
            self.assertEqual(system_proxy._active_services(), [])

    def test_get_subprocess_exception_returns_none(self):
        with patch("subprocess.run", side_effect=OSError("boom")):
            self.assertIsNone(system_proxy._get(["networksetup", "-x"]))


class TestSnapshotEdge(unittest.TestCase):
    def test_unreadable_service_skipped(self):
        with patch.object(system_proxy, "_get", return_value=None):
            state = system_proxy.snapshot_services(["Wi-Fi"])
        self.assertEqual(state, {})

    def test_restore_empty_state_is_noop(self):
        ok, err = system_proxy.restore({})
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_restore_command_error_collected(self):
        state = {"Wi-Fi": {"web": {"enabled": False}, "secure": {"enabled": False},
                           "bypass": []}}
        with patch.object(system_proxy, "_run", return_value=(False, "denied")):
            ok, err = system_proxy.restore(state)
        self.assertFalse(ok)
        self.assertIn("denied", err)


class TestStateMatches(unittest.TestCase):
    def test_service_set_mismatch(self):
        self.assertFalse(system_proxy._state_matches(
            {"A": {}}, {"B": {}}))

    def test_kind_mismatch(self):
        current = {"Wi-Fi": {"web": {"enabled": True}, "secure": {"enabled": False}, "bypass": []}}
        expected = {"Wi-Fi": {"web": {"enabled": False}, "secure": {"enabled": False}, "bypass": []}}
        self.assertFalse(system_proxy._state_matches(current, expected))

    def test_bypass_mismatch(self):
        current = {"Wi-Fi": {"web": {}, "secure": {}, "bypass": ["a"]}}
        expected = {"Wi-Fi": {"web": {}, "secure": {}, "bypass": ["b"]}}
        self.assertFalse(system_proxy._state_matches(current, expected))


class TestApplyTransactionEdge(unittest.TestCase):
    def test_empty_original_returns_false(self):
        ok, err, desired = system_proxy.apply_transaction("h", 80, [], {})
        self.assertFalse(ok)
        self.assertIsNone(desired)

    def test_journal_write_failure_returns_false(self):
        original = {"Wi-Fi": {"web": {}, "secure": {}, "bypass": []}}
        with patch.object(system_proxy, "_write_journal", return_value=False):
            ok, err, desired = system_proxy.apply_transaction("h", 80, [], original)
        self.assertFalse(ok)
        self.assertIn("journal", err)


class TestReleaseTransaction(unittest.TestCase):
    def test_external_change_refuses_restore(self):
        original = {"Wi-Fi": {"web": {}, "secure": {}, "bypass": []}}
        desired = {"Wi-Fi": {"web": {"enabled": True}, "secure": {"enabled": True}, "bypass": []}}
        # Current state differs from desired -> refuse
        with patch.object(system_proxy, "snapshot_services",
                          return_value={"Wi-Fi": {"web": {"enabled": False},
                                                  "secure": {"enabled": False}, "bypass": []}}):
            ok, err = system_proxy.release_transaction(original, desired)
        self.assertFalse(ok)
        self.assertIn("changed externally", err)


class TestRecoverStaleTransaction(unittest.TestCase):
    def test_no_journal_returns_true(self):
        with patch.object(system_proxy.os.path, "exists", return_value=False):
            ok, err = system_proxy.recover_stale_transaction()
        self.assertTrue(ok)

    def test_invalid_journal_returns_false(self):
        m = unittest.mock.mock_open(read_data="{not valid json")
        with patch.object(system_proxy.os.path, "exists", return_value=True), \
             patch.object(system_proxy, "open", m):
            ok, err = system_proxy.recover_stale_transaction()
        self.assertFalse(ok)
        self.assertIn("invalid", err)


class TestWriteRemoveJournal(unittest.TestCase):
    def test_write_journal_failure_returns_false(self):
        """_write_journal delegates to config_store.atomic_write; when that
        fails (returns False), _write_journal surfaces False instead of
        raising — caller (apply_transaction) treats False as the error."""
        with patch("system_proxy.config_store.atomic_write", return_value=False) as aw:
            ok = system_proxy._write_journal({}, {})
        self.assertFalse(ok)
        aw.assert_called_once()

    def test_write_journal_success_returns_true(self):
        with patch("system_proxy.config_store.atomic_write", return_value=True):
            self.assertTrue(system_proxy._write_journal({}, {}))

    def test_write_journal_serializes_payload(self):
        """Payload is JSON with version + original + desired (verifies shape)."""
        captured = {}

        def fake_atomic_write(path, text, **kw):
            captured["path"] = path
            captured["text"] = text
            return True

        with patch("system_proxy.config_store.atomic_write",
                   side_effect=fake_atomic_write):
            system_proxy._write_journal(
                {"Wi-Fi": {"bypass": []}},
                {"Wi-Fi": {"web": {"enabled": True}}},
            )
        import json as _json
        payload = _json.loads(captured["text"])
        self.assertEqual(payload["version"], 1)
        self.assertEqual(payload["original"], {"Wi-Fi": {"bypass": []}})
        self.assertEqual(payload["desired"],
                         {"Wi-Fi": {"web": {"enabled": True}}})

    def test_remove_journal_missing_file_swallowed(self):
        with patch("os.unlink", side_effect=FileNotFoundError):
            system_proxy._remove_journal()  # should not raise


if __name__ == "__main__":
    unittest.main()
