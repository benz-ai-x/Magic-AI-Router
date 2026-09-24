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
    "forwards": [],
    # 多活（v0.9）：该隧道的转发会话随应用启动自动恢复（纯 -L，不占
    # socks5 端口；代理隧道自身不受此字段影响）
    "forward_autostart": False,
    # NFSv4 over SSH 隧道挂载（ADR-007）：单条 -L(2049) 承载本隧道全部
    # 挂载；enabled 才有运行时意义，其余字段随配置持久化。归一见
    # normalize_nfs（浅拷贝防护：nfs dict 绝不跨隧道共享）
    "nfs": {
        "enabled": False,
        "local_port": 12049,
        "squash_to_ssh_user": False,
        "mounts": [],   # [{name, remote_path, local_dir, auto_mount}]
    },
}

DEFAULT_CONFIG = {
    "socks5_port": 1080,
    "http_listen_port": 8888,
    "system_proxy_default": False,
    # 代理角色（v0.9.2 起双表示）：current_tunnel_id（稳定 id）是唯一
    # 持久真相——删除/调序隧道不再让角色漂移；current_tunnel 下标仅为
    # 旧版本读兼容 + merge 派生投影（每次按 id 回写）
    "current_tunnel": 0,
    "current_tunnel_id": "",
    "tunnels": [],
    "capture_port": DEFAULT_CAPTURE_PORT,
    "capture_dir": DEFAULT_CAPTURE_DIR,
    "retention_days": 7,
    "prevent_sleep": False,
    "launch_at_login": False,
    "config_port": 9528,
    # 配置 API 常驻开关（ADR-009）：False=默认不监听 :9528——设置窗
    # 打开期间/复制 AI 助手指令手势按需起停；True=随应用常驻（浏览器
    # 直开与 AI agent 随时可连）。Docker 形态不走此键（恒常驻）。
    "config_api_enabled": False,
}


def stable_tunnel_id(user: str, host: str, port) -> str:
    """确定性 id：t-<sha1(user@host:port)[:10]>——同身份恒同 id（issue #8）。"""
    return stable_id("t", f"{user or ''}@{host or ''}:{port or 22}")


def _coerce_port(value, fallback: int) -> int:
    """端口读时兼容：字符串数字接受，非法/越界回落 fallback（merge 侧）。"""
    try:
        port = int(value)
    except (TypeError, ValueError):
        return fallback
    return port if 1 <= port <= 65535 else fallback


def _normalize_forward(row) -> dict:
    """归一一条端口转发行：剥未知键、端口读时兼容、remote_host 缺省回环。

    merge 的 DEFAULT_TUNNEL.copy() 是浅拷贝——forwards 列表绝不能跨隧道
    共享默认值，这里逐行构造全新 dict。prepare 校验在 merge 前做严格
    检查（非 int 即拒）；本函数是读路径的容错半边：手编字符串端口接受，
    非法值落 0（下次保存被 prepare 拦下，绝不静默丢行）。
    enabled（逐条启停，随 v0.11）：缺省 True——旧配置零迁移；False 的
    行不进会话 -L 集合、不占本地端口（冲突检查退出）。
    """
    remote_host = str(row.get("remote_host") or "").strip() or "127.0.0.1"
    return {
        "local_port": _coerce_port(row.get("local_port"), 0),
        "remote_host": remote_host,
        "remote_port": _coerce_port(row.get("remote_port"), 0),
        "enabled": row.get("enabled") is not False,
    }


def normalize_forwards(forwards) -> list:
    """forwards 字段的读路径归一入口：非列表→[]，非 dict 行剔除。"""
    if not isinstance(forwards, list):
        return []
    return [_normalize_forward(f) for f in forwards if isinstance(f, dict)]


NFS_LOCAL_PORT_FALLBACK = 12049


