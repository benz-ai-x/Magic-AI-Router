"""TDD suite for ai_capture_addon (ADR-001 Task 3).

Pure functions (identify / extract_request / reassemble / build_record /
write_jsonl) carry no mitmproxy dependency, so they run under plain pytest.
Real mitmproxy flow composition is exercised separately in SIT.
"""
import json
import os

from capture import ai_capture_addon as addon
from capture import capture_store
import pytest


@pytest.fixture(autouse=True)
def _capture_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(capture_store, "DEFAULT_CAPTURE_DIR", str(tmp_path))
    monkeypatch.setattr(capture_store, "_home_dir", lambda: os.path.realpath(tmp_path))


def _sse(*events):
    """Build a wire-accurate text/event-stream body.

    Each event is either a data-object (anonymous event) or a
    ``(event_name, data-object)`` tuple. Strings pass through as the raw
    ``data:`` payload (e.g. ``"[DONE]"``).
    """
    blocks = []
    for ev in events:
        name, data = ev if isinstance(ev, tuple) else (None, ev)
        lines = []
        if name:
            lines.append(f"event: {name}")
        lines.append("data: " + (data if isinstance(data, str) else json.dumps(data)))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n\n"


# --------------------------------------------------------------------------
# identify(host, path) -> (provider, variant) | None
# --------------------------------------------------------------------------

class TestIdentify:
    def test_openai_chat_completions(self):
        assert addon.identify("api.openai.com", "/v1/chat/completions") == ("openai", "chat.completions")

    def test_openai_responses(self):
        assert addon.identify("api.openai.com", "/v1/responses") == ("openai", "responses")

    def test_anthropic_messages(self):
        assert addon.identify("api.anthropic.com", "/v1/messages") == ("anthropic", "messages")

    def test_deepseek_with_and_without_v1(self):
        assert addon.identify("api.deepseek.com", "/chat/completions") == ("deepseek", "chat.completions")
        assert addon.identify("api.deepseek.com", "/v1/chat/completions") == ("deepseek", "chat.completions")

    def test_doubao_ark_regional_host_suffix(self):
        assert addon.identify("ark.cn-beijing.volces.com", "/api/v3/chat/completions") == ("doubao", "chat.completions")
        assert addon.identify("ark.ap-southeast.volces.com", "/api/v3/chat/completions") == ("doubao", "chat.completions")

    def test_qwen_compatible_mode(self):
        assert addon.identify("dashscope.aliyuncs.com", "/compatible-mode/v1/chat/completions") == ("qwen", "chat.completions")
        assert addon.identify("dashscope-intl.aliyuncs.com", "/compatible-mode/v1/chat/completions") == ("qwen", "chat.completions")

    def test_qwen_native_dashscope(self):
        assert addon.identify("dashscope.aliyuncs.com", "/api/v1/services/aigc/text-generation/generation") == ("qwen", "dashscope.native")

    def test_minimax_v2_global_and_cn(self):
        assert addon.identify("api.minimaxi.com", "/v1/text/chatcompletion_v2") == ("minimax", "chat.completions")
        assert addon.identify("api.minimax.io", "/v1/text/chatcompletion_v2") == ("minimax", "chat.completions")

    def test_minimax_pro_legacy(self):
        assert addon.identify("api.minimax.io", "/v1/text/chatcompletion_pro") == ("minimax", "minimax.pro")

    def test_strips_query_string(self):
        assert addon.identify("api.openai.com", "/v1/chat/completions?stream=true") == ("openai", "chat.completions")

    def test_non_ai_host_returns_none(self):
        assert addon.identify("example.com", "/v1/chat/completions") is None

    def test_ai_host_non_chat_path_returns_none(self):
        # embeddings / images / audio / models must pass through un-captured
        assert addon.identify("api.openai.com", "/v1/embeddings") is None
        assert addon.identify("api.openai.com", "/v1/models") is None


# --------------------------------------------------------------------------
# extract_request(variant, body) -> {model, stream, system, messages}
# --------------------------------------------------------------------------

