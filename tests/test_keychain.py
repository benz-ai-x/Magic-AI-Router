"""Tests for keychain.py — macOS Keychain via Security framework.

SECURITY contract (#40): passwords must never appear in a subprocess argv
(the old `security -w <pw>` CLI path was visible in `ps`).  The module now
uses SecItemAdd/SecItemCopyMatching/SecItemDelete directly.
"""
import unittest
from unittest.mock import patch, MagicMock

from sysctl import keychain
_TUNNEL = {"ssh_user": "u", "ssh_host": "h", "ssh_port": 22, "auth_type": "password"}


class TestAccountKey(unittest.TestCase):
    def test_account_format(self):
        self.assertEqual(keychain._account(_TUNNEL), "u@h:22")

    def test_account_defaults(self):
        self.assertEqual(keychain._account({}), "@:22")


def _mock_security():
    """A MagicMock shaped like the real pyobjc Security module."""
    sec = MagicMock()
    sec.kSecClass = "kSecClass"
    sec.kSecClassGenericPassword = "genp"
    sec.kSecAttrService = "svc"
    sec.kSecAttrAccount = "acct"
    sec.kSecValueData = "data"
    sec.kSecReturnData = "rtndata"
    sec.kSecMatchLimit = "ml"
    sec.kSecMatchLimitOne = "one"
    sec.errSecSuccess = 0
    sec.SecItemAdd.return_value = (0, MagicMock())
    sec.SecItemCopyMatching.return_value = (0, b"pw")
    sec.SecItemDelete.return_value = 0
    return sec


class TestSetPassword(unittest.TestCase):
    @patch("sysctl.keychain.Security", new_callable=_mock_security)
    @patch("subprocess.run")
    def test_writes_via_security_framework_without_subprocess(self, mock_run, sec):
        """The password must reach the keychain as data, never as argv."""
        self.assertTrue(keychain.set_password(_TUNNEL, "secret"))
        mock_run.assert_not_called()
        attrs = sec.SecItemAdd.call_args[0][0]
        self.assertEqual(attrs[sec.kSecAttrService], keychain.SERVICE)
        self.assertEqual(attrs[sec.kSecAttrAccount], "u@h:22")
        self.assertEqual(attrs[sec.kSecValueData], b"secret")

    @patch("sysctl.keychain.Security", new_callable=_mock_security)
    def test_deletes_existing_entry_before_add(self, sec):
        keychain.set_password(_TUNNEL, "secret")
        self.assertIn(sec.kSecClassGenericPassword,
                      sec.SecItemDelete.call_args[0][0].values())

    @patch("sysctl.keychain.Security", new_callable=_mock_security)
    def test_add_failure_returns_false(self, sec):
        sec.SecItemAdd.return_value = (-1, MagicMock())
        with self.assertLogs("magic-proxy.keychain", level="WARNING"):
            self.assertFalse(keychain.set_password(_TUNNEL, "secret"))

    @patch("sysctl.keychain.Security", new_callable=_mock_security)
    def test_exception_returns_false_without_leaking_password(self, sec):
        sec.SecItemAdd.side_effect = RuntimeError("boom")
        secret = "leak-me-123"
        with self.assertLogs("magic-proxy.keychain", level="WARNING") as cm:
            self.assertFalse(keychain.set_password(_TUNNEL, secret))
        self.assertNotIn(secret, "\n".join(cm.output))

    def test_no_host_returns_false(self):
        self.assertFalse(keychain.set_password({"ssh_host": ""}, "pw"))


class TestGetPassword(unittest.TestCase):
    @patch("sysctl.keychain.Security", new_callable=_mock_security)
    def test_success(self, sec):
        sec.SecItemCopyMatching.return_value = (0, b"mypw")
        self.assertEqual(keychain.get_password(_TUNNEL), "mypw")

    @patch("sysctl.keychain.Security", new_callable=_mock_security)
    def test_not_found_returns_empty(self, sec):
        sec.SecItemCopyMatching.return_value = (-25300, None)  # errSecItemNotFound
        self.assertEqual(keychain.get_password(_TUNNEL), "")

    def test_no_host_returns_empty(self):
        self.assertEqual(keychain.get_password({"ssh_host": ""}), "")


class TestDeletePassword(unittest.TestCase):
    @patch("sysctl.keychain.Security", new_callable=_mock_security)
    def test_success(self, sec):
        keychain.delete_password(_TUNNEL)
        self.assertIn(sec.kSecClassGenericPassword,
                      sec.SecItemDelete.call_args[0][0].values())

    @patch("sysctl.keychain.Security", new_callable=_mock_security)
    def test_not_found_is_silent(self, sec):
        sec.SecItemDelete.return_value = -25300
        keychain.delete_password(_TUNNEL)  # should not raise

    @patch("sysctl.keychain.Security", new_callable=_mock_security)
    def test_real_failure_reports_false(self, sec):
        """#69 R7：SecItemDelete 其他非零状态（真实失败）如实返回 False
        ——不再恒报 True。"""
        sec.SecItemDelete.return_value = -26276  # 非 0 非 NotFound
        self.assertFalse(keychain.delete_password(_TUNNEL))

    def test_no_host_is_noop(self):
        keychain.delete_password({"ssh_host": ""})  # should not raise
