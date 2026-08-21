"""本地客户端 token（issue #9，决策 A×4）：每安装实例专用随机 token.

随机生成一次、存 `~/.magic-proxy.json` 的 `local_client_token` 字段
（配置 store 原子写 0600 + 事务边界）；单活轮换——任意时刻一个有效值。
Claude Code 同步写入该 token；网关只用它做本地客户端认证，并在任何
Provider 出站前无条件剥除。明文永不回显于 UI/日志/diff（掩码布尔契约）。
"""
from __future__ import annotations

import json
import os
import secrets

FIELD = "local_client_token"


def _read(path: str) -> dict:
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write(path: str, cfg: dict) -> None:
    parent = os.path.dirname(path) or "."
    if not os.path.isdir(parent):
        os.makedirs(parent, mode=0o700)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def get_local_token(path: str) -> str:
    """幂等读取；不存在则生成一次并落盘（0600）。"""
    cfg = _read(path)
    tok = cfg.get(FIELD)
    if isinstance(tok, str) and tok:
        return tok
    cfg[FIELD] = secrets.token_hex(16)
    _write(path, cfg)
    return cfg[FIELD]


def rotate_token(path: str) -> str:
    """单活轮换：生成新值覆盖落盘，旧值即刻作废。返回新 token。"""
    cfg = _read(path)
    cfg[FIELD] = secrets.token_hex(16)
    _write(path, cfg)
    return cfg[FIELD]


def mask_token_state(path: str) -> dict:
    """掩码布尔契约：token_set 布尔 + 永不回显明文。"""
    cfg = _read(path)
    return {"token_set": bool(cfg.get(FIELD))}