class TestExtractRequest:
    def test_chat_completions_pulls_system_and_keeps_messages_verbatim(self):
        body = {
            "model": "gpt-4o",
            "stream": True,
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hi 你好，帮我写代码"},
            ],
        }
        out = addon.extract_request("chat.completions", body)
        assert out["model"] == "gpt-4o"
        assert out["stream"] is True
        assert out["system"] == "You are helpful."
        # messages kept verbatim (system stays in-band per ADR example)
        assert out["messages"] == [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi 你好，帮我写代码"},
        ]

    def test_chat_completions_multimodal_content_parts_joined_with_placeholder(self):
        body = {
            "model": "gpt-4o",
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "look at "},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ]},
            ],
        }
        out = addon.extract_request("chat.completions", body)
        assert out["messages"][0]["content"] == "look at [image]"
        assert out["stream"] is False  # default when absent

    def test_responses_instructions_and_string_input(self):
        body = {"model": "gpt-4o", "stream": True, "instructions": "Be concise.", "input": "Hello"}
        out = addon.extract_request("responses", body)
        assert out["system"] == "Be concise."
        assert out["messages"] == [{"role": "user", "content": "Hello"}]

    def test_responses_structured_input_items(self):
        body = {"model": "gpt-4o", "input": [
            {"role": "user", "content": [{"type": "input_text", "text": "第一句"}]},
        ]}
        out = addon.extract_request("responses", body)
        assert out["messages"] == [{"role": "user", "content": "第一句"}]

    def test_anthropic_toplevel_system_string(self):
        body = {
            "model": "claude-opus-4-8", "stream": True, "system": "You are Claude.",
            "messages": [{"role": "user", "content": "你好"}],
        }
        out = addon.extract_request("messages", body)
        assert out["system"] == "You are Claude."
        assert out["messages"] == [{"role": "user", "content": "你好"}]

    def test_anthropic_system_as_block_array_and_content_blocks(self):
        body = {
            "model": "claude-opus-4-8",
            "system": [{"type": "text", "text": "sys-a"}],
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "see "},
                {"type": "image", "source": {"type": "base64"}},
            ]}],
        }
        out = addon.extract_request("messages", body)
        assert out["system"] == "sys-a"
        assert out["messages"][0]["content"] == "see [image]"

    def test_dashscope_native_nested_input_messages(self):
        body = {
            "model": "qwen-plus",
            "input": {"messages": [
                {"role": "system", "content": "S"},
                {"role": "user", "content": "U 中文"},
            ]},
            "parameters": {"incremental_output": True},
        }
        out = addon.extract_request("dashscope.native", body)
        assert out["model"] == "qwen-plus"
        assert out["stream"] is True
        assert out["system"] == "S"
        assert out["messages"] == [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "U 中文"},
        ]

    def test_minimax_pro_sender_type_and_bot_setting(self):
        body = {
            "model": "abab6.5s-chat", "stream": False,
            "bot_setting": [{"bot_name": "MM", "content": "You are MM."}],
            "messages": [{"sender_type": "USER", "sender_name": "u", "text": "hi"}],
        }
        out = addon.extract_request("minimax.pro", body)
        assert out["system"] == "You are MM."
        assert out["messages"] == [{"role": "user", "content": "hi"}]


# --------------------------------------------------------------------------
# reassemble(variant, is_stream, body_text) — 4 SSE families + non-stream
# --------------------------------------------------------------------------

