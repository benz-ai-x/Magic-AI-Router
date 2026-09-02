"""稳定身份（issue #8）：凭证只以不可变 id 寻址.

S1 —— keychain account 优先 tunnel["id"]（tunnel:<id>）；无 id 时回退
legacy user@host:port（只读兼容，供迁移期读取旧 secret）。
"""
import unittest
from unittest.mock import patch

from shared import keychain


def _tun(**kw):
    return {"ssh_user": "u", "ssh_host": "h", "ssh_port": 22, **kw}


class TestIdAddressing(unittest.TestCase):
    def test_account_prefers_stable_id(self):
        self.assertEqual(
            keychain._account(_tun(id="t-abc123")), "tunnel:t-abc123")

    def test_legacy_account_when_no_id(self):
        self.assertEqual(
            keychain._account(_tun()), "u@h:22")

    def test_get_password_falls_back_to_legacy_for_migration_reads(self):
        calls = []
        def fake_copy(query, _):
            calls.append(query.get(keychain.Security.kSecAttrAccount))
            if len(calls) == 1:
                return (keychain.Security.errSecItemNotFound, None)
            return (keychain.Security.errSecSuccess, b"legacy-secret")
        with patch.object(keychain.Security, "SecItemCopyMatching",
                          side_effect=fake_copy):
            got = keychain.get_password(_tun(id="t-new"))
        self.assertEqual(got, "legacy-secret")
        self.assertEqual(calls, ["tunnel:t-new", "u@h:22"])

    def test_set_password_writes_id_account_only(self):
        written = []
        def fake_add(attrs, _):
            written.append(attrs.get(keychain.Security.kSecAttrAccount))
            return keychain.Security.errSecSuccess
        with patch.object(keychain.Security, "SecItemAdd",
                          side_effect=fake_add), \
             patch.object(keychain.Security, "SecItemDelete",
                          return_value=keychain.Security.errSecSuccess):
            ok = keychain.set_password(_tun(id="t-x"), "pw")
        self.assertTrue(ok)
        self.assertEqual(written, ["tunnel:t-x"])

    def test_delete_removes_id_and_legacy_accounts(self):
        deleted = []
        with patch.object(keychain.Security, "SecItemDelete",
                          side_effect=lambda q: deleted.append(
                              q.get(keychain.Security.kSecAttrAccount)) or 0):
            keychain.delete_password(_tun(id="t-x"))
        self.assertEqual(sorted(deleted), ["tunnel:t-x", "u@h:22"],
                         "删除清两端：新 id 账户 + 遗留 legacy 账户")


if __name__ == "__main__":
    unittest.main()
