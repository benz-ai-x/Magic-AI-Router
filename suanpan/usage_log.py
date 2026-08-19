"""Append-only JSONL writer for per-request usage entries.

账房：月底对账就靠这个文件。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from threading import Lock


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

    def __init__(self, *, enabled: bool, path: str) -> None:
        self.enabled = enabled
        self.path = Path(path).expanduser()
        self._lock = Lock()
        # In-memory rolling totals (aggregate reads never scan the JSONL)
        self.rolling = {"calls": 0, "input_tokens": 0, "output_tokens": 0,
                         "errors": 0, "latency_sum": 0}
        if self.enabled:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, entry: UsageEntry) -> None:
        if not self.enabled:
            return
        record = {"ts": _now_iso(), **asdict(entry)}
        line = json.dumps(record, ensure_ascii=False)
        with self._lock:
            self._maybe_rotate()
            with self.path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            # Update rolling totals
            self.rolling["calls"] += 1
            self.rolling["input_tokens"] += entry.input_tokens
            self.rolling["output_tokens"] += entry.output_tokens
            self.rolling["latency_sum"] += entry.latency_ms
            if entry.status >= 400:
                self.rolling["errors"] += 1

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
