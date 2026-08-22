"""Tests for config_server.py — sp save pipeline, handler dispatch."""
import unittest

from services import config_server
from mpconf import config_store
from mpconf.provider_auth import restore_masked_key as _restore_key


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
    # conftest.py now sandboxes config_store.PATHS — #46 后 sp 写径唯一
    # 归宿 ConfigStateStore，经 PATHS 默认键提交即只触沙箱。
    def test_commit_via_registry_round_trips(self):
        from mpconf.config_state import ConfigStateStore
        store = ConfigStateStore()
        plan = store.prepare(sp={"providers": {}})
        self.assertTrue(plan.ok, plan.errors)
        self.assertTrue(store.commit(plan).ok)
        from suanpan.config import load_config
        cfg = load_config(config_store.get_path("sp"))
        self.assertEqual(cfg.providers, {})
        self.assertEqual(cfg.listen_port, 9527)


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

