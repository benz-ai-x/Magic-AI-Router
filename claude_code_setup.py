"""Configure Claude Code to route through the Suanpan gateway.

Extracted from config_server._setup_claude_code so the setup logic is
testable in isolation and writes through config_store.atomic_write +
PATHS["claude_settings"] (tests can redirect; production never does).

Entry points:
- setup(roles=None) -> {"ok", "action", "msg"}.
- default_roles() -> {role: {"model", "ctx_1m"}} — derived from Suanpan
  routing rules; the seed for the UI's role table.

- roles: optional dict of role → {"model": str, "ctx_1m": bool,
  "name": optional display name for *_MODEL_NAME vars}.  When None,
  derives from Suanpan routing rules (backward compatible).
- The gateway listen always comes from config_store.suanpan_listen()
  (the canonical ADR-023 resolver).
- Backs up existing ~/.claude/settings.json to .bak on the FIRST write only
  (re-runs never clobber the user's pre-gateway backup) and reports replaced
  ANTHROPIC_BASE_URL/AUTH_TOKEN values (token never echoed in plaintext).
- Writes via config_store.atomic_write (mkstemp + chmod 0600 + os.replace).
"""
import json
import logging
import os

import config_store

logger = logging.getLogger("magic-proxy.claude_code_setup")

# Suanpan prefix rules that map to Claude Code tier roles.
_PREFIX_TO_TIER = {
    "claude-opus": "opus",
    "claude-sonnet": "sonnet",
    "claude-haiku": "haiku",
    "claude-fable": "fable",
}

# Role definitions: (key, label, env_var, has_model_name).
# "subagent" is a Claude Code concept (CLAUDE_CODE_SUBAGENT_MODEL), not a
# Suanpan routing prefix — it maps to the subagent role in the UI.
_ROLES = [
    ("opus", "Opus", "ANTHROPIC_DEFAULT_OPUS_MODEL", True),
    ("sonnet", "Sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL", True),
    ("fable", "Fable", "ANTHROPIC_DEFAULT_FABLE_MODEL", True),
    ("haiku", "Haiku", "ANTHROPIC_DEFAULT_HAIKU_MODEL", True),
    ("subagent", "Subagent", "CLAUDE_CODE_SUBAGENT_MODEL", False),
    ("default", "默认兜底模型", "ANTHROPIC_MODEL", False),
]


def _add_suffix(model: str, ctx_1m: bool) -> str:
    """Append "[1M]" to a model target if enabled and not already present."""
    if not ctx_1m or not model or model.endswith("[1M]"):
        return model
    return model + "[1M]"


def _first_tier_rule(rules: list, tier_prefix: str):
    """First rule (in list order) whose match_prefix relates to tier_prefix.

    #42: mirrors suanpan/router.py's first-hit prefix semantics — a rule
    routes models M where M.startswith(match_prefix).  For the tier prefix
    T, the rules that can route some T* model are exactly those where
    T.startswith(match_prefix) (broader/equal rule) or
    match_prefix.startswith(T) (more specific rule, e.g. claude-sonnet-4-5
    for tier claude-sonnet).  Returns route_to or None.
    """
    for r in rules:
        if not isinstance(r, dict):
            continue
        mp = r.get("match_prefix")
        if mp and r.get("route_to") and (
                tier_prefix.startswith(mp) or mp.startswith(tier_prefix)):
            return r["route_to"]
    return None


def _default_roles_from_sp(sp: dict) -> dict:
    """Derive default role mappings from Suanpan routing rules.

    Returns {role_key: {"model": str, "ctx_1m": True}}.  Used when the caller
    doesn't provide explicit roles (backward compat) or as initial values
    for the UI.
    """
    roles = {}
    if not isinstance(sp, dict):
        return roles

    rules = sp.get("rules") or []
    if not isinstance(rules, list):
        rules = []

    default_target = (sp.get("router") or {}).get("default") or ""

    for prefix, role_key in _PREFIX_TO_TIER.items():
        target = _first_tier_rule(rules, prefix) or default_target
        if target:
            roles[role_key] = {"model": target, "ctx_1m": True}

    if default_target:
        roles["default"] = {"model": default_target, "ctx_1m": True}
        # #43: subagents use the cheap haiku tier (CONTEXT.md design
        # intent: 子代理用便宜模型), falling back to the default route
        # when no haiku rule exists.
        roles["subagent"] = {
            "model": (roles.get("haiku") or {}).get("model") or default_target,
            "ctx_1m": True,
        }

    return roles


