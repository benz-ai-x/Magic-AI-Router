"""Tests for ca_trust module (ADR-022 Task 4 AC-3: CA trust detection +
first-run guidance). Trust-state detection and the privileged trust command
are pure functions mocked at the subprocess boundary (same convention as
test_system_proxy.py) -- the PyObjC guide window itself is exercised via
its testable decision function, attempt_trust(), not GUI instantiation.

SIT boundary (product-lead directive): automated tests never invoke the
REAL `security add-trusted-cert` -- it needs interactive macOS admin auth
and would persist a change to the real trust store. Every test here mocks
subprocess.run/Popen.
"""
import os
import subprocess
import unittest
from unittest.mock import patch, MagicMock

from capture import ca_trust
def _completed(stdout="", stderr="", returncode=0):
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.stdout = stdout
    cp.stderr = stderr
    cp.returncode = returncode
    return cp


class TestCaCertExists(unittest.TestCase):
    def test_true_when_file_present(self):
        with patch("os.path.exists", return_value=True):
            self.assertTrue(ca_trust.ca_cert_exists())

    def test_false_when_file_absent(self):
        with patch("os.path.exists", return_value=False):
            self.assertFalse(ca_trust.ca_cert_exists())


class TestIsTrusted(unittest.TestCase):
    def test_false_fast_when_cert_missing_no_subprocess_call(self):
        with patch("os.path.exists", return_value=False), \
             patch("subprocess.run") as run:
            self.assertFalse(ca_trust.is_trusted())
        run.assert_not_called()

    def test_true_when_verify_cert_exits_zero(self):
        with patch("os.path.exists", return_value=True), \
             patch("subprocess.run", return_value=_completed(returncode=0)):
            self.assertTrue(ca_trust.is_trusted())

    def test_false_when_verify_cert_exits_nonzero(self):
        with patch("os.path.exists", return_value=True), \
             patch("subprocess.run", return_value=_completed(returncode=1)):
            self.assertFalse(ca_trust.is_trusted())

    def test_false_on_timeout(self):
        with patch("os.path.exists", return_value=True), \
             patch("subprocess.run", side_effect=subprocess.TimeoutExpired("security", 10)):
            self.assertFalse(ca_trust.is_trusted())

    def test_false_on_oserror(self):
        with patch("os.path.exists", return_value=True), \
             patch("subprocess.run", side_effect=OSError("not found")):
            self.assertFalse(ca_trust.is_trusted())

    def test_exact_command_constructed(self):
        with patch("os.path.exists", return_value=True), \
             patch("subprocess.run", return_value=_completed(returncode=0)) as run:
            ca_trust.is_trusted()
        args = run.call_args[0][0]
        self.assertEqual(
            args, ["security", "verify-cert", "-c", ca_trust._ca_cert_path(), "-l"],
        )


class TestTrustCa(unittest.TestCase):
    def test_ok_when_returncode_zero(self):
        with patch("subprocess.run", return_value=_completed(returncode=0)):
            ok, err = ca_trust.trust_ca()
        self.assertTrue(ok)
        self.assertEqual(err, "")

    def test_fails_with_stderr_on_nonzero(self):
        with patch("subprocess.run", return_value=_completed(stderr="denied", returncode=1)):
            ok, err = ca_trust.trust_ca()
        self.assertFalse(ok)
        self.assertIn("denied", err)

    def test_fails_on_oserror(self):
        with patch("subprocess.run", side_effect=OSError("boom")):
            ok, err = ca_trust.trust_ca()
        self.assertFalse(ok)
        self.assertIn("boom", err)

    def test_exact_command_constructed_trusts_only_public_cert(self):
        """Global Constraint 3: never touch the private key -- only the
        public mitmproxy-ca-cert.pem is ever passed to security."""
        with patch("subprocess.run", return_value=_completed(returncode=0)) as run:
            ca_trust.trust_ca()
        args = run.call_args[0][0]
        expected_keychain = ca_trust._login_keychain()
        self.assertEqual(
            args,
            ["security", "add-trusted-cert", "-d", "-r", "trustRoot",
             "-k", expected_keychain, ca_trust._ca_cert_path()],
        )
        self.assertNotIn("mitmproxy-ca.pem", " ".join(args))  # private key never referenced


class TestRevealCaCert(unittest.TestCase):
    def test_opens_finder_reveal(self):
        with patch("subprocess.Popen") as popen:
            ok = ca_trust.reveal_ca_cert()
        self.assertTrue(ok)
        popen.assert_called_once_with(["open", "-R", ca_trust._ca_cert_path()])

    def test_returns_false_on_oserror(self):
        with patch("subprocess.Popen", side_effect=OSError("no open")):
            ok = ca_trust.reveal_ca_cert()
        self.assertFalse(ok)


class TestAttemptTrust(unittest.TestCase):
    """attempt_trust() is the testable decision function doTrust_ delegates
    to -- exercises the full routing logic without touching PyObjC."""

    def test_success_path_calls_trust_then_reverifies(self):
        with patch.object(ca_trust, "trust_ca", return_value=(True, "")) as t, \
             patch.object(ca_trust, "is_trusted", return_value=True) as v:
            ok, err = ca_trust.attempt_trust()
        self.assertTrue(ok)
        self.assertEqual(err, "")
        t.assert_called_once()
        v.assert_called_once()

    def test_trust_command_failure_short_circuits_before_reverify(self):
        with patch.object(ca_trust, "trust_ca", return_value=(False, "user cancelled")), \
             patch.object(ca_trust, "is_trusted") as v:
            ok, err = ca_trust.attempt_trust()
        self.assertFalse(ok)
        self.assertIn("user cancelled", err)
        v.assert_not_called()

    def test_trust_reports_ok_but_reverify_still_false(self):
        # Defense in depth: don't trust add-trusted-cert's own exit code alone.
        with patch.object(ca_trust, "trust_ca", return_value=(True, "")), \
             patch.object(ca_trust, "is_trusted", return_value=False):
            ok, err = ca_trust.attempt_trust()
        self.assertFalse(ok)
        self.assertTrue(err)  # non-empty explanatory message


if __name__ == "__main__":
    unittest.main()
