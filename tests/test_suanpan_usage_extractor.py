"""Tests for suanpan/usage_extractor.py — SSE usage extraction.

Seam: UsageExtractor.feed() with real provider event shapes. The regression
class here: providers that send placeholder zeros in message_start and the
authoritative counts in message_delta (GLM does exactly this) must still
yield correct input_tokens.
"""
import unittest

from suanpan.usage_extractor import UsageExtractor


def _sse(*events):
    """Build an SSE byte stream from (event_type, json_payload) tuples."""
    import json
    parts = []
    for payload in events:
        parts.append(b"data: " + json.dumps(payload).encode() + b"\n\n")
    return b"".join(parts)


class TestAnthropicShape(unittest.TestCase):
    """Anthropic: message_start carries authoritative input_tokens;
    message_delta carries output_tokens only."""

    def test_input_from_message_start_output_from_delta(self):
        ex = UsageExtractor()
        ex.feed(_sse(
            {"type": "message_start", "message": {"usage": {
                "input_tokens": 1200, "output_tokens": 1,
                "cache_read_input_tokens": 300,
                "cache_creation_input_tokens": 50}}},
            {"type": "message_delta", "usage": {"output_tokens": 42}},
        ))
        self.assertEqual(ex.input_tokens, 1200)
        self.assertEqual(ex.output_tokens, 42)
        self.assertEqual(ex.cache_read_tokens, 300)
        self.assertEqual(ex.cache_creation_tokens, 50)


class TestNonStreamJsonBody(unittest.TestCase):
    """Non-streaming responses are a single JSON document, not SSE: the
    usage sits at the top level. DeepSeek-bound stream:false requests
    (Claude Code sends them) logged zero usage until this mode existed."""

    def test_complete_json_body_extracted(self):
        ex = UsageExtractor(json_mode=True)
        body = (b'{"id":"msg_1","type":"message","usage":{'
                b'"input_tokens":85,"output_tokens":29,'
                b'"cache_read_input_tokens":3328,'
                b'"cache_creation_input_tokens":0}}')
        # Fed in one chunk
        ex.feed(body)
        self.assertEqual(ex.input_tokens, 85)
        self.assertEqual(ex.output_tokens, 29)
        self.assertEqual(ex.cache_read_tokens, 3328)
        self.assertEqual(ex.cache_creation_tokens, 0)

    def test_json_body_fed_across_chunks(self):
        ex = UsageExtractor(json_mode=True)
        body = (b'{"usage":{"input_tokens":12,"output_tokens":34,'
                b'"cache_read_input_tokens":0,"cache_creation_input_tokens":5}}')
        # Split mid-key — partial JSON must buffer until complete
        for i in range(0, len(body), 7):
            ex.feed(body[i:i + 7])
        self.assertEqual(ex.input_tokens, 12)
        self.assertEqual(ex.output_tokens, 34)
        self.assertEqual(ex.cache_creation_tokens, 5)

    def test_error_json_without_usage_yields_zeros(self):
        ex = UsageExtractor(json_mode=True)
        ex.feed(b'{"type":"error","error":{"type":"overloaded_error"}}')
        self.assertEqual(ex.input_tokens, 0)
        self.assertEqual(ex.output_tokens, 0)

    def test_sse_mode_ignores_bare_json_lines(self):
        # Default (SSE) mode must keep ignoring anything that is not a
        # data: line — a JSON-mode regression here would corrupt streaming.
        ex = UsageExtractor()
        ex.feed(b'{"usage":{"input_tokens":99,"output_tokens":99}}')
        self.assertEqual(ex.input_tokens, 0)
        self.assertEqual(ex.output_tokens, 0)


class TestGLMShape(unittest.TestCase):
    """GLM (open.bigmodel.cn/api/anthropic): message_start sends placeholder
    zeros; the REAL counts arrive in message_delta.usage."""

    def test_delta_counts_override_start_placeholders(self):
        ex = UsageExtractor()
        ex.feed(_sse(
            {"type": "message_start", "message": {"usage": {
                "input_tokens": 0, "output_tokens": 0}}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"},
             "usage": {"input_tokens": 7, "output_tokens": 16,
                       "cache_read_input_tokens": 0}},
        ))
        self.assertEqual(ex.input_tokens, 7)
        self.assertEqual(ex.output_tokens, 16)

    def test_start_zeros_then_delta_without_input_keeps_zero(self):
        # If a delta never provides input_tokens, the placeholder 0 stands.
        ex = UsageExtractor()
        ex.feed(_sse(
            {"type": "message_start", "message": {"usage": {"input_tokens": 0}}},
            {"type": "message_delta", "usage": {"output_tokens": 5}},
        ))
        self.assertEqual(ex.input_tokens, 0)
        self.assertEqual(ex.output_tokens, 5)


class TestKimiShape(unittest.TestCase):
    """KIMI (api.kimi.com/coding): message_start carries the real input count;
    message_delta zeroes input_tokens and reclassifies it as cache_read."""

    def test_placeholder_zero_in_delta_does_not_clobber_real_input(self):
        ex = UsageExtractor()
        ex.feed(_sse(
            {"type": "message_start", "message": {"usage": {
                "input_tokens": 87, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "output_tokens": 0}}},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"},
             "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                       "cache_read_input_tokens": 87, "output_tokens": 16}},
        ))
        self.assertEqual(ex.input_tokens, 87)
        self.assertEqual(ex.cache_read_tokens, 87)
        self.assertEqual(ex.output_tokens, 16)

    def test_exact_wire_bytes_no_space_prefix(self):
        # KIMI's actual wire format: "data:{...}" with no space after colon.
        # This is what arrives through the gateway's aiter_raw passthrough.
        ex = UsageExtractor()
        ex.feed(
            b'event:message_start\n'
            b'data:{"type":"message_start","message":{"usage":{"input_tokens":87,'
            b'"cache_creation_input_tokens":0,"cache_read_input_tokens":0,"output_tokens":0}}}\n\n'
            b'event:message_delta\n'
            b'data:{"type":"message_delta","delta":{"stop_reason":"max_tokens"},'
            b'"usage":{"input_tokens":0,"cache_creation_input_tokens":0,'
            b'"cache_read_input_tokens":87,"output_tokens":16}}\n\n'
        )
        self.assertEqual(ex.input_tokens, 87)
        self.assertEqual(ex.cache_read_tokens, 87)
        self.assertEqual(ex.output_tokens, 16)