def _normalize_mount_row(row) -> dict:
    """归一一条 NFS 挂载行：剥未知键，字符串原样 strip（空值合法——
    local_dir 空表示用默认 /Volumes/<name>，见 resolve_mount_dir）。"""
    return {
        "name": str(row.get("name") or "").strip(),
        "remote_path": str(row.get("remote_path") or "").strip(),
        "local_dir": str(row.get("local_dir") or "").strip(),
        "auto_mount": row.get("auto_mount") is True,
    }


def normalize_nfs(nfs) -> dict:
    """nfs 节的读路径归一：非 dict→默认；端口读时兼容；mounts 逐行全新
    构造（DEFAULT_TUNNEL 浅拷贝下 nfs dict 绝不跨隧道共享）。"""
    if not isinstance(nfs, dict):
        nfs = {}
    mounts = nfs.get("mounts")
    if not isinstance(mounts, list):
        mounts = []
    return {
        "enabled": nfs.get("enabled") is True,
        "local_port": _coerce_port(nfs.get("local_port"),
                                   NFS_LOCAL_PORT_FALLBACK),
        "squash_to_ssh_user": nfs.get("squash_to_ssh_user") is True,
        "mounts": [_normalize_mount_row(m) for m in mounts
                   if isinstance(m, dict)],
    }


def resolve_mount_dir(mount_row) -> str:
    """挂载点目录的单一解析归宿：显式 local_dir 优先，空则
    /Volumes/<name>（name 内的路径分隔符替换为 -，防嵌套）。"""
    explicit = str((mount_row or {}).get("local_dir") or "").strip()
    if explicit:
        return explicit
    name = str((mount_row or {}).get("name") or "").strip()
    safe = name.replace("/", "-").replace("\\", "-").strip() or "nfs"
    return f"/Volumes/{safe}"


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


def forward_row(cfg, tunnel_id, index):
    """按稳定 id + 行下标取转发行（磁盘真相读侧）——翻转意图的目标行
    推导单一归宿（None = 隧道/行不存在或形状不符）。"""
    rows = forward_rows(cfg, tunnel_id)
    if 0 <= index < len(rows) and isinstance(rows[index], dict):
        return rows[index]
    return None


def forward_rows(cfg, tunnel_id):
    """按稳定 id 取该隧道全部转发行（形状安全；无隧道/无行 = []）——
    翻转后的「还有启用行吗」等读侧推导共用。"""
    tunnel = next((t for t in (cfg or {}).get("tunnels", [])
                   if isinstance(t, dict) and t.get("id") == tunnel_id), None)
    rows = (tunnel or {}).get("forwards") or []
    return rows if isinstance(rows, list) else []


def toggle_forward_row(cfg, tunnel_id, index, enabled):
    """update_mp 的 mutate 构造（架构评审 C1）：翻转指定转发行的
    enabled，浅拷贝构造不动原 cfg；行不存在时原样返回（读侧已校验）。"""
    tunnels = []
    for t in (cfg or {}).get("tunnels", []):
        if isinstance(t, dict) and t.get("id") == tunnel_id:
            fws = [dict(f) for f in (t.get("forwards") or [])]
            if 0 <= index < len(fws) and isinstance(fws[index], dict):
                fws[index]["enabled"] = bool(enabled)
            tunnels.append({**t, "forwards": fws})
        else:
            tunnels.append(t)
    return {**cfg, "tunnels": tunnels}


