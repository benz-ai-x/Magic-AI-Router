"""Append-only JSONL writer for per-request usage entries.

账房：月底对账就靠这个文件。

失败路径韧性（issue #15）：write/rotate 的任何 OSError 都被吞并计数
（best-effort adapter——业务响应不依赖观测写入成功）。聚合读取方在
services/balance_usage（全文件扫描，CST 范围过滤）；目录/文件强制
0700/0600、拒绝不安全 symlink。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock

logger = logging.getLogger("magic-proxy.suanpan.usage")


@dataclass
class UsageEntry:
    provider: str
    source_model: str
    target_model: str
    scenario: str
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    latency_ms: int
    status: int
    error: str | None


class UsageLogger:
    _MAX_BYTES = 50 * 1024 * 1024  # 50 MB → rotate
    _FAIL_LOG_EVERY = 10           # 失败节流：每 10 次才记一次 WARNING

    def __init__(self, *, enabled: bool, path: str) -> None:
        self.enabled = enabled
        self.path = Path(path).expanduser()
        self._lock = Lock()
        self.write_failures = 0
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(self.path.parent, 0o700)

    def _fail(self, exc: Exception) -> None:
        self.write_failures += 1
        if self.write_failures % self._FAIL_LOG_EVERY == 1:
            logger.warning("usage write failed (%d): %s",
                           self.write_failures, type(exc).__name__)

    def _safe_open_append(self):
        """0600 + 拒 symlink 的安全 append。"""
        path = self.path
        if path.is_symlink():
            raise OSError("usage 路径是不安全的符号链接")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        fd = os.open(path, flags, 0o600)
        os.fchmod(fd, 0o600)
        return os.fdopen(fd, "a", encoding="utf-8")

    def write(self, entry: UsageEntry) -> None:
        if not self.enabled:
            return
        record = {"ts": _now_iso(), **asdict(entry)}
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            try:
                self._maybe_rotate()
                with self._safe_open_append() as f:
                    f.write(line + "\n")
            except OSError as exc:
                self._fail(exc)
                return

    def _maybe_rotate(self) -> None:
        """Rename current log to .1 when it exceeds the size limit."""
        try:
            if self.path.exists() and self.path.stat().st_size >= self._MAX_BYTES:
                rotated = self.path.with_suffix(".jsonl.1")
                if rotated.exists():
                    rotated.unlink()
                self.path.rename(rotated)
        except OSError:
            pass  # best-effort; don't block the request


CST = timezone(timedelta(hours=8))

def _now_iso() -> str:
    return datetime.now(CST).isoformat(timespec="milliseconds")
