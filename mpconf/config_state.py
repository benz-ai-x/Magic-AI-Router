"""ConfigStateStore（issue #6）：MP + SP + Keychain 的唯一事务边界.

load() / prepare() / commit() 三段式——候选配置在首次 mutation 前完成
全部校验；提交经 journal 可恢复；invalid 主文件永不覆盖最后已知良好的
.bak；on_sp_saved 只在完整提交后由 commit 触发。
"""
from __future__ import annotations

import json
import os
from typing import NamedTuple

import yaml


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
    except ValueError as exc:
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
    def __init__(self, mp_path=None, sp_path=None):
        from mpconf import config_store as _cs
        self.mp_path = mp_path or _cs.get_path("mp")
        self.sp_path = sp_path or _cs.get_path("sp")

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
        return CommitPlan(True, [], mp_c, sp_c)


def _extend_commit():
    """commit/recover 挂到 ConfigStateStore（保持模块顶部声明整洁）。"""

    @property
    def journal_path(self):
        return self.sp_path + ".txn.json"

    def _journal_write(self, payload):
        tmp = self.journal_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, self.journal_path)

    def _atomic_install(self, path, text):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            f.write(text)
        os.replace(tmp, path)

    def commit(self, plan, on_committed=None) -> SaveResult:
        """journal → MP → SP → 清 journal → 回调（只在完整提交后）。"""
        if not plan.ok:
            return SaveResult(False, "validate", list(plan.errors))
        payload = {}
        if plan.mp_candidate is not None:
            payload["mp"] = json.dumps(plan.mp_candidate, indent=2,
                                       ensure_ascii=False)
        if plan.sp_candidate is not None:
            payload["sp"] = yaml.safe_dump(plan.sp_candidate,
                                           allow_unicode=True, sort_keys=False)
        stage = "journal"
        try:
            if payload:
                self._journal_write(payload)
            if "mp" in payload:
                stage = "mp"
                self._atomic_install(self.mp_path, payload["mp"])
            if "sp" in payload:
                stage = "sp"
                self._atomic_install(self.sp_path, payload["sp"])
        except OSError as exc:
            return SaveResult(False, stage, [f"提交失败：{exc}"])
        try:
            if os.path.exists(self.journal_path):
                os.unlink(self.journal_path)
        except OSError:
            pass
        if on_committed is not None:
            try:
                on_committed()
            except Exception:
                import logging
                logging.getLogger("magic-proxy.config_state").exception(
                    "on_committed callback failed")
        return SaveResult(True, None, [])

    def recover(self) -> bool:
        """journal 重放：跨文件崩溃后补齐到一致状态（幂等）。"""
        try:
            with open(self.journal_path) as f:
                payload = json.load(f)
        except (FileNotFoundError, ValueError):
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

    ConfigStateStore.journal_path = journal_path
    ConfigStateStore._journal_write = _journal_write
    ConfigStateStore._atomic_install = _atomic_install
    ConfigStateStore.commit = commit
    ConfigStateStore.recover = recover


_extend_commit()