def default_roles() -> dict:
    """Derive default role mappings from the current Suanpan config.

    Seed for the UI's role table (config_server GET /api/cc-default-roles).

    #44: the payload carries the table metadata (order / labels / readonly)
    derived from _ROLES so config_ui.html needs no parallel role list —
    Python is the single source of truth.  The "default" role is rendered
    by a dedicated UI control (ccRenderDefault), not the table, so it is
    excluded from `order` and `labels`.
    """
    roles = _default_roles_from_sp(config_store.sp_load_raw())
    table = [(key, label, has_name)
             for key, label, _env_var, has_name in _ROLES
             if key != "default"]
    return {
        "roles": roles,
        "order": [key for key, _label, _has in table],
        "labels": {key: label for key, label, _has in table},
        "readonly": [key for key, _label, has_name in table if not has_name],
    }


def _roles_to_env(roles: dict) -> dict:
    """Convert a role mapping dict to Claude Code env vars.

    Each role produces its env var (with optional [1M] suffix).  Tier roles
    also produce a *_MODEL_NAME display variant: the role's custom "name"
    when set, otherwise the model without the [1M] suffix.
    """
    env = {}
    if not isinstance(roles, dict):
        return env
    for key, label, env_var, has_name in _ROLES:
        role = roles.get(key)
        if not isinstance(role, dict):
            continue
        model = role.get("model", "").strip()
        if not model:
            continue
        # #44: ctx_1m replaced one_m; the old key is still read so a stale
        # settings-window payload (pre-rename) keeps working.
        ctx_1m = bool(role.get("ctx_1m", role.get("one_m", True)))
        env[env_var] = _add_suffix(model, ctx_1m)
        if has_name:
            env[env_var + "_NAME"] = role.get("name", "").strip() or model
    return env


def _mask_old(key, old):
    """Display form of a replaced value. The user's real auth token must
    never leave the process (ADR-023 masking spirit) — only ANTHROPIC_AUTH_TOKEN
    carries a secret; URLs and model names are not sensitive."""
    if key == "ANTHROPIC_AUTH_TOKEN" and old not in (None, "", "mage-router"):
        return "（已设置，不回显）"
    return old


def _plan(roles=None):
    """Compute the complete sync plan WITHOUT writing anything.

    Single source of truth shared by setup() (applies the plan) and
    preview() (reports it), so the diff shown before the write can never
    drift from what the write actually does (#3 验收 9). The loaded settings
    document is never mutated — setup() swaps in plan["new_env"] wholesale.
    """
    listen = config_store.suanpan_listen()
    gateway_url = f"http://{listen}"
    settings_path = config_store.get_path("claude_settings")
    exists = bool(settings_path) and os.path.exists(settings_path)
    settings = {}
    if exists:
        with open(settings_path) as f:
            settings = json.load(f)
    env = dict(settings.get("env") or {})

    if roles is None:
        roles = _default_roles_from_sp(config_store.sp_load_raw())
    model_env = _roles_to_env(roles)

    already = (
        env.get("ANTHROPIC_BASE_URL") == gateway_url
        and env.get("CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS") == "1"
        and all(env.get(k) == v for k, v in model_env.items())
    )
    old_base_url = env.get("ANTHROPIC_BASE_URL") or ""
    # "mage-router" is our own placeholder — only a different, user-set
    # token counts as replaced.
    had_auth_token = env.get("ANTHROPIC_AUTH_TOKEN") not in (None, "", "mage-router")
    # First write = settings did not yet point at this gateway. Only then
    # is the .bak the user's pre-gateway state; re-runs must not clobber
    # it with an already-configured state.
    first_write = old_base_url != gateway_url

    # Wipe-set derived from _ROLES so it only ever removes keys this module
    # owns (never user-set ANTHROPIC_DEFAULT_*_MODEL vars of other tools).
    model_keys = {env_var for _, _, env_var, _ in _ROLES}
    model_keys |= {env_var + "_NAME" for _, _, env_var, has_name in _ROLES
                   if has_name}

    new_env = dict(env)
    new_env["ANTHROPIC_BASE_URL"] = gateway_url
    new_env["ANTHROPIC_AUTH_TOKEN"] = "mage-router"
    new_env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
    for key in list(new_env.keys()):
        if key in model_keys:
            del new_env[key]
    new_env.update(model_env)

    # Diff rows: fixed trio first, then owned model keys sorted; no-op rows
    # are dropped so the preview shows exactly what will change.
    changes = []
    for key in ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"):
        old, new = env.get(key), new_env.get(key)
        if old == new:
            continue
        changes.append({"key": key,
                        "action": "add" if old is None else "replace",
                        "old": _mask_old(key, old), "new": new})
    for key in sorted(model_keys):
        old, new = env.get(key), new_env.get(key)
        if old == new:
            continue
        if new is None:
            changes.append({"key": key, "action": "remove",
                            "old": old, "new": None})
        else:
            changes.append({"key": key,
                            "action": "add" if old is None else "replace",
                            "old": old, "new": new})

    return {
        "settings_path": settings_path, "exists": exists,
        "settings": settings, "new_env": new_env, "model_env": model_env,
        "already": already, "gateway_url": gateway_url,
        "old_base_url": old_base_url, "had_auth_token": had_auth_token,
        "first_write": first_write, "changes": changes,
    }


