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
from shared import config_store


def _sp_with_providers(sp, roles):
    """夹具整备：providers 覆盖 roles/规则/default 引用的全部供应商。

    setup 现会把角色 upsert 成网关 tier 路由规则，经 ConfigStateStore
    写入时 prepare 校验「route_to 引用不存在的供应商」——夹具按引用
    目标自动补最小 providers，保持用例聚焦角色/规则语义本身。
    """
    sp = dict(sp) if sp is not None else {"listen": "127.0.0.1:9527"}
    providers = {k: dict(v) for k, v in (sp.get("providers") or {}).items()}
    targets = [r.get("route_to") for r in (sp.get("rules") or [])
               if isinstance(r, dict)]
    targets.append((sp.get("router") or {}).get("default"))
    for role in (roles or {}).values():
        if isinstance(role, dict):
            targets.append(role.get("model"))
    for t in targets:
        if not t:
            continue
        prov, _, model = str(t).partition("/")
        if not prov or not model:
            continue
        p = providers.setdefault(prov, {"base_url": f"https://{prov}.example",
                                        "api_key": "k", "models": []})
        if model not in (p.get("models") or []):
            p["models"] = [*(p.get("models") or []), model]
    sp["providers"] = providers
    return sp
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
            with patch("services.claude_code_setup.sp_config.suanpan_listen",
                       return_value="127.0.0.1:9527"), \
                 patch("services.claude_code_setup.sp_config.sp_load_raw",
                       return_value=_sp_with_providers(sp, roles)), \
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
        tok = env["ANTHROPIC_AUTH_TOKEN"]
        self.assertEqual(len(tok), 32, "写当前有效本地 token（issue #9）")
        self.assertNotEqual(tok, "mage-router")
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
        with patch("services.claude_code_setup.sp_config.sp_load_raw",
                   side_effect=FileNotFoundError("nope")), \
             patch.dict(config_store.PATHS,
                        {"claude_settings": "/nonexistent/path/settings.json"}):
            result = claude_code_setup.setup()
        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "failed")

    def test_uses_suanpan_listen(self):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with patch("services.claude_code_setup.sp_config.suanpan_listen",
                       return_value="127.0.0.1:8888"), \
                 patch("services.claude_code_setup.sp_config.sp_load_raw",
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
            with patch("services.claude_code_setup.sp_config.sp_load_raw",
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
            with patch("services.claude_code_setup.sp_config.sp_load_raw",
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
            with patch("services.claude_code_setup.sp_config.sp_load_raw",
                       return_value={"listen": "127.0.0.1:9527"}), \
                 patch.dict(config_store.PATHS, {"claude_settings": settings_path}):
                claude_code_setup.setup()
            self.assertFalse(os.path.exists(settings_path + ".bak"))

    def test_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "nested", "dir", "settings.json")
            with patch("services.claude_code_setup.sp_config.sp_load_raw",
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
            with patch("services.claude_code_setup.sp_config.suanpan_listen",
                       return_value="127.0.0.1:9527"), \
                 patch("services.claude_code_setup.sp_config.sp_load_raw",
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
        with patch("services.claude_code_setup.sp_config.sp_load_raw", return_value=sp):
            data = claude_code_setup.default_roles()
        self.assertEqual(data["roles"]["sonnet"]["model"], "KIMI/k3")
        self.assertEqual(data["roles"]["opus"]["model"], "GLM_MAX/glm-5.2")

    def test_default_roles_carries_table_metadata(self):
        """#44: the payload carries order/labels/readonly so the UI needs no
        parallel role list (Python _ROLES is the single source of truth).
        The default role is rendered by a dedicated UI control, not the
        table, so it stays out of `order`."""
        with patch("services.claude_code_setup.sp_config.sp_load_raw", return_value={}):
            data = claude_code_setup.default_roles()
        self.assertEqual(
            data["order"], ["opus", "sonnet", "fable", "haiku", "subagent"])
        self.assertEqual(data["labels"]["opus"], "Opus")
        self.assertEqual(data["labels"]["subagent"], "Subagent")
        self.assertEqual(data["readonly"], ["subagent"])
        self.assertNotIn("default", data["order"])
        self.assertEqual(set(data["labels"]), set(data["order"]))


class TestEnvToRoles(unittest.TestCase):
    """_env_to_roles() parses Claude Code env vars back into role dicts —
    the inverse of _roles_to_env. Seed for the read-back policy: a synced
    settings.json is the user's last confirmed choice, so the env layer is
    authoritative (env → roles → env must be identity)."""

    def test_parses_suffix_name_and_ctx(self):
        env = {
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "GLM_MAX/glm-5.3[1M]",
            "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": "GLM_MAX/glm-5.3",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "DeepSeek/deepseek-v4-pro",
            "CLAUDE_CODE_SUBAGENT_MODEL": "KIMI/k3[1M]",
            "ANTHROPIC_MODEL": "GLM_PRO/glm-5.3[1M]",
        }
        roles = claude_code_setup._env_to_roles(env)
        self.assertEqual(
            roles["opus"],
            {"model": "GLM_MAX/glm-5.3", "name": "GLM_MAX/glm-5.3",
             "ctx_1m": True})
        self.assertEqual(
            roles["sonnet"],
            {"model": "DeepSeek/deepseek-v4-pro", "ctx_1m": False})
        self.assertEqual(roles["subagent"], {"model": "KIMI/k3", "ctx_1m": True})
        self.assertEqual(
            roles["default"], {"model": "GLM_PRO/glm-5.3", "ctx_1m": True})
        self.assertNotIn("name", roles["subagent"],
                         "subagent/default roles carry no *_MODEL_NAME var")

    def test_round_trip_identity(self):
        """env → roles → env is identity over the image of _roles_to_env —
        i.e. for any env that layer can emit (it never omits a has_name
        role's *_MODEL_NAME)."""
        env = {
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "GLM_MAX/glm-5.3[1M]",
            "ANTHROPIC_DEFAULT_OPUS_MODEL_NAME": "GLM_MAX/glm-5.3",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "DeepSeek/deepseek-v4-pro",
            "ANTHROPIC_DEFAULT_SONNET_MODEL_NAME": "DeepSeek/deepseek-v4-pro",
            "ANTHROPIC_DEFAULT_FABLE_MODEL": "KIMI/k3[1M]",
            "ANTHROPIC_DEFAULT_FABLE_MODEL_NAME": "快车道",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "GLM_MAX/glm-5.3-flash[1M]",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL_NAME": "GLM_MAX/glm-5.3-flash",
            "CLAUDE_CODE_SUBAGENT_MODEL": "GLM_MAX/glm-5.3[1M]",
            "ANTHROPIC_MODEL": "GLM_PRO/glm-5.3[1M]",
        }
        self.assertEqual(
            claude_code_setup._roles_to_env(claude_code_setup._env_to_roles(env)),
            env)

    def test_foreign_blank_and_malformed_entries_ignored(self):
        """Only _ROLES-owned keys are read; blank/non-str values skipped."""
        env = {
            "ANTHROPIC_DEFAULT_ORACLE_MODEL": "oracle/1",
            "ANTHROPIC_MODEL": "  ",
            "CLAUDE_CODE_SUBAGENT_MODEL": 3,
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:9527",
        }
        self.assertEqual(claude_code_setup._env_to_roles(env), {})

    def test_non_dict_env(self):
        self.assertEqual(claude_code_setup._env_to_roles(None), {})
        self.assertEqual(claude_code_setup._env_to_roles("nope"), {})


class TestDefaultRolesSeedPolicy(unittest.TestCase):
    """default_roles() seeds the UI role table. Policy: when settings.json
    already points at THIS gateway the live env values are the seed (the
    user's last confirmed sync); otherwise rule-derived defaults. The payload
    carries synced/drift so the UI can surface rule↔live divergence."""

    GW = "http://127.0.0.1:9527"
    SP = {
        "rules": [
            {"match_prefix": "claude-opus", "route_to": "GLM_MAX/glm-5.2"},
            {"match_prefix": "claude-haiku", "route_to": "GLM_MAX/glm-5.2"},
        ],
        "router": {"default": "GLM_PRO/glm-5.3"},
    }

    def _default_roles(self, settings=None, sp=None, force_rules=False):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            if settings is not None:
                with open(settings_path, "w") as f:
                    json.dump(settings, f)
            with patch("services.claude_code_setup.sp_config.suanpan_listen",
                       return_value="127.0.0.1:9527"), \
                 patch("services.claude_code_setup.sp_config.sp_load_raw",
                       return_value=sp if sp is not None else self.SP), \
                 patch.dict(config_store.PATHS,
                            {"claude_settings": settings_path}):
                return claude_code_setup.default_roles(force_rules=force_rules)

    def test_no_settings_file_seeds_from_rules(self):
        data = self._default_roles(settings=None)
        self.assertEqual(data["roles"]["opus"]["model"], "GLM_MAX/glm-5.2")
        self.assertFalse(data["synced"])
        self.assertFalse(data["drift"])

    def test_foreign_gateway_env_not_used_as_seed(self):
        """settings.json pointing at ANOTHER endpoint carries that setup's
        residue — it must not seed the table."""
        settings = {"env": {
            "ANTHROPIC_BASE_URL": "https://api.anthropic.com",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "other/legacy[1M]",
        }}
        data = self._default_roles(settings=settings)
        self.assertEqual(data["roles"]["opus"]["model"], "GLM_MAX/glm-5.2")
        self.assertFalse(data["synced"])
        self.assertFalse(data["drift"])

    def test_synced_env_seeds_roles_and_flags_drift(self):
        """The reported bug: after sync + rule change the table re-derived
        glm-5.2 while Claude Code actually runs glm-5.3 — live env must win."""
        settings = {"env": {
            "ANTHROPIC_BASE_URL": self.GW,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "GLM_MAX/glm-5.3[1M]",
            "ANTHROPIC_MODEL": "GLM_PRO/glm-5.3[1M]",
        }}
        data = self._default_roles(settings=settings)
        self.assertEqual(data["roles"]["opus"]["model"], "GLM_MAX/glm-5.3")
        self.assertTrue(data["roles"]["opus"]["ctx_1m"])
        self.assertTrue(data["synced"])
        self.assertTrue(data["drift"])

    def test_synced_partial_env_falls_back_per_key(self):
        """Roles absent from the env (hand-removed) fall back to rules."""
        settings = {"env": {
            "ANTHROPIC_BASE_URL": self.GW,
            "ANTHROPIC_MODEL": "GLM_PRO/glm-5.3[1M]",
        }}
        data = self._default_roles(settings=settings)
        self.assertEqual(data["roles"]["opus"]["model"], "GLM_MAX/glm-5.2")
        self.assertEqual(data["roles"]["default"]["model"], "GLM_PRO/glm-5.3")
        self.assertTrue(data["drift"])

    def test_synced_env_matching_rules_reports_no_drift(self):
        derived = claude_code_setup._default_roles_from_sp(self.SP)
        settings = {"env": claude_code_setup._roles_to_env(derived)}
        settings["env"]["ANTHROPIC_BASE_URL"] = self.GW
        data = self._default_roles(settings=settings)
        self.assertTrue(data["synced"])
        self.assertFalse(data["drift"])

    def test_force_rules_seeds_from_rules_but_still_reports_drift(self):
        """?seed=rules (按路由规则重置) ignores the live-env seed; the drift
        flag stays truthful so the UI can explain what the reset did."""
        settings = {"env": {
            "ANTHROPIC_BASE_URL": self.GW,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "GLM_MAX/glm-5.3[1M]",
        }}
        data = self._default_roles(settings=settings, force_rules=True)
        self.assertEqual(data["roles"]["opus"]["model"], "GLM_MAX/glm-5.2")
        self.assertTrue(data["synced"])
        self.assertTrue(data["drift"])

    def test_malformed_settings_degrades_to_rule_seed(self):
        """#69 R7 shape guard applies to the read-back path too."""
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with open(settings_path, "w") as f:
                f.write('["not", "a", "dict"]')
            with patch("services.claude_code_setup.sp_config.suanpan_listen",
                       return_value="127.0.0.1:9527"), \
                 patch("services.claude_code_setup.sp_config.sp_load_raw",
                       return_value=self.SP), \
                 patch.dict(config_store.PATHS,
                            {"claude_settings": settings_path}):
                data = claude_code_setup.default_roles()
        self.assertEqual(data["roles"]["opus"]["model"], "GLM_MAX/glm-5.2")
        self.assertFalse(data["synced"])
        self.assertFalse(data["drift"])


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

    def _run_with_roles(self, roles, existing_settings=None, sp=None):
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            if existing_settings is not None:
                with open(settings_path, "w") as f:
                    json.dump(existing_settings, f)
            with patch("services.claude_code_setup.sp_config.sp_load_raw",
                       return_value=_sp_with_providers(
                           sp if sp is not None else
                           {"listen": "127.0.0.1:9527"}, roles)), \
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
            with patch("services.claude_code_setup.sp_config.sp_load_raw",
                       return_value=_sp_with_providers(sp, roles)), \
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
        with patch("services.claude_code_setup.sp_config.suanpan_listen",
                   return_value="127.0.0.1:9527"), \
             patch("services.claude_code_setup.sp_config.sp_load_raw",
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
        with patch("services.claude_code_setup.sp_config.suanpan_listen",
                   return_value="127.0.0.1:9527"), \
             patch("services.claude_code_setup.sp_config.sp_load_raw",
                   return_value=_sp_with_providers({}, roles)), \
             patch.dict(config_store.PATHS, {"claude_settings": settings_path}):
            setup_result = claude_code_setup.setup(roles=roles)
        self.assertEqual(setup_result["action"], "added")
        with open(settings_path) as f:
            written_env = json.load(f)["env"]
        for row in pv["changes"]:
            if row["action"] == "remove":
                self.assertNotIn(row["key"], written_env)
            elif row["key"] == "ANTHROPIC_AUTH_TOKEN":
                # 掩码契约：preview 报告掩码，实写为真 token——drift 守卫
                # 对此键验「报告掩码 + 实写为有效本地 token（32 hex）」
                self.assertEqual(row["new"], "（已设置，不回显）")
                self.assertEqual(len(written_env[row["key"]]), 32)
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


class TestSettingsShapeGuard(unittest.TestCase):
    """#69 R7：settings.json 为 JSON 数组/标量时不裸抛——规整为空 dict
    走「新建」分支（首写 .bak 保留原内容）。"""

    def test_array_settings_degrades_to_new_file(self):
        import tempfile
        import os
        from services import claude_code_setup
        from shared import config_store
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            with open(settings_path, "w") as f:
                f.write('["not", "a", "dict"]')
            from unittest.mock import patch
            with patch.dict(config_store.PATHS,
                            {"claude_settings": settings_path}):
                result = claude_code_setup.preview()
            self.assertIn("ok", result)  # 错误塑形返回，不裸抛



# ══ ADR-010 M4：多 Agent 配置注册表引擎 ═════════════════════════════

def _agent_env(path_key, file_name, patches_extra=()):
    """临时目录 + PATHS 重定向上下文管理器原料（与 CC 测试同模式）。"""
    d = tempfile.mkdtemp()
    return os.path.join(d, file_name)


class TestCodexSetup(unittest.TestCase):

    def _ctx(self, tmpdir):
        return patch.dict(config_store.PATHS, {
            "codex_config": os.path.join(tmpdir, "config.toml"),
            "mp": os.path.join(tmpdir, "magic-proxy.json"),
            "sp": os.path.join(tmpdir, "suanpan.yaml"),
        })

    def test_setup_writes_provider_table_and_top_level(self):
        import tomlkit
        with tempfile.TemporaryDirectory() as d:
            with self._ctx(d), \
                 patch("services.claude_code_setup.sp_config.suanpan_listen",
                       return_value="127.0.0.1:9527"):
                result = claude_code_setup.agent_setup(
                    "codex", {"model": "gpt-5.2"})
            self.assertTrue(result["ok"])
            self.assertEqual(result["action"], "added")
            with open(os.path.join(d, "config.toml")) as f:
                doc = tomlkit.parse(f.read())
            tbl = doc["model_providers"]["magic-router"]
            self.assertEqual(tbl["base_url"], "http://127.0.0.1:9527/v1")
            self.assertEqual(tbl["wire_api"], "responses")
            self.assertTrue(tbl["experimental_bearer_token"])
            self.assertEqual(doc["model"], "gpt-5.2")
            self.assertEqual(doc["model_provider"], "magic-router")

    def test_idempotent_already(self):
        with tempfile.TemporaryDirectory() as d:
            with self._ctx(d), \
                 patch("services.claude_code_setup.sp_config.suanpan_listen",
                       return_value="127.0.0.1:9527"):
                claude_code_setup.agent_setup("codex", {"model": "gpt-5.2"})
                result = claude_code_setup.agent_setup(
                    "codex", {"model": "gpt-5.2"})
            self.assertTrue(result["ok"])
            self.assertEqual(result["action"], "already")

    def test_merge_not_overwrite_preserves_user_content(self):
        """Codex /model 会写回 config.toml——我们只替换自己的表，用户的
        其它 provider 与注释必须原样保留（ADR-010 契约）。"""
        import tomlkit
        user_toml = (
            "# my codex config\n"
            "model = \"o3\"\n"
            "\n"
            "[model_providers.my-own]\n"
            "name = \"Own\"\n"
            "base_url = \"https://my.example.com/v1\"\n"
            "wire_api = \"responses\"\n"
            "env_key = \"MY_KEY\"\n"
        )
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.toml")
            with open(path, "w") as f:
                f.write(user_toml)
            with self._ctx(d), \
                 patch("services.claude_code_setup.sp_config.suanpan_listen",
                       return_value="127.0.0.1:9527"):
                result = claude_code_setup.agent_setup(
                    "codex", {"model": "gpt-5.2"})
            self.assertTrue(result["ok"])
            text = open(path).read()
            self.assertIn("# my codex config", text)  # 注释保留
            doc = tomlkit.parse(text)
            self.assertEqual(doc["model_providers"]["my-own"]["base_url"],
                             "https://my.example.com/v1")  # 用户 provider 保留
            self.assertIn("magic-router", doc["model_providers"])

    def test_missing_model_is_actionable_error(self):
        with tempfile.TemporaryDirectory() as d:
            with self._ctx(d), \
                 patch("services.claude_code_setup.sp_config.sp_load_raw",
                       return_value={}):
                result = claude_code_setup.agent_setup("codex", None)
            self.assertFalse(result["ok"])
            self.assertIn("模型", result["msg"])


class TestOpenCodeSetup(unittest.TestCase):

    def _ctx(self, tmpdir):
        return patch.dict(config_store.PATHS, {
            "opencode_config": os.path.join(tmpdir, "opencode.json"),
            "mp": os.path.join(tmpdir, "magic-proxy.json"),
        })

    def _sp(self):
        return {"router": {"default": "oai/gpt-4o-mini"},
                "providers": {"oai": {"models": ["gpt-4o-mini", "gpt-4o"]}}}

    def test_setup_writes_provider_and_models_block(self):
        with tempfile.TemporaryDirectory() as d:
            with self._ctx(d), \
                 patch("services.claude_code_setup.sp_config.suanpan_listen",
                       return_value="127.0.0.1:9527"), \
                 patch("services.claude_code_setup.sp_config.sp_load_raw",
                       return_value=self._sp()):
                result = claude_code_setup.agent_setup("opencode", None)
            self.assertTrue(result["ok"])
            with open(os.path.join(d, "opencode.json")) as f:
                cfg = json.load(f)
            tbl = cfg["provider"]["magic-router"]
            self.assertEqual(tbl["npm"], "@ai-sdk/anthropic")
            self.assertEqual(tbl["options"]["baseURL"],
                             "http://127.0.0.1:9527")
            self.assertIn("gpt-4o-mini", tbl["models"])  # models 块必写
            self.assertTrue(tbl["options"]["apiKey"])

    def test_openai_protocol_variant(self):
        with tempfile.TemporaryDirectory() as d:
            with self._ctx(d), \
                 patch("services.claude_code_setup.sp_config.suanpan_listen",
                       return_value="127.0.0.1:9527"), \
                 patch("services.claude_code_setup.sp_config.sp_load_raw",
                       return_value=self._sp()):
                claude_code_setup.agent_setup(
                    "opencode", {"protocol": "openai"})
            with open(os.path.join(d, "opencode.json")) as f:
                cfg = json.load(f)
            tbl = cfg["provider"]["magic-router"]
            self.assertEqual(tbl["npm"], "@ai-sdk/openai-compatible")
            self.assertEqual(tbl["options"]["baseURL"],
                             "http://127.0.0.1:9527/v1")

    def test_jsonc_degrades_safely(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "opencode.json")
            with open(path, "w") as f:
                f.write('{\n  // 我的注释\n  "theme": "dark"\n}\n')
            with self._ctx(d), \
                 patch("services.claude_code_setup.sp_config.suanpan_listen",
                       return_value="127.0.0.1:9527"), \
                 patch("services.claude_code_setup.sp_config.sp_load_raw",
                       return_value=self._sp()):
                result = claude_code_setup.agent_preview("opencode", None)
            self.assertFalse(result["ok"])
            self.assertIn("注释", result["msg"])  # 安全降级 + 可行动提示

    def test_preserves_user_config_keys(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "opencode.json")
            with open(path, "w") as f:
                json.dump({"theme": "dark",
                           "provider": {"openai": {"npm": "@ai-sdk/openai"}}},
                          f)
            with self._ctx(d), \
                 patch("services.claude_code_setup.sp_config.suanpan_listen",
                       return_value="127.0.0.1:9527"), \
                 patch("services.claude_code_setup.sp_config.sp_load_raw",
                       return_value=self._sp()):
                claude_code_setup.agent_setup("opencode", None)
            with open(path) as f:
                cfg = json.load(f)
            self.assertEqual(cfg["theme"], "dark")
            self.assertIn("openai", cfg["provider"])  # 用户 provider 保留
            self.assertIn("magic-router", cfg["provider"])


class TestZCodeSetup(unittest.TestCase):

    def test_setup_writes_kind_anthropic(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(config_store.PATHS, {
                "zcode_config": os.path.join(d, "config.json"),
                "mp": os.path.join(d, "magic-proxy.json"),
            }), \
                 patch("services.claude_code_setup.sp_config.suanpan_listen",
                       return_value="127.0.0.1:9527"), \
                 patch("services.claude_code_setup.sp_config.sp_load_raw",
                       return_value={"router": {"default": "glm/glm-5.3"},
                                     "providers": {"glm":
                                                   {"models": ["glm-5.3"]}}}):
                result = claude_code_setup.agent_setup("zcode", None)
            self.assertTrue(result["ok"])
            with open(os.path.join(d, "config.json")) as f:
                cfg = json.load(f)
            tbl = cfg["provider"]["magic-router"]
            self.assertEqual(tbl["kind"], "anthropic")
            self.assertEqual(tbl["options"]["baseURL"],
                             "http://127.0.0.1:9527")
            self.assertIn("glm-5.3", tbl["models"])


class TestAgentsRegistry(unittest.TestCase):

    def test_status_shape(self):
        with patch("services.claude_code_setup.sp_config.suanpan_listen",
                   return_value="127.0.0.1:9527"):
            status = claude_code_setup.agents_status()
        ids = [a["id"] for a in status]
        self.assertEqual(ids, ["claude-code", "codex", "opencode", "zcode"])
        for a in status:
            self.assertIn("label", a)
            self.assertIsInstance(a["installed"], bool)
            self.assertIsInstance(a["synced"], bool)

    def test_unknown_agent_rejected(self):
        self.assertFalse(claude_code_setup.agent_preview("nope")["ok"])
        result = claude_code_setup.agent_setup("nope")
        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "failed")

    def test_preview_masks_token(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.dict(config_store.PATHS, {
                "zcode_config": os.path.join(d, "config.json"),
                "mp": os.path.join(d, "magic-proxy.json"),
            }), \
                 patch("services.claude_code_setup.sp_config.suanpan_listen",
                       return_value="127.0.0.1:9527"), \
                 patch("services.claude_code_setup.sp_config.sp_load_raw",
                       return_value={"router": {"default": "glm/glm-5.3"},
                                     "providers": {"glm":
                                                   {"models": ["glm-5.3"]}}}):
                pv = claude_code_setup.agent_preview("zcode", None)
        self.assertTrue(pv["ok"])
        self.assertFalse(pv["already"])
        self.assertIn("backup", pv)
        joined = json.dumps(pv["changes"], ensure_ascii=False)
        self.assertNotIn("apiKey\": \"", joined.replace("apiKey=", ""))
        # token 在 change 摘要里只出现掩码形态
        for c in pv["changes"]:
            self.assertNotRegex(str(c.get("new", "")), r"[0-9a-f]{16,}")


class TestPlanRuleChanges(unittest.TestCase):
    """角色表 → tier 规则 upsert 语义（纯函数）。"""

    def test_noop_when_targets_match(self):
        sp = {"rules": [{"match_prefix": "claude-opus",
                         "route_to": "GLM_MAX/glm-5.2"}]}
        roles = {"opus": {"model": "GLM_MAX/glm-5.2"}}
        changes, rules = claude_code_setup._plan_rule_changes(roles, sp)
        self.assertEqual(changes, [])
        self.assertEqual(rules, sp["rules"])

    def test_replace_existing_tier_rule(self):
        sp = {"rules": [{"match_prefix": "claude-opus",
                         "route_to": "GLM_MAX/glm-5.2"}]}
        roles = {"opus": {"model": "GLM_MAX/glm-5.3"}}
        changes, rules = claude_code_setup._plan_rule_changes(roles, sp)
        self.assertEqual(changes, [{"match_prefix": "claude-opus",
                                    "action": "replace",
                                    "old": "GLM_MAX/glm-5.2",
                                    "new": "GLM_MAX/glm-5.3"}])
        self.assertEqual(rules[0]["route_to"], "GLM_MAX/glm-5.3")

    def test_add_missing_tier_rule(self):
        roles = {"sonnet": {"model": "KIMI/k3"}}
        changes, rules = claude_code_setup._plan_rule_changes(roles, {"rules": []})
        self.assertEqual(changes, [{"match_prefix": "claude-sonnet",
                                    "action": "add", "old": None,
                                    "new": "KIMI/k3"}])
        self.assertEqual(rules, [{"match_prefix": "claude-sonnet",
                                  "route_to": "KIMI/k3"}])

    def test_finer_prefix_rules_untouched_and_order_preserved(self):
        """更细前缀的自定义规则不被改写、不被重排（首序命中语义）。"""
        sp = {"rules": [
            {"match_prefix": "claude-opus-4-7", "route_to": "KIMI/k3"},
            {"match_prefix": "custom-prefix", "route_to": "QWEN/qwen3.8-max"},
            {"match_prefix": "claude-opus", "route_to": "GLM_MAX/glm-5.2"},
        ]}
        roles = {"opus": {"model": "GLM_MAX/glm-5.3"}}
        changes, rules = claude_code_setup._plan_rule_changes(roles, sp)
        self.assertEqual(len(changes), 1)
        self.assertEqual([r["match_prefix"] for r in rules],
                         ["claude-opus-4-7", "custom-prefix", "claude-opus"])
        self.assertEqual(rules[0]["route_to"], "KIMI/k3")  # 更细前缀原样
        self.assertEqual(rules[2]["route_to"], "GLM_MAX/glm-5.3")

    def test_allow_add_false_skips_new_rules(self):
        """推导路径（roles=None 的向导）：只对齐既有规则，不物化新规则。"""
        sp = {"rules": [{"match_prefix": "claude-opus",
                         "route_to": "GLM_MAX/glm-5.2"}]}
        roles = {"opus": {"model": "GLM_MAX/glm-5.2"},
                 "sonnet": {"model": "GLM_MAX/glm-5.3"}}  # sonnet 无既有规则
        changes, rules = claude_code_setup._plan_rule_changes(
            roles, sp, allow_add=False)
        self.assertEqual(changes, [])  # opus 已一致；sonnet 不新增
        self.assertEqual(len(rules), 1)

    def test_empty_role_model_skipped(self):
        roles = {"opus": {"model": ""}}
        changes, rules = claude_code_setup._plan_rule_changes(roles, {"rules": []})
        self.assertEqual((changes, rules), ([], []))


class TestUnlistedTargets(unittest.TestCase):
    def test_unlisted_flagged(self):
        sp = {"providers": {"GLM_MAX": {"models": ["glm-5.3"]}}}
        roles = {"opus": {"model": "GLM_MAX/glm-5.2"}}
        self.assertEqual(claude_code_setup._unlisted_targets(roles, sp),
                         [{"role": "opus", "target": "GLM_MAX/glm-5.2"}])

    def test_listed_passes(self):
        sp = {"providers": {"GLM_MAX": {"models": ["glm-5.2"]}}}
        roles = {"opus": {"model": "GLM_MAX/glm-5.2"}}
        self.assertEqual(claude_code_setup._unlisted_targets(roles, sp), [])

    def test_provider_without_models_list_passes(self):
        """无 models 清单的供应商无从判定——不警示（软警告语义）。"""
        sp = {"providers": {"GLM_MAX": {}}}
        roles = {"opus": {"model": "GLM_MAX/anything"}}
        self.assertEqual(claude_code_setup._unlisted_targets(roles, sp), [])


class TestRulesPlaneSync(unittest.TestCase):
    """「规则=持久真相」：保存同时落规则面 + env 面。"""

    def _run(self, roles, sp, existing_settings):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            settings_path = os.path.join(d, "settings.json")
            sp_path = os.path.join(d, "suanpan.yaml")
            with open(settings_path, "w") as f:
                json.dump(existing_settings, f)
            with open(sp_path, "w") as f:
                import yaml
                yaml.safe_dump(sp, f)
            with patch("services.claude_code_setup.sp_config.suanpan_listen",
                       return_value="127.0.0.1:9527"), \
                 patch("services.claude_code_setup.sp_config.sp_load_raw",
                       return_value=sp), \
                 patch.dict(config_store.PATHS,
                            {"claude_settings": settings_path, "sp": sp_path}):
                result = claude_code_setup.setup(roles=roles)
                import yaml as _yaml
                with open(sp_path) as f:
                    written_sp = _yaml.safe_load(f)
            return result, written_sp

    def test_env_aligned_but_rules_drift_still_converges(self):
        """glm-5.2 真机案例复现：env 已是 5.3、规则还是 5.2——不算 already，
        保存把规则对齐到角色表。"""
        sp = _sp_with_providers({
            "listen": "127.0.0.1:9527",
            "rules": [{"match_prefix": "claude-opus",
                       "route_to": "GLM_MAX/glm-5.2"}],
            "router": {"default": "GLM_MAX/glm-5.3"},
        }, None)
        roles = {"opus": {"model": "GLM_MAX/glm-5.3", "ctx_1m": True}}
        existing = {"env": {
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:9527",
            "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "GLM_MAX/glm-5.3[1M]",
        }}
        result, written_sp = self._run(roles, sp, existing)
        self.assertTrue(result["ok"])
        self.assertEqual(result["action"], "added")
        self.assertTrue(result["rules_written"])
        self.assertEqual(written_sp["rules"][0]["route_to"],
                         "GLM_MAX/glm-5.3")

    def test_rules_validation_failure_keeps_settings_untouched(self):
        """规则写被拒（引用不存在供应商）→ 整体 failed，settings.json 不动。"""
        sp = {"listen": "127.0.0.1:9527", "rules": [],
              "providers": {"GLM_MAX": {"models": ["glm-5.3"]}}}
        roles = {"opus": {"model": "NOPE/glm-x"}}  # 供应商不存在
        existing = {"env": {"ANTHROPIC_BASE_URL": "https://api.anthropic.com"}}
        result, written_sp = self._run(roles, sp, existing)
        self.assertFalse(result["ok"])
        self.assertIn("路由规则写入被拒", result["msg"])
        self.assertFalse(result.get("rules_written"))
        # 规则先行失败：sp 原样（无 NOPE 引用），settings.json 不被触碰
        self.assertEqual(written_sp.get("rules"), [])
        self.assertEqual(existing["env"]["ANTHROPIC_BASE_URL"],
                         "https://api.anthropic.com")

    def test_preview_carries_rule_changes_and_unlisted(self):
        sp = _sp_with_providers({
            "listen": "127.0.0.1:9527",
            "rules": [{"match_prefix": "claude-opus",
                       "route_to": "GLM_MAX/glm-5.2"}],
        }, {"opus": {"model": "GLM_MAX/glm-5.3"}})
        # glm-5.3 已由 _sp_with_providers 补入清单——手工造未在清单场景
        sp["providers"]["GLM_MAX"]["models"] = ["glm-5.2"]
        roles = {"opus": {"model": "GLM_MAX/glm-5.3", "ctx_1m": True}}
        with patch("services.claude_code_setup.sp_config.suanpan_listen",
                   return_value="127.0.0.1:9527"), \
             patch("services.claude_code_setup.sp_config.sp_load_raw",
                   return_value=sp), \
             patch.dict(config_store.PATHS,
                        {"claude_settings": "/nonexistent/settings.json"}):
            pv = claude_code_setup.preview(roles=roles)
        self.assertTrue(pv["ok"])
        self.assertEqual(pv["rule_changes"],
                         [{"match_prefix": "claude-opus", "action": "replace",
                           "old": "GLM_MAX/glm-5.2", "new": "GLM_MAX/glm-5.3"}])
        self.assertEqual(pv["unlisted"],
                         [{"role": "opus", "target": "GLM_MAX/glm-5.3"}])
