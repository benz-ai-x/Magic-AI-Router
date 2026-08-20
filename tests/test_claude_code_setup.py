"""Tests for claude_code_setup.py — 提取自 config_server._setup_claude_code。

Tests use config_store.PATHS["claude_settings"] redirect (never real
~/.claude/settings.json) and exercise atomic_write via the real disk path.
"""
import json
import os
import stat
import tempfile
import unittest
from unittest.mock import patch

from services import claude_code_setup
from mpconf import config_store
class TestSetupClaudeCode(unittest.TestCase):
    """setup() writes ~/.claude/settings.json env block via atomic_write."""

    def _run_with_temp_settings(self, existing_settings=None, roles=None, sp=None):
        """Run setup() against a redirected PATHS["claude_settings"].

        sp feeds the roles derivation (sp_load_raw).  Returns
        (result_dict, written_settings_dict, settings_path).
        """
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            if existing_settings is not None:
                with open(settings_path, "w") as f:
                    json.dump(existing_settings, f)
            with patch("services.claude_code_setup.config_store.suanpan_listen",
                       return_value="127.0.0.1:9527"), \
                 patch("services.claude_code_setup.config_store.sp_load_raw",
                       return_value=sp if sp is not None else {}), \
                 patch.dict(config_store.PATHS, {"claude_settings": settings_path}):
                result = claude_code_setup.setup(roles=roles)
            with open(settings_path) as f:
                written = json.load(f)
            return result, written, settings_path

    def test_first_config_writes_all_env_vars(self):
        result, settings, _ = self._run_with_temp_settings()
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "added")
        env = settings["env"]
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:9527")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "mage-router")
        self.assertEqual(env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"], "1")

    def test_reports_replaced_auth_without_echoing_token(self):
        """#2: replacing a user-set ANTHROPIC_BASE_URL/AUTH_TOKEN is by
        design, but the result must say so — and never echo the old token."""
        existing = {
            "env": {
                "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
                "ANTHROPIC_AUTH_TOKEN": "sk-user-real",
            }
        }
        result, _, _ = self._run_with_temp_settings(existing)
        self.assertEqual(result["action"], "added")
        self.assertIn("https://api.anthropic.com", result["msg"])
        self.assertIn("ANTHROPIC_AUTH_TOKEN", result["msg"])
        self.assertNotIn("sk-user-real", result["msg"])

    def test_idempotent_when_no_roles_and_fully_configured(self):
        existing = {
            "env": {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:9527",
                "ANTHROPIC_AUTH_TOKEN": "mage-router",
                "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            }
        }
        result, _, _ = self._run_with_temp_settings(existing)
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "already")

    def test_updates_when_base_url_set_but_compat_missing(self):
        existing = {
            "env": {
                "ANTHROPIC_BASE_URL": "http://127.0.0.1:9527",
                "ANTHROPIC_AUTH_TOKEN": "mage-router",
            }
        }
        result, settings, _ = self._run_with_temp_settings(existing)
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "added")
        self.assertEqual(settings["env"]["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"], "1")

    def test_preserves_existing_env_vars(self):
        existing = {
            "env": {
                "API_TIMEOUT_MS": "3000000",
                "CLAUDE_CODE_ATTRIBUTION_HEADER": "0",
            }
        }
        result, settings, _ = self._run_with_temp_settings(existing)
        self.assertTrue(result["ok"])
        env = settings["env"]
        self.assertEqual(env["API_TIMEOUT_MS"], "3000000")
        self.assertEqual(env["CLAUDE_CODE_ATTRIBUTION_HEADER"], "0")

    def test_removes_model_overrides(self):
        existing = {
            "env": {
                "ANTHROPIC_MODEL": "claude-sonnet-5",
                "CLAUDE_CODE_SUBAGENT_MODEL": "claude-haiku-4-5",
            }
        }
        result, settings, _ = self._run_with_temp_settings(existing)
        self.assertTrue(result["ok"])
        env = settings["env"]
        self.assertNotIn("ANTHROPIC_MODEL", env)
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL", env)

    def test_returns_failed_on_error(self):
        with patch("services.claude_code_setup.config_store.sp_load_raw",
                   side_effect=FileNotFoundError("nope")), \
             patch.dict(config_store.PATHS,
                        {"claude_settings": "/nonexistent/path/settings.json"}):
            result = claude_code_setup.setup()
        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "failed")

    def test_uses_suanpan_listen(self):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with patch("services.claude_code_setup.config_store.suanpan_listen",
                       return_value="127.0.0.1:8888"), \
                 patch("services.claude_code_setup.config_store.sp_load_raw",
                       return_value={}), \
                 patch.dict(config_store.PATHS, {"claude_settings": settings_path}):
                result = claude_code_setup.setup()
            with open(settings_path) as f:
                written = json.load(f)
        self.assertTrue(result["ok"])
        self.assertEqual(
            written["env"]["ANTHROPIC_BASE_URL"], "http://127.0.0.1:8888")

    def test_preserves_unmanaged_model_env_vars(self):
        """Wipe-set is derived from _ROLES: unrelated model vars survive."""
        existing = {"env": {"ANTHROPIC_DEFAULT_ORACLE_MODEL": "oracle/1"}}
        result, settings, _ = self._run_with_temp_settings(existing)
        self.assertTrue(result["ok"])
        self.assertEqual(settings["env"]["ANTHROPIC_DEFAULT_ORACLE_MODEL"], "oracle/1")


