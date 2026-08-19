"""Deep edge-case tests for ai_capture_addon — fills gaps in the existing 55-test suite.

Seams under test (confirmed):
- _truncate_value: recursive bounding of nested dicts/lists/strings
- _env_flag: boolean env-var parsing
- reassemble: empty body, unknown variant, multiple tool calls
- extract_request: unknown variant fallback, None/missing fields
- _part_to_text: unknown block types
- _reassemble_dashscope_stream: cumulative vs delta mode detection
"""
import json

from capture import ai_capture_addon as addon
import pytest


def _sse(*events):
    """Build SSE text from data objects."""
    return "".join(f"data: {json.dumps(ev)}\n\n" for ev in events)


# ── _truncate_value ─────────────────────────────────────────────────

class TestTruncateValue:
    def test_short_string_not_truncated(self):
        val, cut = addon._truncate_value("short")
        assert val == "short" and cut is False

    def test_long_string_truncated(self):
        long = "x" * (addon.MAX_CAPTURE_TEXT_CHARS + 100)
        val, cut = addon._truncate_value(long)
        assert len(val) == addon.MAX_CAPTURE_TEXT_CHARS
        assert cut is True

    def test_dict_values_truncated_recursively(self):
        data = {"a": "short", "b": "y" * (addon.MAX_CAPTURE_TEXT_CHARS + 1)}
        val, cut = addon._truncate_value(data)
        assert len(val["b"]) == addon.MAX_CAPTURE_TEXT_CHARS
        assert len(val["a"]) == 5
        assert cut is True

    def test_list_items_truncated_recursively(self):
        data = ["ok", "z" * (addon.MAX_CAPTURE_TEXT_CHARS + 1)]
        val, cut = addon._truncate_value(data)
        assert val[0] == "ok"
        assert len(val[1]) == addon.MAX_CAPTURE_TEXT_CHARS
        assert cut is True

    def test_nested_dict_in_list_truncated(self):
        data = [{"text": "w" * (addon.MAX_CAPTURE_TEXT_CHARS + 1)}]
        val, cut = addon._truncate_value(data)
        assert len(val[0]["text"]) == addon.MAX_CAPTURE_TEXT_CHARS
        assert cut is True

    def test_int_passed_through(self):
        val, cut = addon._truncate_value(42)
        assert val == 42 and cut is False

    def test_none_passed_through(self):
        val, cut = addon._truncate_value(None)
        assert val is None and cut is False

    def test_no_truncation_returns_false(self):
        data = {"msg": "hello", "nums": [1, 2, 3]}
        val, cut = addon._truncate_value(data)
        assert val == data and cut is False


# ── _env_flag ───────────────────────────────────────────────────────

class TestEnvFlag:
    @pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "Yes"])
    def test_truthy_values(self, val, monkeypatch):
        monkeypatch.setenv("TEST_FLAG", val)
        assert addon._env_flag("TEST_FLAG") is True

    @pytest.mark.parametrize("val", ["0", "false", "no", "off", "", "random"])
    def test_falsy_values(self, val, monkeypatch):
        monkeypatch.setenv("TEST_FLAG", val)
        assert addon._env_flag("TEST_FLAG") is False

    def test_missing_env_returns_false(self, monkeypatch):
        monkeypatch.delenv("NO_SUCH_FLAG", raising=False)
        assert addon._env_flag("NO_SUCH_FLAG") is False


# ── reassemble edge cases ───────────────────────────────────────────

class TestReassembleEdgeCases:
    def test_empty_body_returns_blank(self):
        r = addon.reassemble("chat.completions", True, "")
        assert r["reassembled"] == ""
        assert r["event_count"] == 0

    def test_unknown_variant_stream_uses_openai_parser(self):
        sse = 'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        r = addon.reassemble("unknown.variant", True, sse)
        assert r["reassembled"] == "hi"

    def test_unknown_variant_non_stream_parses_as_chat_completions(self):
        body = json.dumps({"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]})
        r = addon.reassemble("unknown.variant", False, body)
        assert r["reassembled"] == "hello"
        assert r["finish_reason"] == "stop"

    def test_multiple_tool_calls_in_openai_stream(self):
        delta = {"delta": {"tool_calls": [
            {"index": 0, "function": {"name": "get_weather", "arguments": '{"city":"SF"}'}},
            {"index": 1, "function": {"name": "get_time", "arguments": '{"zone":"PT"}'}},
        ]}}
        r = addon.reassemble("chat.completions", True, _sse({"choices": [delta]}))
        assert len(r["tool_calls"]) == 2
        assert r["tool_calls"][0]["name"] == "get_weather"
        assert r["tool_calls"][1]["name"] == "get_time"

    def test_tool_call_args_split_across_deltas(self):
        d1 = {"delta": {"tool_calls": [{"index": 0, "function": {"name": "search", "arguments": "hel"}}]}}
        d2 = {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "lo world"}}]}}
        r = addon.reassemble("chat.completions", True, _sse({"choices": [d1]}, {"choices": [d2]}))
        assert r["tool_calls"][0]["arguments"] == "hello world"

    def test_invalid_json_in_stream_skipped_gracefully(self):
        text = 'data: not-json\n\ndata: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        r = addon.reassemble("chat.completions", True, text)
        assert r["reassembled"] == "ok"
        assert r["event_count"] == 2

    def test_done_marker_does_not_crash(self):
        r = addon.reassemble("chat.completions", True, "data: [DONE]\n\n")
        assert r["reassembled"] == ""


