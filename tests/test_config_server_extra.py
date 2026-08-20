"""Tests for config_server.py — sp save pipeline, handler dispatch."""
import os
import unittest

from services import config_server
from mpconf import config_store
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

    def test_auth_url_removed_and_url_carries_no_secret(self):
        """issue #10：auth_url 已删除；url 本身不含任何凭证。"""
        cs = config_server.ConfigServer(port=9999)
        self.assertFalse(hasattr(cs, "auth_url"))
        self.assertNotIn("token", cs.url)

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