class TestSetupClaudeCodeAtomicWrite(unittest.TestCase):
    """setup() uses config_store.atomic_write (0600 + .bak backup)."""

    def test_written_file_has_0600_mode(self):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with patch("services.claude_code_setup.config_store.sp_load_raw",
                       return_value={"listen": "127.0.0.1:9527"}), \
                 patch.dict(config_store.PATHS, {"claude_settings": settings_path}):
                claude_code_setup.setup()
            self.assertEqual(
                stat.S_IMODE(os.stat(settings_path).st_mode), 0o600)

    def test_existing_settings_backed_up_before_overwrite(self):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            original = {"env": {"FOO": "bar"}}
            with open(settings_path, "w") as f:
                json.dump(original, f)
            with patch("services.claude_code_setup.config_store.sp_load_raw",
                       return_value={"listen": "127.0.0.1:9527"}), \
                 patch.dict(config_store.PATHS, {"claude_settings": settings_path}):
                claude_code_setup.setup()
            bak = settings_path + ".bak"
            self.assertTrue(os.path.exists(bak))
            with open(bak) as f:
                bak_content = json.load(f)
            self.assertEqual(bak_content, original)

    def test_no_backup_when_no_prior_file(self):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with patch("services.claude_code_setup.config_store.sp_load_raw",
                       return_value={"listen": "127.0.0.1:9527"}), \
                 patch.dict(config_store.PATHS, {"claude_settings": settings_path}):
                claude_code_setup.setup()
            self.assertFalse(os.path.exists(settings_path + ".bak"))

    def test_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "nested", "dir", "settings.json")
            with patch("services.claude_code_setup.config_store.sp_load_raw",
                       return_value={"listen": "127.0.0.1:9527"}), \
                 patch.dict(config_store.PATHS, {"claude_settings": settings_path}):
                claude_code_setup.setup()
            self.assertTrue(os.path.exists(settings_path))

    def test_rerun_preserves_original_backup(self):
        """#2: the .bak made on the first write must survive later re-runs —
        a second setup (e.g. changed model mappings) must not overwrite it
        with the already-gateway-configured state."""
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            original = {"env": {"ANTHROPIC_BASE_URL": "https://api.anthropic.com",
                                "ANTHROPIC_AUTH_TOKEN": "sk-user-real",
                                "FOO": "bar"}}
            with open(settings_path, "w") as f:
                json.dump(original, f)
            with patch("services.claude_code_setup.config_store.suanpan_listen",
                       return_value="127.0.0.1:9527"), \
                 patch("services.claude_code_setup.config_store.sp_load_raw",
                       return_value={}), \
                 patch.dict(config_store.PATHS, {"claude_settings": settings_path}):
                first = claude_code_setup.setup()
                self.assertEqual(first["action"], "added")
                # Re-run with explicit roles → different model env → rewrites.
                second = claude_code_setup.setup(
                    roles={"default": {"model": "glm/glm-4.6", "ctx_1m": False}})
                self.assertEqual(second["action"], "added")
            with open(settings_path + ".bak") as f:
                bak_content = json.load(f)
            self.assertEqual(bak_content, original)