class TestReassembleFamilyA:
    """OpenAI-style data: SSE (chat.completions: openai/deepseek/doubao/qwen-compat/minimax-v2)."""

    def test_stream_concatenates_content_reads_finish_and_usage(self):
        body = _sse(
            {"choices": [{"delta": {"role": "assistant"}, "index": 0}]},
            {"choices": [{"delta": {"content": "Hello"}, "index": 0}]},
            {"choices": [{"delta": {"content": " 世界"}, "index": 0}]},
            {"choices": [{"delta": {}, "finish_reason": "stop", "index": 0}],
             "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}},
            "[DONE]",
        )
        out = addon.reassemble("chat.completions", True, body)
        assert out["reassembled"] == "Hello 世界"
        assert out["finish_reason"] == "stop"
        assert out["usage"] == {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
        assert out["reasoning"] is None
        assert out["event_count"] == 5

    def test_stream_captures_deepseek_reasoning_content(self):
        body = _sse(
            {"choices": [{"delta": {"reasoning_content": "let me think"}}]},
            {"choices": [{"delta": {"content": "answer"}}]},
            "[DONE]",
        )
        out = addon.reassemble("chat.completions", True, body)
        assert out["reassembled"] == "answer"
        assert out["reasoning"] == "let me think"

    def test_stream_accumulates_tool_call_arguments(self):
        body = _sse(
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1", "function": {"name": "get_weather", "arguments": ""}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"city":'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ' "SF"}'}}]}}]},
            "[DONE]",
        )
        out = addon.reassemble("chat.completions", True, body)
        assert out["tool_calls"] == [{"name": "get_weather", "arguments": '{"city": "SF"}'}]

    def test_stream_ignores_blank_lines_and_sse_comments(self):
        body = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n: keep-alive\n\ndata: [DONE]\n\n'
        out = addon.reassemble("chat.completions", True, body)
        assert out["reassembled"] == "ok"

    def test_non_stream_reads_message_content(self):
        body = json.dumps({"choices": [{"message": {"role": "assistant", "content": "Hi there"},
                                        "finish_reason": "stop"}], "usage": {"total_tokens": 5}})
        out = addon.reassemble("chat.completions", False, body)
        assert out["reassembled"] == "Hi there"
        assert out["finish_reason"] == "stop"
        assert out["usage"] == {"total_tokens": 5}


class TestReassembleFamilyB:
    """Anthropic named-event SSE (/v1/messages)."""

    def test_stream_concatenates_text_delta_reads_stop_and_merges_usage(self):
        body = _sse(
            ("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 25, "output_tokens": 1}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "!"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 15}}),
            ("message_stop", {"type": "message_stop"}),
        )
        out = addon.reassemble("messages", True, body)
        assert out["reassembled"] == "Hello!"
        assert out["finish_reason"] == "end_turn"
        assert out["usage"]["input_tokens"] == 25
        assert out["usage"]["output_tokens"] == 15

    def test_stream_thinking_delta_goes_to_reasoning(self):
        body = _sse(
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "reasoning here"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("content_block_start", {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "final answer"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 1}),
            ("message_stop", {"type": "message_stop"}),
        )
        out = addon.reassemble("messages", True, body)
        assert out["reassembled"] == "final answer"
        assert out["reasoning"] == "reasoning here"

    def test_stream_tool_use_input_json_delta(self):
        body = _sse(
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"location":'}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": ' "SF"}'}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_stop", {"type": "message_stop"}),
        )
        out = addon.reassemble("messages", True, body)
        assert out["tool_calls"] == [{"name": "get_weather", "arguments": '{"location": "SF"}'}]

    def test_non_stream_joins_text_blocks(self):
        body = json.dumps({"content": [{"type": "text", "text": "The answer is 21."}],
                           "stop_reason": "end_turn", "usage": {"input_tokens": 25, "output_tokens": 10}})
        out = addon.reassemble("messages", False, body)
        assert out["reassembled"] == "The answer is 21."
        assert out["finish_reason"] == "end_turn"
        assert out["usage"] == {"input_tokens": 25, "output_tokens": 10}


class TestReassembleFamilyC:
    """OpenAI Responses semantic-event SSE (/v1/responses)."""

    def test_stream_concatenates_output_text_delta(self):
        body = _sse(
            ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "Hel"}),
            ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "lo"}),
            ("response.completed", {"type": "response.completed",
                                    "response": {"usage": {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}, "status": "completed"}}),
        )
        out = addon.reassemble("responses", True, body)
        assert out["reassembled"] == "Hello"
        assert out["usage"]["total_tokens"] == 7

    def test_non_stream_reads_output_items(self):
        body = json.dumps({"output": [{"type": "message", "role": "assistant",
                                       "content": [{"type": "output_text", "text": "Hi"}]}], "usage": {"total_tokens": 3}})
        out = addon.reassemble("responses", False, body)
        assert out["reassembled"] == "Hi"


class TestReassembleFamilyD:
    """DashScope-native SSE — default cumulative output (Qwen native)."""

    def test_stream_takes_last_cumulative_text(self):
        body = _sse(
            {"output": {"text": "你"}, "usage": {"total_tokens": 1}},
            {"output": {"text": "你好"}, "usage": {"total_tokens": 2}},
            {"output": {"text": "你好世界"}, "usage": {"total_tokens": 3}},
        )
        out = addon.reassemble("dashscope.native", True, body)
        assert out["reassembled"] == "你好世界"
        assert out["usage"]["total_tokens"] == 3

    def test_non_stream_text_form(self):
        body = json.dumps({"output": {"text": "答案"}, "usage": {"total_tokens": 2}})
        out = addon.reassemble("dashscope.native", False, body)
        assert out["reassembled"] == "答案"

    def test_non_stream_message_form(self):
        body = json.dumps({"output": {"choices": [{"message": {"content": "答案2"}, "finish_reason": "stop"}]}, "usage": {}})
        out = addon.reassemble("dashscope.native", False, body)
        assert out["reassembled"] == "答案2"
        assert out["finish_reason"] == "stop"