def preview(roles=None):
    """Read-only dry run of setup(): target path, per-key env diff and the
    backup decision — without touching disk. Backs the settings-window
    confirmation dialog (#3 验收 9); old token values are masked, never
    echoed."""
    try:
        plan = _plan(roles)
        if plan["already"]:
            return {"ok": True, "already": True, "changes": [],
                    "target": plan["settings_path"], "exists": plan["exists"],
                    "gateway_url": plan["gateway_url"],
                    "backup": {"will": False, "path": None,
                               "note": "已指向本网关且映射一致，重复同步不会写入，也不覆盖既有备份"}}
        if not plan["exists"]:
            backup = {"will": False, "path": None,
                      "note": "目标文件不存在，将新建（无需备份）"}
        elif plan["first_write"]:
            backup = {"will": True, "path": plan["settings_path"] + ".bak",
                      "note": "首次接入网关：写入前当前文件先备份为 .bak（之后的重复同步不再覆盖该备份）"}
        else:
            backup = {"will": False, "path": plan["settings_path"] + ".bak",
                      "note": "已指向本网关；保留首次同步前创建的 .bak 备份不变"}
        return {"ok": True, "already": False,
                "target": plan["settings_path"], "exists": plan["exists"],
                "gateway_url": plan["gateway_url"],
                "changes": plan["changes"], "backup": backup}
    except (OSError, json.JSONDecodeError, ValueError) as e:
        return {"ok": False, "msg": str(e)}


def setup(roles=None):
    """Configure ~/.claude/settings.json to route Claude Code through the gateway.

    Writes ANTHROPIC_BASE_URL to the gateway address, sets a placeholder auth
    token, writes model mappings (from explicit roles or derived from Suanpan
    routing rules), and sets CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1.
    Backs up the original settings on the first write only (see module
    docstring); replaced user-set values are reported in the returned msg.

    Returns {"ok": bool, "action": "added"|"already"|"failed", "msg": str}.
    """
    try:
        plan = _plan(roles)
        if plan["already"]:
            return {"ok": True, "action": "already",
                    "msg": f"Claude Code 已指向本网关 ({plan['gateway_url']})，无需重复配置"}

        settings_path = plan["settings_path"]
        settings = plan["settings"]
        settings["env"] = plan["new_env"]

        text = json.dumps(settings, indent=2, ensure_ascii=False)
        ok = config_store.atomic_write(settings_path, text, backup=plan["first_write"])
        if not ok:
            return {"ok": False, "action": "failed",
                    "msg": f"写入 {settings_path} 失败"}

        n_models = len([k for k in plan["model_env"] if not k.endswith("_NAME")])
        msg = f"已配置 → {plan['gateway_url']}，写入 {n_models} 个模型映射，重启 Claude Code 生效"
        msg += "，已启用供应商兼容模式"
        if plan["first_write"]:
            msg += "，原配置备份: settings.json.bak"
        if plan["old_base_url"] and plan["old_base_url"] != plan["gateway_url"]:
            msg += f"；已替换原 ANTHROPIC_BASE_URL={plan['old_base_url']}"
        if plan["had_auth_token"]:
            msg += "；原 ANTHROPIC_AUTH_TOKEN 已被替换（明文不回显，原值见备份）"
        return {"ok": True, "action": "added", "msg": msg}

    except (OSError, json.JSONDecodeError, ValueError) as e:
        return {"ok": False, "action": "failed", "msg": str(e)}