class TestDefaultRolesFromSp(unittest.TestCase):
    """_default_roles_from_sp() derives role mappings from Suanpan config."""

    def test_empty_config(self):
        self.assertEqual(claude_code_setup._default_roles_from_sp({}), {})

    def test_full_rules(self):
        sp = {
            "rules": [
                {"match_prefix": "claude-opus", "route_to": "GLM_MAX/glm-5.2"},
                {"match_prefix": "claude-sonnet", "route_to": "DeepSeek/deepseek-v4-flash"},
                {"match_prefix": "claude-haiku", "route_to": "KIMI/k3-256k"},
                {"match_prefix": "claude-fable", "route_to": "KIMI/k3"},
            ],
            "router": {"default": "GLM_MAX/glm-5.2"},
        }
        roles = claude_code_setup._default_roles_from_sp(sp)
        self.assertEqual(roles["opus"]["model"], "GLM_MAX/glm-5.2")
        self.assertEqual(roles["sonnet"]["model"], "DeepSeek/deepseek-v4-flash")
        self.assertEqual(roles["haiku"]["model"], "KIMI/k3-256k")
        self.assertEqual(roles["fable"]["model"], "KIMI/k3")
        self.assertEqual(roles["default"]["model"], "GLM_MAX/glm-5.2")
        # #43: subagents use the cheap haiku tier, not the default route.
        self.assertEqual(roles["subagent"]["model"], "KIMI/k3-256k")
        # All default to 1M enabled
        for r in roles.values():
            self.assertTrue(r["ctx_1m"])

    def test_missing_rule_falls_back_to_default(self):
        sp = {
            "rules": [{"match_prefix": "claude-opus", "route_to": "GLM_MAX/glm-5.2"}],
            "router": {"default": "DeepSeek/deepseek-v4-pro"},
        }
        roles = claude_code_setup._default_roles_from_sp(sp)
        self.assertEqual(roles["sonnet"]["model"], "DeepSeek/deepseek-v4-pro")

    def test_specific_sonnet_rule_seeds_sonnet_role(self):
        """#42: a rule like claude-sonnet-4-5 routes claude-sonnet-4-5-*
        models in the router; the derived sonnet role must use it, not
        silently fall back to router.default."""
        sp = {
            "rules": [
                {"match_prefix": "claude-sonnet-4-5",
                 "route_to": "DeepSeek/deepseek-v4-pro"},
            ],
            "router": {"default": "GLM_MAX/glm-5.2"},
        }
        roles = claude_code_setup._default_roles_from_sp(sp)
        self.assertEqual(roles["sonnet"]["model"], "DeepSeek/deepseek-v4-pro")

    def test_broader_rule_listed_first_wins(self):
        """#42: rule order mirrors the router's first-hit. A broad 'claude'
        rule listed before 'claude-sonnet' routes claude-sonnet-* models in
        the router, so the derived sonnet role takes the broad rule's target."""
        sp = {
            "rules": [
                {"match_prefix": "claude", "route_to": "KIMI/k3"},
                {"match_prefix": "claude-sonnet", "route_to": "DeepSeek/deepseek-v4-flash"},
            ],
            "router": {"default": "GLM_MAX/glm-5.2"},
        }
        roles = claude_code_setup._default_roles_from_sp(sp)
        self.assertEqual(roles["sonnet"]["model"], "KIMI/k3")

    def test_specific_rule_listed_first_wins(self):
        """#42: a more specific rule listed first takes the tier role, same
        as the router would for claude-sonnet-4-5-* models."""
        sp = {
            "rules": [
                {"match_prefix": "claude-sonnet-4-5", "route_to": "KIMI/k3"},
                {"match_prefix": "claude-sonnet", "route_to": "DeepSeek/deepseek-v4-flash"},
            ],
            "router": {"default": "GLM_MAX/glm-5.2"},
        }
        roles = claude_code_setup._default_roles_from_sp(sp)
        self.assertEqual(roles["sonnet"]["model"], "KIMI/k3")

    def test_legacy_35_sonnet_rule_does_not_seed_sonnet(self):
        """#42: a legacy claude-3-5-sonnet rule can never route a
        claude-sonnet* model (no prefix relation), so the sonnet role falls
        back to default — exactly what the router would do."""
        sp = {
            "rules": [
                {"match_prefix": "claude-3-5-sonnet",
                 "route_to": "anthropic/claude-sonnet-4-20250514"},
            ],
            "router": {"default": "DeepSeek/deepseek-v4-pro"},
        }
        roles = claude_code_setup._default_roles_from_sp(sp)
        self.assertEqual(roles["sonnet"]["model"], "DeepSeek/deepseek-v4-pro")

    def test_subagent_uses_haiku_target(self):
        """#43: with a haiku rule, the subagent role derives from the cheap
        tier, decoupled from the default fallback."""
        sp = {
            "rules": [
                {"match_prefix": "claude-haiku", "route_to": "KIMI/k3-256k"},
            ],
            "router": {"default": "DeepSeek/deepseek-v4-pro"},
        }
        roles = claude_code_setup._default_roles_from_sp(sp)
        self.assertEqual(roles["subagent"]["model"], "KIMI/k3-256k")
        self.assertEqual(roles["default"]["model"], "DeepSeek/deepseek-v4-pro")

    def test_subagent_falls_back_to_default_without_haiku_rule(self):
        sp = {
            "rules": [{"match_prefix": "claude-opus", "route_to": "GLM_MAX/glm-5.2"}],
            "router": {"default": "DeepSeek/deepseek-v4-pro"},
        }
        roles = claude_code_setup._default_roles_from_sp(sp)
        self.assertEqual(roles["subagent"]["model"], "DeepSeek/deepseek-v4-pro")

    def test_no_default_no_rules(self):
        sp = {"rules": []}
        roles = claude_code_setup._default_roles_from_sp(sp)
        self.assertEqual(roles, {})

    def test_default_roles_reads_current_config(self):
        sp = {
            "rules": [{"match_prefix": "claude-sonnet", "route_to": "KIMI/k3"}],
            "router": {"default": "GLM_MAX/glm-5.2"},
        }
        with patch("services.claude_code_setup.config_store.sp_load_raw", return_value=sp):
            data = claude_code_setup.default_roles()
        self.assertEqual(data["roles"]["sonnet"]["model"], "KIMI/k3")
        self.assertEqual(data["roles"]["opus"]["model"], "GLM_MAX/glm-5.2")

    def test_default_roles_carries_table_metadata(self):
        """#44: the payload carries order/labels/readonly so the UI needs no
        parallel role list (Python _ROLES is the single source of truth).
        The default role is rendered by a dedicated UI control, not the
        table, so it stays out of `order`."""
        with patch("services.claude_code_setup.config_store.sp_load_raw", return_value={}):
            data = claude_code_setup.default_roles()
        self.assertEqual(
            data["order"], ["opus", "sonnet", "fable", "haiku", "subagent"])
        self.assertEqual(data["labels"]["opus"], "Opus")
        self.assertEqual(data["labels"]["subagent"], "Subagent")
        self.assertEqual(data["readonly"], ["subagent"])
        self.assertNotIn("default", data["order"])
        self.assertEqual(set(data["labels"]), set(data["order"]))