# ── extract_request edge cases ──────────────────────────────────────

class TestExtractRequestEdgeCases:
    def test_unknown_variant_returns_empty_messages(self):
        result = addon.extract_request("unknown", {"model": "x", "messages": []})
        assert result["messages"] == []
        assert result["system"] is None
        assert result["model"] == "x"

    def test_none_body_handled(self):
        result = addon.extract_request("chat.completions", None)
        assert result["model"] is None
        assert result["messages"] == []

    def test_missing_messages_key(self):
        result = addon.extract_request("chat.completions", {"model": "gpt-4"})
        assert result["messages"] == []
        assert result["system"] is None

    def test_none_content_normalized_to_empty(self):
        result = addon.extract_request("chat.completions", {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": None}],
        })
        assert result["messages"][0]["content"] == ""

    def test_dashscope_native_without_parameters(self):
        result = addon.extract_request("dashscope.native", {
            "model": "qwen",
            "input": {"messages": [{"role": "user", "content": "hi"}]},
        })
        assert result["stream"] is False
        assert result["messages"][0]["content"] == "hi"


# ── _part_to_text edge cases ────────────────────────────────────────

class TestPartToText:
    def test_unknown_block_type_returns_empty(self):
        assert addon._part_to_text({"type": "unknown_format"}) == ""

    def test_non_dict_non_string_returns_empty(self):
        assert addon._part_to_text(42) == ""
        assert addon._part_to_text(None) == ""
        assert addon._part_to_text([]) == ""

    def test_dict_with_text_key(self):
        assert addon._part_to_text({"text": "hello"}) == "hello"

    def test_image_url_placeholder(self):
        assert addon._part_to_text({"type": "image_url", "image_url": "..."}) == "[image]"

    def test_tool_result_placeholder(self):
        assert addon._part_to_text({"type": "tool_result", "tool_result": "..."}) == "[tool_result]"


# ── DashScope cumulative vs delta detection ─────────────────────────

class TestDashScopeCumulativeMode:
    def test_cumulative_mode_takes_last_text(self):
        events = [
            {"output": {"text": "Hello"}},
            {"output": {"text": "Hello world"}},
            {"output": {"text": "Hello world!", "finish_reason": "stop"}},
        ]
        r = addon.reassemble("dashscope.native", True, _sse(*events))
        assert r["reassembled"] == "Hello world!"
        assert r["finish_reason"] == "stop"

    def test_delta_mode_concatenates_parts(self):
        events = [
            {"output": {"text": "AAA"}},
            {"output": {"text": "BBB", "finish_reason": "stop"}},
        ]
        r = addon.reassemble("dashscope.native", True, _sse(*events))
        assert r["reassembled"] == "AAABBB"

    def test_dashscope_stream_via_choices(self):
        events = [
            {"output": {"choices": [{"message": {"content": "via choices"}, "finish_reason": "stop"}]}},
        ]
        r = addon.reassemble("dashscope.native", True, _sse(*events))
        assert r["reassembled"] == "via choices"
        assert r["finish_reason"] == "stop"


# ── build_record edge cases ─────────────────────────────────────────

class TestBuildRecordEdgeCases:
    def test_non_stream_response_with_empty_body(self):
        meta = {"variant": "chat.completions", "stream": False, "ts": "2026-01-01T00:00:00Z"}
        record = addon.build_record(meta, 200, "", 0)
        assert record["response"]["reassembled"] == ""
        assert record["capture_error"] is None

    def test_preserves_all_meta_fields(self):
        meta = {
            "ts": "2026-01-01T00:00:00.000Z", "flow_id": "abc", "method": "POST",
            "url": "https://api.openai.com/v1/chat/completions", "host": "api.openai.com",
            "provider": "openai", "variant": "chat.completions", "model": "gpt-4",
            "stream": False, "system": "be helpful", "messages": [{"role": "user", "content": "hi"}],
            "bytes_up": 100, "duration_ms": 500,
        }
        record = addon.build_record(meta, 200, '{"choices":[]}', 50)
        assert record["host"] == "api.openai.com"
        assert record["bytes_up"] == 100
        assert record["duration_ms"] == 500
        assert record["status_code"] == 200