class TestReassembleMiniMaxPro:
    def test_non_stream_reads_choices_messages(self):
        body = json.dumps({"choices": [{"messages": [{"sender_type": "BOT", "text": "回答"}], "finish_reason": "stop"}],
                           "usage": {"total_tokens": 3}})
        out = addon.reassemble("minimax.pro", False, body)
        assert out["reassembled"] == "回答"


class TestReassembleCRLF:
    """SSE with CRLF (\\r\\n\\r\\n) event separators must reassemble correctly.
    HTTP/SSE line endings are spec'd as CRLF; a raw \\n\\n split would merge all
    events into one block and corrupt/empty the reassembly."""

    def test_family_a_crlf_separated_events(self):
        body = ('data: {"choices":[{"delta":{"content":"hi"}}]}\r\n\r\n'
                'data: {"choices":[{"delta":{"content":" there"}}]}\r\n\r\n'
                'data: [DONE]\r\n\r\n')
        out = addon.reassemble("chat.completions", True, body)
        assert out["reassembled"] == "hi there"

    def test_family_b_crlf_with_event_lines(self):
        body = ('event: content_block_delta\r\n'
                'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你好"}}\r\n\r\n'
                'event: message_stop\r\n'
                'data: {"type":"message_stop"}\r\n\r\n')
        out = addon.reassemble("messages", True, body)
        assert out["reassembled"] == "你好"


# --------------------------------------------------------------------------
# build_record / write_jsonl — JSONL schema + coordinator-pinned raw policy
# --------------------------------------------------------------------------

def _meta(**over):
    meta = {
        "ts": "2026-07-08T12:00:00.000Z", "flow_id": "abc", "method": "POST",
        "url": "https://api.openai.com/v1/chat/completions", "host": "api.openai.com",
        "provider": "openai", "variant": "chat.completions", "model": "gpt-4o",
        "stream": True, "system": "S", "messages": [{"role": "user", "content": "你好"}],
        "bytes_up": 10, "duration_ms": 100,
    }
    meta.update(over)
    return meta


class TestBuildRecord:
    def test_preserves_adr_fields_and_reassembles_response(self):
        sse = _sse({"choices": [{"delta": {"content": "hi"}}]}, "[DONE]")
        rec = addon.build_record(_meta(), 200, sse, 42, capture_raw_sse=False)
        assert rec["provider"] == "openai"
        assert rec["api_variant"] == "chat.completions"
        assert rec["model"] == "gpt-4o"
        assert rec["request"]["system"] == "S"
        assert rec["request"]["messages"] == [{"role": "user", "content": "你好"}]
        assert rec["response"]["reassembled"] == "hi"
        assert rec["bytes_up"] == 10 and rec["bytes_down"] == 42
        assert rec["status_code"] == 200
        assert rec["capture_error"] is None

    def test_drops_raw_sse_chunks_by_default(self):
        sse = _sse({"choices": [{"delta": {"content": "hi"}}]}, "[DONE]")
        rec = addon.build_record(_meta(), 200, sse, 42, capture_raw_sse=False)
        assert "sse_chunks" not in rec["response"]
        assert "raw" not in rec["response"]

    def test_raw_sse_opt_in_stores_event_payloads(self):
        sse = _sse({"choices": [{"delta": {"content": "hi"}}]}, "[DONE]")
        rec = addon.build_record(_meta(), 200, sse, 42, capture_raw_sse=True)
        assert rec["response"]["sse_chunks"] == ['{"choices": [{"delta": {"content": "hi"}}]}', "[DONE]"]

    def test_capture_error_keeps_raw_unconditionally(self, monkeypatch):
        # When reassembly raises, the record must carry capture_error AND keep the
        # raw body for debugging — even with raw_sse disabled (coordinator refinement).
        def boom(*a, **k):
            raise ValueError("boom")
        monkeypatch.setattr(addon, "reassemble", boom)
        rec = addon.build_record(_meta(stream=False), 200, "RAW-BODY", 9, capture_raw_sse=False)
        assert rec["capture_error"] is not None and "boom" in rec["capture_error"]
        assert rec["response"]["raw"] == "RAW-BODY"