class TestRolesToEnv(unittest.TestCase):
    """_roles_to_env() converts role dicts to Claude Code env vars."""

    def test_empty_roles(self):
        self.assertEqual(claude_code_setup._roles_to_env({}), {})

    def test_full_roles(self):
        roles = {
            "opus": {"model": "GLM_MAX/glm-5.2", "ctx_1m": True},
            "sonnet": {"model": "DeepSeek/deepseek-v4-flash", "ctx_1m": True},
            "fable": {"model": "KIMI/k3", "ctx_1m": False},
            "haiku": {"model": "KIMI/k3-256k", "ctx_1m": True},
            "subagent": {"model": "GLM_MAX/glm-5.2", "ctx_1m": True},
            "default": {"model": "GLM_PRO/glm-5.2", "ctx_1m": True},
        }
        env = claude_code_setup._roles_to_env(roles)
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "GLM_MAX/glm-5.2[1M]")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL_NAME"], "GLM_MAX/glm-5.2")
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "DeepSeek/deepseek-v4-flash[1M]")
        self.assertEqual(env["ANTHROPIC_DEFAULT_FABLE_MODEL"], "KIMI/k3")
        self.assertEqual(env["ANTHROPIC_DEFAULT_FABLE_MODEL_NAME"], "KIMI/k3")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "KIMI/k3-256k[1M]")
        self.assertEqual(env["CLAUDE_CODE_SUBAGENT_MODEL"], "GLM_MAX/glm-5.2[1M]")
        self.assertEqual(env["ANTHROPIC_MODEL"], "GLM_PRO/glm-5.2[1M]")

    def test_ctx_1m_disabled_no_suffix(self):
        roles = {"opus": {"model": "GLM_MAX/glm-5.2", "ctx_1m": False}}
        env = claude_code_setup._roles_to_env(roles)
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "GLM_MAX/glm-5.2")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL_NAME"], "GLM_MAX/glm-5.2")

    def test_custom_name_used_for_model_name_var(self):
        roles = {"opus": {"model": "GLM_MAX/glm-5.2", "name": "至尊模型", "ctx_1m": True}}
        env = claude_code_setup._roles_to_env(roles)
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "GLM_MAX/glm-5.2[1M]")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL_NAME"], "至尊模型")

    def test_blank_name_falls_back_to_model(self):
        roles = {"opus": {"model": "GLM_MAX/glm-5.2", "name": "  ", "ctx_1m": True}}
        env = claude_code_setup._roles_to_env(roles)
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL_NAME"], "GLM_MAX/glm-5.2")

    def test_already_suffixed_not_doubled(self):
        roles = {"opus": {"model": "GLM_MAX/glm-5.2[1M]", "ctx_1m": True}}
        env = claude_code_setup._roles_to_env(roles)
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "GLM_MAX/glm-5.2[1M]")

    def test_empty_model_skipped(self):
        roles = {"opus": {"model": "", "ctx_1m": True}, "default": {"model": "GLM_MAX/glm-5.2", "ctx_1m": True}}
        env = claude_code_setup._roles_to_env(roles)
        self.assertNotIn("ANTHROPIC_DEFAULT_OPUS_MODEL", env)
        self.assertIn("ANTHROPIC_MODEL", env)

    def test_non_dict_role_skipped(self):
        roles = {"opus": "not a dict", "default": {"model": "GLM_MAX/glm-5.2", "ctx_1m": True}}
        env = claude_code_setup._roles_to_env(roles)
        self.assertNotIn("ANTHROPIC_DEFAULT_OPUS_MODEL", env)
        self.assertIn("ANTHROPIC_MODEL", env)

    def test_subagent_no_model_name(self):
        """Subagent role should NOT produce a *_MODEL_NAME var."""
        roles = {"subagent": {"model": "GLM_MAX/glm-5.2", "ctx_1m": True}}
        env = claude_code_setup._roles_to_env(roles)
        self.assertIn("CLAUDE_CODE_SUBAGENT_MODEL", env)
        self.assertNotIn("CLAUDE_CODE_SUBAGENT_MODEL_NAME", env)

    def test_default_no_model_name(self):
        """Default role (ANTHROPIC_MODEL) should NOT produce a *_MODEL_NAME var."""
        roles = {"default": {"model": "GLM_MAX/glm-5.2", "ctx_1m": True}}
        env = claude_code_setup._roles_to_env(roles)
        self.assertIn("ANTHROPIC_MODEL", env)
        self.assertNotIn("ANTHROPIC_MODEL_NAME", env)


