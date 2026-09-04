"""Shared config I/O for Magic AI Router.

Reads, writes, migrates, and validates ~/.magic-proxy.json.
Imported by app.py and config_server.py — no circular dependency.

The file location comes from 配置存储 (config_store.PATHS["mp"]), read at
call time; CONFIG_PATH remains as a compatibility alias for the default.
"""
import json
import logging
import os

from shared import keychain
from shared import netloc
from shared.defaults import DEFAULT_CAPTURE_DIR, DEFAULT_CAPTURE_PORT
from shared.identity import IdentityMigrationError, stable_id
from shared.config_store import DEFAULT_PATHS, atomic_write, get_path

logger = logging.getLogger("magic-proxy.config")

# Compatibility alias for the default location — the live value is
# config_store.PATHS["mp"], read at call time via get_path().
CONFIG_PATH = DEFAULT_PATHS["mp"]

DEFAULT_TUNNEL = {
    "name": "",
    "ssh_user": "",
    "ssh_host": "",
    "ssh_port": 22,
    "auth_type": "key",
    "ssh_key": "",
    "ssh_compression": True,
}

DEFAULT_CONFIG = {
    "socks5_port": 1080,
    "http_listen_port": 8888,
    "system_proxy_default": False,
    "current_tunnel": 0,
    "tunnels": [],
    "capture_port": DEFAULT_CAPTURE_PORT,
    "capture_dir": DEFAULT_CAPTURE_DIR,
    "retention_days": 7,
    "prevent_sleep": False,
    "launch_at_login": False,
    "config_port": 9528,
}


def stable_tunnel_id(user: str, host: str, port) -> str:
    """确定性 id：t-<sha1(user@host:port)[:10]>——同身份恒同 id（issue #8）。"""
    return stable_id("t", f"{user or ''}@{host or ''}:{port or 22}")


def assign_stable_ids(tunnels) -> int:
    """为无 id 的隧道赋确定性 id；重复身份/重复 id 抛可行动错误。

    返回迁移数量。已有 id 一律不动（重命名/改地址不影响）。
    """
    seen_ids, seen_identity = {}, {}
    migrated = 0
    for t in tunnels or []:
        ident = f"{t.get('ssh_user', '')}@{t.get('ssh_host', '')}:{t.get('ssh_port', 22)}"
        if t.get("id"):
            if t["id"] in seen_ids:
                raise IdentityMigrationError(
                    f"隧道配置存在重复 id：{t['id']}（请修正配置文件后重试）")
            seen_ids[t["id"]] = ident
            seen_identity[ident] = True
            continue
        ordinal = 2 if ident in seen_identity else 1
        if ordinal > 1:
            # legacy 同身份双隧道（如 key+password 并存）本合法——确定性
            # 序数后缀区分 id；两隧道仍共享同一 legacy 凭证槽（与迁移前
            # 行为一致），不猜归属。显式手写重复 id 才致命。
            logger.warning("隧道重复身份 %s：以序数后缀区分 id", ident)
        seen_identity[ident] = True
        suffix = f"#{ordinal}" if ordinal > 1 else ""
        t["id"] = stable_tunnel_id(
            t.get("ssh_user", ""), t.get("ssh_host", ""),
            t.get("ssh_port", 22)) + suffix
        if t["id"] in seen_ids:
            raise IdentityMigrationError(
                f"隧道配置存在重复 id：{t['id']}（请修正配置文件后重试）")
        seen_ids[t["id"]] = ident
        migrated += 1
    return migrated


def load_config(path=None):
    """Load and migrate config; returns merged dict or None."""
    p = path or get_path("mp")
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            cfg = json.load(f)
        before = json.dumps(cfg, sort_keys=True)
        migrated = _migrate(cfg)
        # issue #8：迁移错误（重复身份/id）是可行动错误——绝不与损坏
        # 混同进 .bak 隔离；原样上抛让编排层给出可行动提示
        assign_stable_ids(migrated.get("tunnels") or [])
        if json.dumps(migrated, sort_keys=True) != before and not save_config(migrated, p):
            # The migrated dict is already clean in memory, but the file on
            # disk is still the pre-migration version — it may hold plaintext
            # ssh_password. Isolate it so cleartext never survives a retry.
            try:
                os.replace(p, p + ".bak")
                logger.error(
                    "Migrated config could not be written; pre-migration file "
                    "(possibly containing plaintext passwords) moved to %s.bak",
                    p)
            except OSError:
                logger.exception(
                    "Migrated config could not be written AND the old file "
                    "could not be isolated")
        return migrated
    except IdentityMigrationError as e:
        # 迁移可行动错误：不隔离、不改写——上抛（JSONDecodeError 等仍走
        # 既有损坏隔离路径）
        logger.error("配置迁移失败（需人工处理，原文件未动）：%s", e)
        raise
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as e:
        backup = p + ".bak"
        try:
            os.replace(p, backup)
            logger.warning("Config corrupted, backed up to %s: %s", backup, e)
        except OSError:
            logger.exception("Config corrupted and backup failed")
        return None