class TestWriteJsonl:
    def test_appends_utf8_line_and_returns_stable_path(self, tmp_path):
        rec = {"provider": "openai", "response": {"reassembled": "你好世界"}}
        directory = tmp_path / "captures"
        p1 = addon.write_jsonl(rec, str(directory))
        p2 = addon.write_jsonl(rec, str(directory))
        assert p1 == p2  # same local-date file
        lines = open(p1, encoding="utf-8").read().splitlines()
        assert len(lines) == 2
        assert "你好世界" in lines[0]  # ensure_ascii=False keeps prompts human-readable
        assert json.loads(lines[0])["provider"] == "openai"

    def test_creates_private_directory_and_file(self, tmp_path):
        path = addon.write_jsonl({"provider": "openai"}, str(tmp_path / "captures"))
        assert os.stat(os.path.dirname(path)).st_mode & 0o777 == 0o700
        assert os.stat(path).st_mode & 0o777 == 0o600

    def test_rotates_oversized_daily_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(capture_store, "MAX_FILE_BYTES", 1)
        directory = tmp_path / "captures"
        first = addon.write_jsonl({"n": 1}, str(directory))
        second = addon.write_jsonl({"n": 2}, str(directory))
        assert first == second
        assert os.path.exists(first + ".1")
        assert json.loads(open(first, encoding="utf-8").read())["n"] == 2


# --------------------------------------------------------------------------
# AICaptureAddon hooks — passthrough / env contract / capture / never-raise
# (duck-typed fake flow; real mitmproxy HTTPFlow composition covered in SIT)
# --------------------------------------------------------------------------

import types


def _flow(host, path, method="POST", req_body=None, resp_text="", status=200):
    # Mirror the real mitmproxy API the addon uses: .content is content-encoding
    # decoded bytes (addon owns the UTF-8 charset decode), .raw_content is wire bytes.
    req_bytes = json.dumps(req_body).encode("utf-8") if req_body is not None else b""
    resp_bytes = resp_text.encode("utf-8")
    req = types.SimpleNamespace(
        pretty_host=host, path=path, method=method, url=f"https://{host}{path}",
        content=req_bytes, raw_content=req_bytes,
    )
    resp = types.SimpleNamespace(
        status_code=status, content=resp_bytes, raw_content=resp_bytes, headers={},
    )
    return types.SimpleNamespace(id="flow-1", request=req, response=resp, metadata={})


def _only_jsonl_record(dir_path):
    files = [path for path in dir_path.iterdir() if path.name.endswith(".jsonl")]
    assert len(files) == 1, f"expected 1 jsonl, got {[f.name for f in files]}"
    lines = files[0].read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


