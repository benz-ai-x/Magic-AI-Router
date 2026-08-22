"""本地客户端 token（issue #9，决策 A×4）：每安装实例专用随机 token.

随机生成一次、存 `~/.magic-proxy.json` 的 `local_client_token` 字段
（经 config_store.atomic_write：0600 + 原子替换）；单活轮换——任意时刻
一个有效值。Claude Code 同步写入该 token；网关只用它做本地客户端认证，
并在任何 Provider 出站前无条件剥除。明文永不回显于 UI/日志/diff（掩码
布尔契约）。
"""
from __future__ import annotations

import json
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
    # #46 T1c：唯一安全写入口（mkstemp 唯一临时名 + chmod 0600 + 原子
    # 替换 + 失败清理）——旧手写管线用固定 path+".tmp" 名，并发写互相
    # 截断且失败无清理。
    from mpconf import config_store
    ok = config_store.atomic_write(
        path, json.dumps(cfg, indent=2, ensure_ascii=False))
    if not ok:
        raise OSError(f"无法写入本地 token 存储文件 {path}")


def get_local_token(path: str) -> str:
    """幂等读取；不存在则生成一次并落盘（0600）。"""
    cfg = _read(path)
    tok = cfg.get(FIELD)
    if isinstance(tok, str) and tok:
        return tok
    cfg[FIELD] = secrets.token_hex(16)
    _write(path, cfg)
    return cfg[FIELD]