class TestDeepSeekShape(unittest.TestCase):
    """DeepSeek (api.deepseek.com/anthropic): both events carry the real
    input count; message_delta adds output."""

    def test_repeated_input_and_delta_output(self):
        ex = UsageExtractor()
        ex.feed(_sse(
            {"type": "message_start", "message": {"usage": {
                "input_tokens": 84, "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0, "output_tokens": 0}}},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"},
             "usage": {"input_tokens": 84, "cache_creation_input_tokens": 0,
                       "cache_read_input_tokens": 0, "output_tokens": 16}},
        ))
        self.assertEqual(ex.input_tokens, 84)
        self.assertEqual(ex.output_tokens, 16)


class TestQwenShape(unittest.TestCase):
    """QWEN (maas.aliyuncs.com): message_start placeholder is 1 (not 0);
    message_delta carries the authoritative counts."""

    def test_small_placeholder_upgraded_by_delta(self):
        ex = UsageExtractor()
        ex.feed(_sse(
            {"type": "message_start", "message": {"usage": {
                "input_tokens": 1, "output_tokens": 0}}},
            {"type": "message_delta", "delta": {"stop_reason": "max_tokens"},
             "usage": {"input_tokens": 62, "output_tokens": 18,
                       "cache_creation_input_tokens": 0,
                       "cache_read_input_tokens": 0}},
        ))
        self.assertEqual(ex.input_tokens, 62)
        self.assertEqual(ex.output_tokens, 18)


class TestChunkBoundaries(unittest.TestCase):
    def test_split_across_chunks(self):
        ex = UsageExtractor()
        stream = _sse(
            {"type": "message_start", "message": {"usage": {"input_tokens": 9}}},
            {"type": "message_delta", "usage": {"output_tokens": 3}},
        )
        for i in range(0, len(stream), 7):  # awkward split points
            ex.feed(stream[i:i + 7])
        self.assertEqual(ex.input_tokens, 9)
        self.assertEqual(ex.output_tokens, 3)

    def test_garbage_lines_ignored(self):
        ex = UsageExtractor()
        ex.feed(b"data: {not json}\n\ndata: \n\n: comment\n\n")
        self.assertEqual(ex.input_tokens, 0)

    def test_non_dict_json_ignored(self):
        """`null` / `true` / `[]` payloads must not kill the stream."""
        ex = UsageExtractor()
        ex.feed(b"data: null\n\ndata: true\n\ndata: []\n\n"
                b'data:{"type":"message_start","message":{"usage":{"input_tokens":9}}}\n\n')
        self.assertEqual(ex.input_tokens, 9)

    def test_non_int_counts_ignored(self):
        """A string count (e.g. "100") must not raise TypeError in max()."""
        ex = UsageExtractor()
        ex.feed(b'data:{"type":"message_start","message":{"usage":{"input_tokens":"100"}}}\n\n'
                b'data:{"type":"message_delta","usage":{"output_tokens":3.5}}\n\n')
        self.assertEqual(ex.input_tokens, 0)
        self.assertEqual(ex.output_tokens, 0)

    def test_cr_cr_event_separator_recognized(self):
        """SSE allows \r\r as an event separator — it must not sit in the buffer."""
        ex = UsageExtractor()
        ex.feed(b'data:{"type":"message_start","message":{"usage":{"input_tokens":11}}}\r\r')
        self.assertEqual(ex.input_tokens, 11)

    def test_no_space_after_data_colon(self):
        # KIMI emits "data:{...}" (SSE spec allows zero or one space).
        ex = UsageExtractor()
        ex.feed(
            b'data:{"type":"message_start","message":{"usage":{"input_tokens":87}}}\n\n'
            b'data:{"type":"message_delta","usage":{"output_tokens":16}}\n\n'
        )
        self.assertEqual(ex.input_tokens, 87)
        self.assertEqual(ex.output_tokens, 16)

    def test_crlf_event_separator_recognized(self):
        """SSE streams using \r\n line endings must parse correctly."""
        ex = UsageExtractor()
        ex.feed(
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":55}}}\r\n\r\n'
            b'data: {"type":"message_delta","usage":{"output_tokens":9}}\r\n\r\n'
        )
        self.assertEqual(ex.input_tokens, 55)
        self.assertEqual(ex.output_tokens, 9)

    def test_crlf_split_across_chunks(self):
        """CRLF separators split across feed() calls must still be recognized."""
        ex = UsageExtractor()
        stream = (
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":3}}}\r\n\r\n'
            b'data: {"type":"message_delta","usage":{"output_tokens":7}}\r\n\r\n'
        )
        for i in range(0, len(stream), 5):
            ex.feed(stream[i:i + 5])
        self.assertEqual(ex.input_tokens, 3)
        self.assertEqual(ex.output_tokens, 7)


if __name__ == "__main__":
    unittest.main()
