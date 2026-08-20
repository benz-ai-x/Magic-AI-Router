"""ConfigStateStore（issue #6）：MP + SP + Keychain 的唯一事务边界.

load() / prepare() / commit() 三段式——候选配置在首次 mutation 前完成
全部校验；提交经 journal 可恢复；invalid 主文件永不覆盖最后已知良好的
.bak；on_sp_saved 只在完整提交后由 commit 触发。
"""
from __future__ import annotations

import json
import logging
import os
from typing import NamedTuple

import yaml

logger = logging.getLogger("magic-proxy.config_state")


class LoadResult(NamedTuple):
    mp_state: str          # missing | valid | invalid | io_error
    sp_state: str
    mp_data: dict | None
    sp_data: dict | None
    error: str | None


class CommitPlan(NamedTuple):
    ok: bool
    errors: list
    mp_candidate: dict | None = None
    sp_candidate: dict | None = None
    keychain_sets: list = ()      # [(tunnel_snapshot, password)] 密码只在计划里
    keychain_dels: list = ()


class SaveResult(NamedTuple):
    ok: bool
    stage: str | None   # validate | journal | mp | sp | keychain | callback
    errors: list


def _read_one(path: str, loader):
    """读单个配置文件 → (state, data, error)。"""
    try:
        with open(path) as f:
            text = f.read()
    except FileNotFoundError:
        return "missing", None, None
    except OSError as exc:
        return "io_error", None, str(exc)
    try:
        data = loader(text)
    except (ValueError, yaml.YAMLError) as exc:
        return "invalid", None, str(exc)
    if not isinstance(data, dict):
        return "invalid", None, "根节点必须是对象"
    return "valid", data, None


# ── prepare：候选配置在首次 mutation 前的完整校验 ─────────────────────
_MP_PORTS = ("socks5_port", "http_listen_port", "capture_port", "config_port")
_SP_PORT_MAX = 65535
_RETENTION_MAX = 3650        # 十年封顶：再大属单位填错
_BODY_LIMIT_MAX = 512        # MB
_REQUEST_TIMEOUT_MAX = 86400


def _valid_http_origin(url) -> bool:
    if not isinstance(url, str):
        return False
    from urllib.parse import urlsplit
    parts = urlsplit(url)
    return (parts.scheme in ("http", "https")
            and bool(parts.hostname)
            and not parts.query and not parts.fragment)