def resolve_proxy_tunnel(tunnels, current_tunnel_id, current_tunnel):
    """代理角色单一解析：有效 id（真相）→ 旧下标（兼容无 id 旧档/手编
    配置）→ 首条。merge 双写回与 /api/state 装饰（is_proxy）共用的唯一
    判定（架构评审 C2：装饰处原为手写第三种变体）。返回隧道 dict 或
    None（无隧道/全部异形）。"""
    if isinstance(current_tunnel_id, str) and current_tunnel_id:
        for t in tunnels:
            if isinstance(t, dict) and t.get("id") == current_tunnel_id:
                return t
    try:
        idx = int(current_tunnel or 0)
    except (TypeError, ValueError):
        idx = 0
    if 0 <= idx < len(tunnels):
        return tunnels[idx]
    return tunnels[0] if tunnels else None


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
            # 浅拷贝防护：forwards 默认 [] 不跨隧道共享，逐行全新构造
            mt["forwards"] = normalize_forwards(mt.get("forwards"))
            mt["forward_autostart"] = mt.get("forward_autostart") is True
            # 同款浅拷贝防护：nfs dict 逐字段全新构造
            mt["nfs"] = normalize_nfs(mt.get("nfs"))
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
    # 代理角色解析（单一语义，读/存路径共用）：有效 id → 旧下标（兼容
    # 无 id 旧档/手编配置）→ 首条。解析后双写回：id 是真相，下标是投影
    resolved = resolve_proxy_tunnel(merged["tunnels"],
                                    merged.get("current_tunnel_id"),
                                    merged.get("current_tunnel", 0))
    merged["current_tunnel_id"] = \
        (resolved.get("id") or "") if isinstance(resolved, dict) else ""
    merged["current_tunnel"] = next(
        (i for i, t in enumerate(merged["tunnels"]) if t is resolved), 0)
    for _key in ("prevent_sleep", "launch_at_login"):
        if not isinstance(merged.get(_key), bool):
            merged[_key] = False
    capture_dir = merged.get("capture_dir")
    if not isinstance(capture_dir, str) or not capture_dir.strip():
        capture_dir = DEFAULT_CAPTURE_DIR
    merged["capture_dir"] = os.path.abspath(os.path.expanduser(capture_dir))
    return merged


# /api/state 运行态装饰字段的单一归宿（架构评审 C2）：装饰只写这四个
# 键；config_state.READONLY_DECORATED_FIELDS 的运行态半边由此派生——
# 「新增一条运行态事实 = 此处加一个键」替代两份手维护名单
RUNTIME_DECORATED_FIELDS = frozenset(
    {"capture_active", "is_proxy", "forward_running", "nfs_states"})


def decorate_runtime_state(mp, proj):
    """给 merge 后的 mp 注入运行态装饰（纯函数，/api/state 唯一装饰点）。

    读 RuntimeProjection（叶子层容器——按字段形状消费，不 import 生产
    者域）：capture_active 布尔 + forwards/mounts 命名投影（ForwardState
    /MountState）。is_proxy 经 resolve_proxy_tunnel 与 merge 同一解析序。
    proj=None（缺席/异常）= 空投影：capture_active=False、装饰全空。
    只写 RUNTIME_DECORATED_FIELDS 声明的键——prepare 剥除同一集合，
    装饰永不落盘。
    """
    if not isinstance(mp, dict):
        return mp
    forward_status = {}
    for s in tuple(getattr(proj, "forwards", ()) or ()):
        tid = getattr(s, "tunnel_id", None)
        if tid is not None:
            forward_status[tid] = getattr(s, "status", "")
    mount_states = {}
    for entry in tuple(getattr(proj, "mounts", ()) or ()):
        mount_states.setdefault(getattr(entry, "tunnel_id", None), {})[
            getattr(entry, "name", "")] = {
                "status": getattr(entry, "status", ""),
                "error": getattr(entry, "error", ""),
                "fixable": getattr(entry, "fixable", ""),
            }
    mp["capture_active"] = bool(getattr(proj, "capture_active", False))
    role = resolve_proxy_tunnel(mp.get("tunnels") or [],
                                mp.get("current_tunnel_id"),
                                mp.get("current_tunnel", 0))
    for t in mp.get("tunnels") or []:
        t["is_proxy"] = t is role
        t["forward_running"] = t.get("id") in forward_status
        t["nfs_states"] = mount_states.get(t.get("id")) or {}
    return mp
