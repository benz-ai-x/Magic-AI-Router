"""Read-only SSE event scanner for Anthropic Messages API usage extraction.

Feed bytes from a streaming response; the extractor parses ``message_start``
and ``message_delta`` events to collect authoritative token counts.
Never blocks or mutates the bytes passed through.

有界增量 scanner（issue #13）：bytearray + 释放已消费前缀，每输入字节
摊销 O(1)；单 event / json_mode 文档有明确上限，超限进入 terminal
truncated 态并丢弃后续缓存——解析失败只影响统计，绝不阻塞或改变响应。
"""
from __future__ import annotations

import json

# 上限：单 SSE event 帧 4MB；json_mode 整个文档 8MB。超过即 terminal。
_MAX_EVENT_BYTES = 4 * 1024 * 1024
_MAX_JSON_DOC_BYTES = 8 * 1024 * 1024


class UsageExtractor:
    """Read-only usage scanner. Feed bytes; never blocks or mutates them.

    Two modes: SSE (default) scans ``message_start``/``message_delta``
    events; ``json_mode`` buffers a non-streaming response body and reads
    the top-level ``usage`` object once the JSON document is complete.

    ``truncated``：进入 terminal 态（超限），后续输入被丢弃——统计标记
    为「usage unavailable」。
    """

    def __init__(self, *, json_mode: bool = False) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_creation_tokens = 0
        self.truncated = False
        self._json_mode = json_mode
        self._buffer = bytearray()

    def _buf_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, chunk: bytes) -> None:
        if self.truncated:
            return
        self._buffer += chunk
        if self._json_mode:
            if len(self._buffer) > _MAX_JSON_DOC_BYTES:
                self.truncated = True
                self._buffer.clear()
                return
            self._try_parse_json_body()
            return
        self._consume_sse()

    def _consume_sse(self) -> None:
        # SSE spec allows \n\n, \r\n\r\n, or \r\r as event separators.
        # 逐帧边界扫描：bytearray 替换式前缀释放（摊销 O(1)）。
        pos = 0
        while True:
            end = self._buffer.find(b"\n\n", pos)
            crlf_end = self._buffer.find(b"\r\n\r\n", pos)
            cr_end = self._buffer.find(b"\r\r", pos)
            candidates = [e for e in (end, crlf_end, cr_end) if e >= 0]
            if not candidates:
                if len(self._buffer) - pos > _MAX_EVENT_BYTES:
                    self.truncated = True
                    self._buffer.clear()
                else:
                    del self._buffer[:pos]  # 释放已消费前缀
                return
            nxt = min(candidates)
            if nxt == end:
                self._consume(bytes(self._buffer[pos:nxt]))
                pos = nxt + 2
            elif nxt == crlf_end:
                # CRLF 帧：内含 \r\n 行分隔，先归一
                frame = bytes(self._buffer[pos:nxt]).replace(b"\r\n", b"\n")
                self._consume(frame)
                pos = nxt + 4
            else:
                frame = bytes(self._buffer[pos:nxt]).replace(b"\r\r", b"\n")
                self._consume(frame)
                pos = nxt + 2

    def _try_parse_json_body(self) -> None:
        """Parse the buffered body once it forms a complete JSON document."""
        try:
            data = json.loads(bytes(self._buffer))
        except ValueError:
            return
        if isinstance(data, dict):
            self._merge(data.get("usage") or {})
        self._buffer.clear()

    def _consume(self, event: bytes) -> None:
        for line in event.split(b"\n"):
            # SSE spec: "data:" followed by ZERO OR ONE space. GLM uses
            # "data: {...}", KIMI uses "data:{...}" — accept both.
            if line.startswith(b"data:"):
                line = line[5:]
                if line.startswith(b" "):
                    line = line[1:]
            else:
                continue
            try:
                data = json.loads(line)
            except (ValueError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue  # `null` / `true` / `[]` payloads carry no usage
            t = data.get("type")
            if t == "message_start":
                usage = (data.get("message") or {}).get("usage") or {}
                self._merge(usage)
            elif t == "message_delta":
                # Providers disagree on which event carries real counts:
                # Anthropic → message_start; GLM/QWEN → message_delta (start
                # has placeholder 0/1); KIMI → message_start (its delta re-
                # zeroes input_tokens). Max-merge satisfies all four — counts
                # are monotonic within a stream and placeholders are smallest.
                self._merge(data.get("usage") or {})

    def _merge(self, usage: dict) -> None:
        for field, attr in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cache_read_input_tokens", "cache_read_tokens"),
            ("cache_creation_input_tokens", "cache_creation_tokens"),
        ):
            value = usage.get(field)
            if isinstance(value, int):
                setattr(self, attr, max(getattr(self, attr), value))