class ConfigStateStore:
    def __init__(self, mp_path=None, sp_path=None, keychain=None):
        from mpconf import config_store as _cs
        self.mp_path = mp_path or _cs.get_path("mp")
        self.sp_path = sp_path or _cs.get_path("sp")
        self._keychain = keychain

    def load(self) -> LoadResult:
        mp_state, mp_data, mp_err = _read_one(self.mp_path, json.loads)
        sp_state, sp_data, sp_err = _read_one(self.sp_path, yaml.safe_load)
        return LoadResult(mp_state, sp_state, mp_data, sp_data,
                          mp_err or sp_err)

    def prepare(self, mp=None, sp=None) -> CommitPlan:
        """schema + 数值/URL + 跨引用全量校验。任何失败都不触碰磁盘。"""
        errors = []
        mp_c = mp if isinstance(mp, dict) else None
        sp_c = sp if isinstance(sp, dict) else None

        if mp_c is not None:
            for field in _MP_PORTS:
                port = mp_c.get(field)
                if port in (None, ""):
                    continue
                if not isinstance(port, int) or not 1 <= port <= _SP_PORT_MAX:
                    errors.append(f"{field} 端口无效（须 1..65535）")
            retention = mp_c.get("retention_days")
            if retention is not None and (
                    not isinstance(retention, int)
                    or not 0 <= retention <= _RETENTION_MAX):
                errors.append(f"retention_days 无效（须 0..{_RETENTION_MAX}）")

        if sp_c is not None:
            lp = sp_c.get("listen_port")
            if lp is not None and (not isinstance(lp, int)
                                   or not 1 <= lp <= _SP_PORT_MAX):
                errors.append(f"listen_port 端口无效（须 1..65535）")
            server = sp_c.get("server") or {}
            timeout = server.get("request_timeout_s")
            if timeout is not None and (not isinstance(timeout, int)
                                        or not 0 < timeout <= _REQUEST_TIMEOUT_MAX):
                errors.append(f"request_timeout_s 无效（须 >0 且 ≤{_REQUEST_TIMEOUT_MAX}）")
            body_limit = server.get("body_limit_mb")
            if body_limit is not None and (not isinstance(body_limit, int)
                                           or not 0 < body_limit <= _BODY_LIMIT_MAX):
                errors.append(f"body_limit_mb 无效（须 >0 且 ≤{_BODY_LIMIT_MAX}）")
            providers = sp_c.get("providers") or {}
            for name, p in providers.items():
                if not _valid_http_origin((p or {}).get("base_url", "")):
                    errors.append(f"供应商 {name} 的 base_url 必须是合法 http(s) origin")
            routes = set()
            for r in sp_c.get("rules") or []:
                target = (r or {}).get("route_to", "")
                routes.add(str(target).split("/", 1)[0])
            routes.add(str((sp_c.get("router") or {}).get("default") or "")
                       .split("/", 1)[0])
            routes.discard("")
            for prov in sorted(routes):
                if prov and prov not in providers:
                    errors.append(f"route_to/default 引用了不存在的供应商：{prov}")

        if errors:
            return CommitPlan(False, errors)
        # merge 默认值必须在校验之后：merge_config 会把非法端口/负保留
        # 静默重置为默认，前置会让 mp 侧数值约束在真实入口永不触发
        if mp_c is not None:
            from mpconf.config import merge_config
            mp_c = merge_config(mp_c)
        if sp_c is not None:
            # 掩码 key 恢复（原 save_config_dict 语义——live PUT 唯一保存
            # 路径在此）：按 id 匹配旧档恢复真实 key；legacy 无 id 档按名
            sp_c = self._restore_masked_sp_keys(sp_c)
        kc_sets, kc_dels = [], []
        if mp_c is not None:
            import copy
            mp_c = copy.deepcopy(mp_c)
            # 删除的隧道（id 在旧档、不在候选）：双账户清理 secret
            old_mp = self._read_mp_current() or {}
            new_ids = {t.get("id") for t in mp_c.get("tunnels") or []
                       if isinstance(t, dict)}
            for t in old_mp.get("tunnels") or []:
                if isinstance(t, dict) and t.get("id") and t["id"] not in new_ids:
                    kc_dels.append(("all", t))
            for t in mp_c.get("tunnels") or []:
                t.pop("has_password", None)      # 服务端注入的只读字段
                t.pop("capture_active", None)
                pw = t.pop("password", None)
                if pw:
                    kc_sets.append((dict(t), pw))
                elif (t.get("auth_type") == "password" and t.get("id")
                      and self._keychain is not None):
                    # issue #8 re-pin——只在 id==当前身份哈希时读 legacy：
                    # 身份编辑过的隧道 id 与地址已脱钩，legacy 账户可能
                    # 属于别的实体（Y 改址到 X 旧地址会串走 X 的密码），
                    # 绝不猜测归属。收敛：写入 id 账户 + legacy-only 删除。
                    from mpconf.config import stable_tunnel_id
                    if t["id"] == stable_tunnel_id(
                            t.get("ssh_user", ""), t.get("ssh_host", ""),
                            t.get("ssh_port", 22)):
                        legacy = {k: v for k, v in t.items() if k != "id"}
                        old_pw = self._keychain.get_password(legacy)
                        if old_pw:
                            kc_sets.append((dict(t), old_pw))
                            kc_dels.append(("legacy-only", legacy))
                elif "auth_type" in t and t.get("auth_type") != "password":
                    kc_dels.append(dict(t))
        return CommitPlan(True, [], mp_c, sp_c, kc_sets, kc_dels)

    @property
    def journal_path(self):
        return self.sp_path + ".txn.json"

    def _journal_write(self, payload):
        parent = os.path.dirname(self.journal_path) or "."
        if not os.path.isdir(parent):
            os.makedirs(parent, mode=0o700)
        tmp = self.journal_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, self.journal_path)

    def _atomic_install(self, path, text):
        parent = os.path.dirname(path) or "."
        if not os.path.isdir(parent):
            os.makedirs(parent, mode=0o700)  # 首创建目录权限
        from mpconf import config_store
        if not config_store.atomic_write(path, text):  # 唯一安全写入口
            raise OSError(f"atomic_write failed: {path}")

    def _restore_masked_sp_keys(self, sp_c: dict) -> dict:
        """api_key_set 掩码契约：UI 回传 api_key=null+api_key_set=true 表示
        保留旧 key——按 id（或 legacy 名）从当前磁盘档恢复真实值。"""
        import copy
        sp_c = copy.deepcopy(sp_c)
        try:
            with open(self.sp_path) as f:
                old = yaml.safe_load(f) or {}
        except (OSError, yaml.YAMLError):
            old = {}
        old_by_id = {p.get("id"): p for p in (old.get("providers") or {}).values()
                     if isinstance(p, dict) and p.get("id")}
        old_by_name = old.get("providers") or {}
        for name, p in sp_c.get("providers", {}).items():
            if not isinstance(p, dict):
                continue
            old_p = old_by_id.get(p.get("id"))
            if old_p is None:
                legacy = old_by_name.get(name)
                if isinstance(legacy, dict) and not legacy.get("id"):
                    old_p = legacy
            keep = bool(p.pop("api_key_set", False))
            new_key = p.get("api_key")
            p["api_key"] = (old_p or {}).get("api_key") if (keep and not new_key) \
                else (new_key or None)
        top_keep = bool(sp_c.pop("api_key_set", False))
        top_new = sp_c.get("api_key")
        sp_c["api_key"] = old.get("api_key") if (top_keep and not top_new) \
            else (top_new or None)
        return sp_c

    def _read_mp_current(self) -> dict | None:
        try:
            with open(self.mp_path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _current_text(self, path):
        try:
            with open(path) as f:
                return f.read()
        except OSError:
            return None

    def _rollback(self, payload):
        """文件段/Keychain 失败后恢复旧内容（尽力而为，异常只记日志）。"""
        for key, path in (("mp_old", self.mp_path), ("sp_old", self.sp_path)):
            if key in payload:
                try:
                    if payload[key] is None:
                        try:
                            os.unlink(path)
                        except FileNotFoundError:
                            pass  # 本就不存在 = 已是旧状态
                    else:
                        self._atomic_install(path, payload[key])
                except OSError:
                    logger.warning("rollback %s 失败，journal 保留待启动恢复", path)
                    return False
        try:
            os.unlink(self.journal_path)
        except OSError:
            pass
        return True

    def commit(self, plan, on_committed=None) -> SaveResult:
        """journal → MP → SP → Keychain → 清 journal → 回调.

        任何文件段/Keychain 失败：尽力回滚两文件到旧内容——不暴露
        「接口失败但部分新状态已生效」；回不成则 journal 保留，下次
        启动 recover() 收敛。
        """
        if not plan.ok:
            return SaveResult(False, "validate", list(plan.errors))
        payload = {}
        if plan.mp_candidate is not None:
            payload["mp"] = json.dumps(plan.mp_candidate, indent=2,
                                       ensure_ascii=False)
        if plan.sp_candidate is not None:
            payload["sp"] = yaml.safe_dump(plan.sp_candidate,
                                           allow_unicode=True, sort_keys=False)
        # *_old 只在对应侧参与本事务时存在；None 表示「事务前文件不存在」，
        # 键缺失表示「该侧不在事务内，回滚不得触碰」
        if "mp" in payload:
            payload["mp_old"] = self._current_text(self.mp_path)
        if "sp" in payload:
            payload["sp_old"] = self._current_text(self.sp_path)
        stage = "journal"
        try:
            if any(k in payload for k in ("mp", "sp")):
                self._journal_write(payload)
            if "mp" in payload:
                stage = "mp"
                self._atomic_install(self.mp_path, payload["mp"])
            if "sp" in payload:
                stage = "sp"
                self._atomic_install(self.sp_path, payload["sp"])
        except OSError as exc:
            self._rollback(payload)
            return SaveResult(False, stage, [f"提交失败：{exc}"])
        # Keychain 段原子性：sets 先行且首个失败立即中止——dels 不执行
        # （旧密码绝不因新密码写失败而丢失）；del 失败属非破坏残留（可
        # 重试），逐条收集继续。任何失败都回滚两文件，不暴露部分新状态。
        keychain_errors = []
        if self._keychain is not None:
            for tunnel, pw in plan.keychain_sets:
                if not self._keychain.set_password(tunnel, pw):
                    keychain_errors.append(
                        f"隧道 {tunnel.get('name', '?')} 的密码保存到钥匙串失败")
                    break
            if not keychain_errors:
                for entry in plan.keychain_dels:
                    if isinstance(entry, tuple):
                        mode, tunnel = entry
                    else:
                        mode, tunnel = "all", entry
                    ok = (self._keychain.delete_legacy_password(tunnel)
                          if mode == "legacy-only"
                          else self._keychain.delete_password(tunnel))
                    if not ok:
                        keychain_errors.append(
                            f"隧道 {tunnel.get('name', tunnel.get('ssh_host', '?'))} 的旧密码清理失败")
        if keychain_errors:
            self._rollback(payload)  # 文件回到旧内容：不暴露部分新状态
            return SaveResult(False, "keychain", keychain_errors)
        try:
            if os.path.exists(self.journal_path):
                os.unlink(self.journal_path)
        except OSError:
            pass
        if on_committed is not None:
            try:
                on_committed()
            except Exception:
                logger.exception("on_committed callback failed")
        return SaveResult(True, None, [])

    def recover(self) -> bool:
        """journal 重放：跨文件崩溃后补齐到一致状态（幂等）。

        损坏 journal 视为无事务清除——残留只会永久阻塞后续提交。
        """
        try:
            with open(self.journal_path) as f:
                payload = json.load(f)
        except FileNotFoundError:
            return True
        except ValueError:
            try:
                os.unlink(self.journal_path)
            except OSError:
                pass
            return True
        try:
            if "mp" in payload:
                self._atomic_install(self.mp_path, payload["mp"])
            if "sp" in payload:
                self._atomic_install(self.sp_path, payload["sp"])
            os.unlink(self.journal_path)
        except OSError:
            return False
        return True