def _migrate(cfg):
    """Migrate old single-tunnel format AND move plaintext passwords to Keychain."""
    if not isinstance(cfg, dict):
        raise ValueError("config root must be a JSON object")
    if "tunnels" not in cfg and "ssh_host" in cfg:
        # Old format: flatten into tunnels array.
        tunnel = {}
        for k in DEFAULT_TUNNEL:
            tunnel[k] = cfg.pop(k, DEFAULT_TUNNEL[k])
        if cfg.get("ssh_password"):
            tunnel["auth_type"] = "password"
            tunnel["ssh_password"] = cfg.pop("ssh_password")
        tunnel["name"] = tunnel.get("ssh_host", "Default")
        cfg["tunnels"] = [tunnel]
        for k in ("ssh_user", "ssh_key", "ssh_compression", "ssh_password"):
            cfg.pop(k, None)
        cfg.setdefault("current_tunnel", 0)

    # Sweep any plaintext ssh_password into the Keychain so it never persists.
    # Whether or not the Keychain write succeeds, the plaintext is always
    # removed from the config — load_config saves the migrated dict back to
    # disk, and keeping the value would persist it in cleartext.
    tunnels = cfg.get("tunnels", [])
    if not isinstance(tunnels, list) or any(not isinstance(t, dict) for t in tunnels):
        raise ValueError("config tunnels must be an array of objects")
    migrated = False
    failed = 0
    for t in tunnels:
        if t.get("ssh_password"):
            plaintext = t.pop("ssh_password")
            if keychain.set_password(t, plaintext):
                migrated = True
            else:
                failed += 1
    if migrated:
        logger.info("Migrated plaintext SSH password(s) into Keychain")
    if failed:
        logger.error(
            "%d SSH password(s) could not be stored in the Keychain and were "
            "removed from the config; re-enter them in 偏好设置", failed)
    return cfg


def save_config(config, path=None):
    """Atomic write with 0600 perms — survives mid-write crashes."""
    return atomic_write(path or get_path("mp"), json.dumps(config, indent=2))


# mp 文件的「注册的额外字段」——merge 白名单外但属于合法持久化 schema
# 的键（#66 S2：白名单只拷 DEFAULT_CONFIG 曾把 local_client_token 抹掉，
# Claude Code 侧 ANTHROPIC_AUTH_TOKEN 与 Docker 卷契约随之静默失效）。
EXTRA_CONFIG_FIELDS: set = set()


def merge_config(cfg):
    """Merge raw config with defaults, coerce types, validate ranges."""
    if not isinstance(cfg, dict):
        cfg = None
    merged = DEFAULT_CONFIG.copy()
    if cfg:
        # Backward compat: old configs stored "http_listen" as a "host:port"
        # string. Convert to the new ``http_listen_port`` int field on read.
        # The host part is always loopback (validated below), so we only
        # preserve the port.
        if "http_listen" in cfg and "http_listen_port" not in cfg:
            try:
                _host, port = netloc.parse_listen(str(cfg["http_listen"]))
                cfg = {**cfg, "http_listen_port": port}
            except ValueError:
                pass  # fall through; the range check below resets to default
        for k in DEFAULT_CONFIG:
            if k in cfg:
                merged[k] = cfg[k]
        # 注册的额外字段随白名单外保留（schema 单主化——mp 文件不再
        # 有两个半主）。懒注册：local_token 的 import 顺序不可控，
        # merge 内确定性注册一次（幂等）
        if not EXTRA_CONFIG_FIELDS:
            from mpconf.local_token import FIELD as _lt_field
            EXTRA_CONFIG_FIELDS.add(_lt_field)
        for k in EXTRA_CONFIG_FIELDS:
            if k in cfg:
                merged[k] = cfg[k]
        merged["tunnels"] = []
        tunnels = cfg.get("tunnels", [])
        if not isinstance(tunnels, list):
            tunnels = []
        for t in tunnels:
            if not isinstance(t, dict):
                continue
            mt = DEFAULT_TUNNEL.copy()
            mt.update(t)
            mt["ssh_host"] = str(mt.get("ssh_host") or "").strip()
            mt["ssh_user"] = str(mt.get("ssh_user") or "").strip()
            mt["auth_type"] = mt.get("auth_type") if mt.get("auth_type") in ("key", "password") else "key"
            try:
                mt["ssh_port"] = int(mt.get("ssh_port", 22))
            except (TypeError, ValueError):
                mt["ssh_port"] = 22
            if not 1 <= mt["ssh_port"] <= 65535:
                mt["ssh_port"] = 22
            merged["tunnels"].append(mt)
    for key, default in (("socks5_port", 1080), ("capture_port", DEFAULT_CAPTURE_PORT),
                         ("config_port", 9528), ("http_listen_port", 8888)):
        try:
            merged[key] = int(merged[key])
        except (TypeError, ValueError):
            merged[key] = default
        if not 1 <= merged[key] <= 65535:
            merged[key] = default
    try:
        merged["retention_days"] = max(0, int(merged["retention_days"]))
    except (TypeError, ValueError):
        merged["retention_days"] = 7
    try:
        idx = int(merged["current_tunnel"])
    except (TypeError, ValueError):
        idx = 0
    merged["current_tunnel"] = idx if 0 <= idx < len(merged["tunnels"]) else 0
    for _key in ("prevent_sleep", "launch_at_login"):
        if not isinstance(merged.get(_key), bool):
            merged[_key] = False
    capture_dir = merged.get("capture_dir")
    if not isinstance(capture_dir, str) or not capture_dir.strip():
        capture_dir = DEFAULT_CAPTURE_DIR
    merged["capture_dir"] = os.path.abspath(os.path.expanduser(capture_dir))
    return merged
