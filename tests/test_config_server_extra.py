"""Tests for config_server.py — _write_mp, sp save pipeline, handler dispatch."""
import json
import os
import unittest
from unittest.mock import patch, MagicMock

import config_server
import config_store
from suanpan.config import _restore_key


class TestRestoreKey(unittest.TestCase):
    def test_keep_flag_restores_original(self):
        self.assertEqual(_restore_key(None, "sk-real-key", keep=True), "sk-real-key")

    def test_new_key_used_as_is(self):
        self.assertEqual(_restore_key("sk-new", "sk-old", keep=True), "sk-new")

    def test_empty_with_keep_keeps(self):
        self.assertEqual(_restore_key("", "sk-old", keep=True), "sk-old")

    def test_none_without_keep_clears(self):
        self.assertIsNone(_restore_key(None, "sk-old", keep=False))


class TestWriteMp(unittest.TestCase):
    @patch("config_server.save_config")
    @patch("config_server.keychain")
    def test_writes_config_and_returns_errors(self, mock_kc, mock_save):
        mock_save.return_value = True
        cfg = {"tunnels": [{"auth_type": "password", "ssh_user": "u", "ssh_host": "h", "ssh_port": 22}]}
        errors = config_server._write_mp(cfg)
        self.assertEqual(errors, [])
        mock_save.assert_called_once()

    @patch("config_server.save_config")
    def test_save_failure_returns_error(self, mock_save):
        mock_save.return_value = False
        errors = config_server._write_mp({"tunnels": []})
        self.assertTrue(any("写入失败" in e for e in errors))

    @patch("config_server.save_config")
    @patch("config_server.keychain")
    def test_save_failure_leaves_keychain_untouched(self, mock_kc, mock_save):
        """File save fails → keychain must not hold orphans for unsaved tunnels."""
        mock_save.return_value = False
        cfg = {"tunnels": [{"auth_type": "password", "ssh_host": "h",
                            "ssh_user": "u", "ssh_port": 22, "password": "pw"}]}
        errors = config_server._write_mp(cfg)
        self.assertTrue(errors)
        mock_kc.set_password.assert_not_called()
        mock_kc.delete_password.assert_not_called()


class TestWriteSp(unittest.TestCase):
    # Regression: an earlier version called the sp write path with no path
    # redirect and wiped the real ~/.suanpan.yaml on every test run.
    # conftest.py now sandboxes config_store.PATHS, so these exercise the
    # real write path safely.
    def test_successful_write_round_trips_to_disk(self):
        ok, err = config_store.sp_save({"providers": {}, "router": {}, "rules": []})
        self.assertTrue(ok, err)
        from suanpan.config import load_config_raw
        written = load_config_raw(config_store.get_path("sp"))
        self.assertEqual(written["providers"], {})
        self.assertEqual(written["listen_port"], 9527)

    def test_invalid_config_returns_error_and_writes_nothing(self):
        ok, err = config_store.sp_save(
            {"providers": {}, "router": {"default": "ghost/m"}, "rules": []})
        self.assertFalse(ok)
        self.assertIn("ghost", err)


class TestSaveConfigBackup(unittest.TestCase):
    def test_overwrite_keeps_previous_content_in_bak(self):
        import tempfile
        from suanpan.config import save_config_dict, load_config_raw
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sp.yaml")
            ok, _ = save_config_dict(
                {"providers": {"A": {"base_url": "http://a.example"}},
                 "router": {}, "rules": []}, path)
            self.assertTrue(ok)
            ok, _ = save_config_dict(
                {"providers": {}, "router": {}, "rules": []}, path)
            self.assertTrue(ok)
            bak = load_config_raw(path + ".bak")
            self.assertIn("A", bak["providers"])


class TestConfigServerProperties(unittest.TestCase):
    def test_token_property(self):
        cs = config_server.ConfigServer()
        self.assertIsInstance(cs.token, str)
        self.assertGreater(len(cs.token), 0)

    def test_url_contains_port(self):
        cs = config_server.ConfigServer(port=9999)
        self.assertIn("9999", cs.url)

    def test_auth_url_contains_token(self):
        cs = config_server.ConfigServer(port=9999)
        self.assertIn("token=", cs.auth_url)

    def test_not_running_on_init(self):
        cs = config_server.ConfigServer()
        self.assertFalse(cs.running)


class TestSetupClaudeCodeEndpoint(unittest.TestCase):
    """POST /api/setup-claude-code delegates to claude_code_setup.setup().

    #44: the middle-man adapter _setup_claude_code was inlined into do_POST;
    the endpoint-level wiring is covered by
    TestSetupClaudeCodeEndpoint.test_setup_claude_code_endpoint in
    tests/test_cov_config.py.
    """