class TestSetupClaudeCodeWithRoles(unittest.TestCase):
    """setup() with explicit roles parameter."""

    def _run_with_roles(self, roles, existing_settings=None):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            if existing_settings is not None:
                with open(settings_path, "w") as f:
                    json.dump(existing_settings, f)
            with patch("services.claude_code_setup.config_store.sp_load_raw",
                       return_value={"listen": "127.0.0.1:9527"}), \
                 patch.dict(config_store.PATHS, {"claude_settings": settings_path}):
                result = claude_code_setup.setup(roles=roles)
            with open(settings_path) as f:
                written = json.load(f)
            return result, written

    def test_writes_explicit_roles(self):
        roles = {
            "opus": {"model": "GLM_MAX/glm-5.2", "ctx_1m": True},
            "sonnet": {"model": "DeepSeek/deepseek-v4-flash", "ctx_1m": False},
            "default": {"model": "GLM_PRO/glm-5.2", "ctx_1m": True},
        }
        result, settings = self._run_with_roles(roles)
        self.assertTrue(result["ok"])
        env = settings["env"]
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "GLM_MAX/glm-5.2[1M]")
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "DeepSeek/deepseek-v4-flash")
        self.assertEqual(env["ANTHROPIC_MODEL"], "GLM_PRO/glm-5.2[1M]")

    def test_explicit_roles_override_sp_derivation(self):
        """Explicit roles win over Suanpan routing rules."""
        sp = {
            "listen": "127.0.0.1:9527",
            "rules": [{"match_prefix": "claude-opus", "route_to": "GLM_MAX/glm-5.2"}],
            "router": {"default": "GLM_MAX/glm-5.2"},
        }
        roles = {"opus": {"model": "KIMI/k3", "ctx_1m": False}}
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with patch("services.claude_code_setup.config_store.sp_load_raw",
                       return_value=sp), \
                 patch.dict(config_store.PATHS, {"claude_settings": settings_path}):
                claude_code_setup.setup(roles=roles)
            with open(settings_path) as f:
                written = json.load(f)
        env = written["env"]
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "KIMI/k3")

    def test_overwrites_existing_model_vars(self):
        roles = {"default": {"model": "GLM_MAX/glm-5.2", "ctx_1m": True}}
        existing = {"env": {
            "ANTHROPIC_MODEL": "old-model",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "old-opus",
            "API_TIMEOUT_MS": "3000000",
        }}
        result, settings = self._run_with_roles(roles, existing)
        env = settings["env"]
        self.assertEqual(env["ANTHROPIC_MODEL"], "GLM_MAX/glm-5.2[1M]")
        self.assertNotIn("ANTHROPIC_DEFAULT_OPUS_MODEL", env)
        self.assertEqual(env["API_TIMEOUT_MS"], "3000000")

    def test_already_detects_match(self):
        roles = {"default": {"model": "GLM_MAX/glm-5.2", "ctx_1m": True}}
        existing = {"env": {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:9527",
            "ANTHROPIC_AUTH_TOKEN": "mage-router",
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            "ANTHROPIC_MODEL": "GLM_MAX/glm-5.2[1M]",
        }}
        result, _ = self._run_with_roles(roles, existing)
        self.assertEqual(result["action"], "already")

    def test_already_detects_mismatch(self):
        roles = {"default": {"model": "GLM_MAX/glm-5.2", "ctx_1m": True}}
        existing = {"env": {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:9527",
            "ANTHROPIC_AUTH_TOKEN": "mage-router",
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            "ANTHROPIC_MODEL": "WRONG/model",
        }}
        result, _ = self._run_with_roles(roles, existing)
        self.assertEqual(result["action"], "added")