class TestAddonHooks:
    def _addon(self, tmp_path):
        a = addon.AICaptureAddon()
        a.capture_dir = str(tmp_path)
        return a

    def test_non_ai_flow_passes_through_and_writes_nothing(self, tmp_path):
        a = self._addon(tmp_path)
        flow = _flow("example.com", "/v1/chat/completions", req_body={"messages": []}, resp_text="{}")
        a.request(flow)
        a.response(flow)
        assert list(tmp_path.iterdir()) == []

    def test_ai_host_non_chat_path_passes_through(self, tmp_path):
        a = self._addon(tmp_path)
        flow = _flow("api.openai.com", "/v1/embeddings", req_body={"input": "x"}, resp_text="{}")
        a.request(flow)
        a.response(flow)
        assert list(tmp_path.iterdir()) == []

    def test_captures_openai_stream_prompt_verbatim_and_reassembly(self, tmp_path):
        a = self._addon(tmp_path)
        body = {"model": "gpt-4o", "stream": True, "messages": [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "你好，帮我写代码"}]}
        sse = _sse({"choices": [{"delta": {"content": "好的 "}}]},
                   {"choices": [{"delta": {"content": "世界"}}]}, "[DONE]")
        flow = _flow("api.openai.com", "/v1/chat/completions", req_body=body, resp_text=sse)
        a.request(flow)
        a.response(flow)
        rec = _only_jsonl_record(tmp_path)
        assert rec["provider"] == "openai"
        assert rec["request"]["system"] == "You are helpful."
        assert rec["request"]["messages"][1] == {"role": "user", "content": "你好，帮我写代码"}
        assert rec["response"]["reassembled"] == "好的 世界"
        assert rec["bytes_up"] > 0

    def test_response_never_raises_on_unparseable_body(self, tmp_path):
        a = self._addon(tmp_path)
        body = {"model": "gpt-4o", "stream": False, "messages": [{"role": "user", "content": "x"}]}
        flow = _flow("api.openai.com", "/v1/chat/completions", req_body=body, resp_text="NOT JSON")
        a.request(flow)
        a.response(flow)  # must not raise out of the hook
        rec = _only_jsonl_record(tmp_path)
        assert rec["response"]["reassembled"] == ""  # defensive: unparseable -> empty, no crash

    def test_load_reads_env_contract(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MAGIC_PROXY_CAPTURE_DIR", str(tmp_path))
        monkeypatch.setenv("MAGIC_PROXY_CAPTURE_RAW_SSE", "1")
        monkeypatch.setenv("MAGIC_PROXY_PRESERVE_STREAMING", "true")
        a = addon.AICaptureAddon()
        a.load()
        assert a.capture_dir == str(tmp_path)
        assert a.capture_raw_sse is True
        assert a.preserve_streaming is True

    def test_load_defaults_when_env_absent(self, monkeypatch):
        for key in ("MAGIC_PROXY_CAPTURE_DIR", "MAGIC_PROXY_CAPTURE_RAW_SSE", "MAGIC_PROXY_PRESERVE_STREAMING"):
            monkeypatch.delenv(key, raising=False)
        a = addon.AICaptureAddon()
        a.load()
        assert a.capture_dir.endswith(".magic-proxy-captures")
        assert a.capture_raw_sse is False
        assert a.preserve_streaming is False

    def test_preserve_streaming_off_by_default_no_tee(self, tmp_path):
        a = self._addon(tmp_path)  # preserve_streaming defaults False
        body = {"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "x"}]}
        flow = _flow("api.openai.com", "/v1/chat/completions", req_body=body, resp_text="")
        a.request(flow)
        a.responseheaders(flow)
        assert getattr(flow.response, "stream", None) is None  # no tee installed

    def test_preserve_streaming_tees_chunks_unchanged_and_captures(self, tmp_path):
        a = self._addon(tmp_path)
        a.preserve_streaming = True
        body = {"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "x"}]}
        flow = _flow("api.openai.com", "/v1/chat/completions", req_body=body, resp_text="")
        a.request(flow)
        a.responseheaders(flow)
        assert callable(flow.response.stream)
        chunk = b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
        assert flow.response.stream(chunk) == chunk  # read-only: passes through unchanged
        flow.response.stream(b"data: [DONE]\n\n")
        a.response(flow)
        rec = _only_jsonl_record(tmp_path)
        assert rec["response"]["reassembled"] == "hi"

    def test_error_hook_writes_partial_record_with_capture_error(self, tmp_path):
        a = self._addon(tmp_path)
        body = {"model": "gpt-4o", "stream": True, "messages": [{"role": "user", "content": "x"}]}
        flow = _flow("api.openai.com", "/v1/chat/completions", req_body=body, resp_text="")
        a.request(flow)
        a.error(flow)
        rec = _only_jsonl_record(tmp_path)
        assert rec["capture_error"] is not None

    def test_stream_capture_is_bounded_and_marked_truncated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(addon, "MAX_CAPTURE_FLOW_BYTES", 16)
        a = self._addon(tmp_path)
        a.preserve_streaming = True
        flow = _flow(
            "api.openai.com", "/v1/chat/completions",
            req_body={"model": "gpt", "stream": True, "messages": []},
        )
        a.request(flow)
        a.responseheaders(flow)
        flow.response.stream(b"data: " + b"x" * 100 + b"\n\n")
        a.response(flow)
        rec = _only_jsonl_record(tmp_path)
        assert rec["response"]["truncated"] is True
        assert rec["response"]["captured_bytes"] <= 16
        assert rec["provider"] == "openai"

    def test_module_exposes_addons_list(self):
        assert isinstance(addon.addons, list) and len(addon.addons) == 1
        assert isinstance(addon.addons[0], addon.AICaptureAddon)
