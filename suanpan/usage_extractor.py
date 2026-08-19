"""Read-only SSE event scanner for Anthropic Messages API usage extraction.

Feed bytes from a streaming response; the extractor parses ``message_start``
and ``message_delta`` events to collect authoritative token counts.
Never blocks or mutates the bytes passed through.
"""
from __future__ import annotations

import json


class UsageExtractor:
    """Read-only usage scanner. Feed bytes; never blocks or mutates them.

    Two modes: SSE (default) scans ``message_start``/``message_delta``
    events; ``json_mode`` buffers a non-streaming response body and reads
    the top-level ``usage`` object once the JSON document is complete.
    """

    def __init__(self, *, json_mode: bool = False) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_creation_tokens = 0
        self._json_mode = json_mode
        self._buffer = b""

    def feed(self, chunk: bytes) -> None:
        self._buffer += chunk
        if self._json_mode:
            self._try_parse_json_body()
            return
        # SSE spec allows \n\n, \r\n\r\n, or \r\r as event separators.
        # Normalize CRLF and bare-CR separators to \n\n so one split handles
        # all three variants.
        if b"\r\n" in self._buffer:
            self._buffer = self._buffer.replace(b"\r\n", b"\n")
        if b"\r\r" in self._buffer:
            self._buffer = self._buffer.replace(b"\r\r", b"\n\n")
        while b"\n\n" in self._buffer:
            event, self._buffer = self._buffer.split(b"\n\n", 1)
            self._consume(event)

    def _try_parse_json_body(self) -> None:
        """Parse the buffered body once it forms a complete JSON document.

        Partial bodies raise ValueError and keep buffering; error bodies
        without a ``usage`` object merge nothing (counts stay zero).
        """
        try:
            data = json.loads(self._buffer)
        except ValueError:
            return
        if isinstance(data, dict):
            self._merge(data.get("usage") or {})
        self._buffer = b""

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