class TestPreview(unittest.TestCase):
    """preview(): read-only dry run backing the settings-window confirm
    dialog (#3 验收 9). Must never write, must mask old tokens, and must
    agree with what setup() subsequently writes."""

    def _run_preview(self, roles=None, existing_settings=None):
        """preview() against a redirected PATHS; returns (result, dir, path).
        The caller inspects the file AFTER the context exited to prove
        nothing was written."""
        d = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        settings_path = os.path.join(d, "settings.json")
        if existing_settings is not None:
            with open(settings_path, "w") as f:
                json.dump(existing_settings, f)
        with patch("services.claude_code_setup.config_store.suanpan_listen",
                   return_value="127.0.0.1:9527"), \
             patch("services.claude_code_setup.config_store.sp_load_raw",
                   return_value={}), \
             patch.dict(config_store.PATHS, {"claude_settings": settings_path}):
            result = claude_code_setup.preview(roles=roles)
        return result, d, settings_path

    def test_fresh_install_no_file_no_write(self):
        result, d, settings_path = self._run_preview(
            roles={"default": {"model": "GLM_MAX/glm-5.2", "ctx_1m": True}})
        self.assertTrue(result["ok"])
        self.assertFalse(result["already"])
        self.assertFalse(result["exists"])
        self.assertEqual(result["target"], settings_path)
        self.assertEqual(os.listdir(d), [], "preview must not create files")
        self.assertFalse(result["backup"]["will"])
        self.assertIn("新建", result["backup"]["note"])
        # fixed trio all 新增, model mapping included
        by_key = {c["key"]: c for c in result["changes"]}
        self.assertEqual(by_key["ANTHROPIC_BASE_URL"]["action"], "add")
        self.assertEqual(by_key["ANTHROPIC_BASE_URL"]["new"],
                         "http://127.0.0.1:9527")
        self.assertNotIn("ANTHROPIC_DEFAULT_OPUS_MODEL", by_key,
                         "roles without opus add no opus row")
        self.assertEqual(by_key["ANTHROPIC_MODEL"]["new"],
                         "GLM_MAX/glm-5.2[1M]")

    def test_existing_non_gateway_file_unchanged_and_masked(self):
        existing = {"env": {
            "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
            "ANTHROPIC_AUTH_TOKEN": "sk-real-secret",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "old-model",
        }, "other": {"keep": True}}
        before = json.dumps(existing, sort_keys=True)
        result, d, settings_path = self._run_preview(
            roles={"opus": {"model": "GLM_MAX/glm-5.2", "ctx_1m": True}},
            existing_settings=existing)
        self.assertTrue(result["ok"])
        self.assertTrue(result["exists"])
        self.assertTrue(result["backup"]["will"])
        self.assertEqual(result["backup"]["path"], settings_path + ".bak")
        self.assertEqual(os.listdir(d), ["settings.json"],
                         "preview must not write or back up")
        with open(settings_path) as f:
            self.assertEqual(json.dumps(json.load(f), sort_keys=True), before,
                             "preview must not modify the file")
        by_key = {c["key"]: c for c in result["changes"]}
        self.assertEqual(by_key["ANTHROPIC_BASE_URL"]["old"],
                         "https://api.anthropic.com")
        self.assertEqual(by_key["ANTHROPIC_AUTH_TOKEN"]["old"],
                         "（已设置，不回显）", "real token must never be echoed")
        self.assertNotIn("sk-real-secret", json.dumps(result, ensure_ascii=False))
        self.assertEqual(by_key["ANTHROPIC_DEFAULT_OPUS_MODEL"]["action"],
                         "replace")
        self.assertEqual(by_key["ANTHROPIC_DEFAULT_OPUS_MODEL"]["old"],
                         "old-model")
        self.assertEqual(by_key["ANTHROPIC_DEFAULT_OPUS_MODEL"]["new"],
                         "GLM_MAX/glm-5.2[1M]")

    def test_owned_key_missing_from_new_roles_shows_remove(self):
        existing = {"env": {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:9527",
            "ANTHROPIC_AUTH_TOKEN": "mage-router",
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            "CLAUDE_CODE_SUBAGENT_MODEL": "GLM_PRO/glm-5.2[1M]",
        }}
        # roles without subagent → the stale subagent mapping must show as 移除
        result, _, _ = self._run_preview(
            roles={"default": {"model": "GLM_MAX/glm-5.2", "ctx_1m": True}},
            existing_settings=existing)
        by_key = {c["key"]: c for c in result["changes"]}
        self.assertEqual(by_key["CLAUDE_CODE_SUBAGENT_MODEL"]["action"], "remove")
        self.assertEqual(by_key["CLAUDE_CODE_SUBAGENT_MODEL"]["new"], None)

    def test_already_configured_reports_no_changes(self):
        existing = {"env": {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:9527",
            "ANTHROPIC_AUTH_TOKEN": "mage-router",
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            "ANTHROPIC_MODEL": "GLM_MAX/glm-5.2[1M]",
        }}
        result, _, _ = self._run_preview(
            roles={"default": {"model": "GLM_MAX/glm-5.2", "ctx_1m": True}},
            existing_settings=existing)
        self.assertTrue(result["already"])
        self.assertEqual(result["changes"], [])
        self.assertFalse(result["backup"]["will"])

    def test_re_sync_keeps_first_backup_no_new_backup(self):
        # settings already point at the gateway → first_write False → 不覆盖既有 .bak
        existing = {"env": {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:9527",
            "ANTHROPIC_AUTH_TOKEN": "mage-router",
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            "ANTHROPIC_MODEL": "OLD/model",
        }}
        result, _, _ = self._run_preview(
            roles={"default": {"model": "GLM_MAX/glm-5.2", "ctx_1m": True}},
            existing_settings=existing)
        self.assertFalse(result["already"])
        self.assertFalse(result["backup"]["will"])
        self.assertIn("保留", result["backup"]["note"])

    def test_preview_diff_matches_what_setup_writes(self):
        """Drift guard: every preview row must land in the file exactly as
        shown — the dialog users confirm is the write they get."""
        roles = {
            "opus": {"model": "GLM_MAX/glm-5.2", "ctx_1m": True,
                     "name": "主力"},
            "subagent": {"model": "GLM_PRO/glm-5.2", "ctx_1m": False},
            "default": {"model": "DeepSeek/v4-flash", "ctx_1m": True},
        }
        existing = {"env": {
            "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
            "ANTHROPIC_AUTH_TOKEN": "sk-real-secret",
            "CLAUDE_CODE_SUBAGENT_MODEL": "stale/sub[1M]",
            "USER_OWNED_VAR": "keep-me",
        }, "retain": 1}
        pv, _, settings_path = self._run_preview(roles=roles,
                                                 existing_settings=existing)
        self.assertTrue(pv["ok"])
        with patch("services.claude_code_setup.config_store.suanpan_listen",
                   return_value="127.0.0.1:9527"), \
             patch("services.claude_code_setup.config_store.sp_load_raw",
                   return_value={}), \
             patch.dict(config_store.PATHS, {"claude_settings": settings_path}):
            setup_result = claude_code_setup.setup(roles=roles)
        self.assertEqual(setup_result["action"], "added")
        with open(settings_path) as f:
            written_env = json.load(f)["env"]
        for row in pv["changes"]:
            if row["action"] == "remove":
                self.assertNotIn(row["key"], written_env)
            else:
                self.assertEqual(written_env[row["key"]], row["new"],
                                 f"{row['key']} written value != preview")
        # rows never fabricated: every fixed/model key not in rows is absent
        # or unchanged — USER_OWNED_VAR untouched either way
        self.assertEqual(written_env["USER_OWNED_VAR"], "keep-me")
        with open(settings_path) as f:
            full = json.load(f)
        self.assertEqual(full["retain"], 1)


if __name__ == "__main__":
    unittest.main()
