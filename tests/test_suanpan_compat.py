"""Tests for suanpan/compat.py — 协议适配唯一归宿（ADR-010）.

Seams under test: normalize_body(body, provider) → in-place mutation
（anthropic 车道归一化）；转换 A（Anthropic Messages ⇄ OpenAI Chat）的
请求向 / 响应向 / SSE 翻译器。全部纯函数/纯状态机，无 I/O。
"""
import json
import unittest

from shared.provider_auth import openai_max_tokens_field
from suanpan.compat import (
    OpenAIChatToAnthropicSSE,
    anthropic_to_openai_request,
    normalize_body,
    openai_chat_response_to_anthropic,
    openai_error_to_anthropic,
)
from suanpan.usage_extractor import UsageExtractor


# ── _flatten_system (via normalize_body) ───────────────────────────

class TestFlattenSystem(unittest.TestCase):
    def test_string_system_unchanged(self):
        body = {"system": "You are helpful.", "model": "test"}
        normalize_body(body, "deepseek")
        self.assertEqual(body["system"], "You are helpful.")

    def test_array_system_flattened_to_string(self):
        body = {
            "system": [
                {"type": "text", "text": "You are helpful.",
                 "cache_control": {"type": "ephemeral"}},
            ],
            "model": "test",
        }
        normalize_body(body, "deepseek")
        self.assertEqual(body["system"], "You are helpful.")

    def test_multi_block_system_concatenated(self):
        body = {
            "system": [
                {"type": "text", "text": "Rule 1."},
                {"type": "text", "text": "Rule 2."},
            ],
            "model": "test",
        }
        normalize_body(body, "deepseek")
        self.assertEqual(body["system"], "Rule 1.\nRule 2.")

    def test_cache_control_dropped(self):
        body = {
            "system": [
                {"type": "text", "text": "prompt",
                 "cache_control": {"type": "ephemeral"}},
            ],
            "model": "test",
        }
        normalize_body(body, "deepseek")
        self.assertIsInstance(body["system"], str)
        self.assertNotIn("cache_control", body)

    def test_system_missing_no_error(self):
        body = {"model": "test"}
        normalize_body(body, "deepseek")
        self.assertNotIn("system", body)

    def test_system_none_no_error(self):
        body = {"system": None, "model": "test"}
        normalize_body(body, "deepseek")
        self.assertIsNone(body["system"])

    def test_non_text_block_skipped(self):
        body = {
            "system": [
                {"type": "image", "source": {"url": "data:..."}},
                {"type": "text", "text": "keep this"},
            ],
            "model": "test",
        }
        normalize_body(body, "deepseek")
        self.assertEqual(body["system"], "keep this")


# ── Cross-provider uniformity ──────────────────────────────────────

class TestProviderUniformity(unittest.TestCase):
    """Default-path normalizations are provider-agnostic; providers that
    natively accept Anthropic bodies opt out via anthropic_native."""

    def test_same_result_all_providers(self):
        body = {
            "system": [{"type": "text", "text": "hello"}],
            "model": "test",
        }
        for provider in ("deepseek", "glm", "kimi"):
            b = {k: v for k, v in body.items()}
            normalize_body(b, provider)
            self.assertEqual(b["system"], "hello")


# ── anthropic_native opt-out ───────────────────────────────────────

class TestAnthropicNative(unittest.TestCase):
    """anthropic_native=True skips all compatibility stripping: the backend
    natively accepts Anthropic body shapes, so cache_control markers (prompt
    caching), document blocks, and beta tool fields must survive (#5)."""

    def test_system_array_and_cache_control_preserved(self):
        system = [
            {"type": "text", "text": "You are helpful.",
             "cache_control": {"type": "ephemeral"}},
        ]
        body = {"system": system, "model": "test"}
        normalize_body(body, "anthropic", anthropic_native=True)
        self.assertEqual(body["system"], system)

    def test_document_blocks_preserved(self):
        body = {
            "model": "test",
            "messages": [{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64"}},
                {"type": "text", "text": "see attached"},
            ]}],
        }
        normalize_body(body, "anthropic", anthropic_native=True)
        content = body["messages"][0]["content"]
        self.assertEqual([b["type"] for b in content], ["document", "text"])

    def test_beta_tool_fields_preserved(self):
        body = {
            "model": "test",
            "tools": [{"name": "t", "defer_loading": True,
                       "eager_input_streaming": True}],
        }
        normalize_body(body, "anthropic", anthropic_native=True)
        self.assertTrue(body["tools"][0]["defer_loading"])
        self.assertTrue(body["tools"][0]["eager_input_streaming"])

    def test_default_still_flattens_same_body(self):
        system = [{"type": "text", "text": "x",
                   "cache_control": {"type": "ephemeral"}}]
        body = {"system": system, "model": "test"}
        normalize_body(body, "deepseek")
        self.assertEqual(body["system"], "x")


# ── In-place mutation ──────────────────────────────────────────────

class TestInPlaceMutation(unittest.TestCase):
    def test_returns_none(self):
        body = {"system": "x", "model": "test"}
        result = normalize_body(body, "deepseek")
        self.assertIsNone(result)

    def test_mutates_body_in_place(self):
        body = {"system": [{"type": "text", "text": "x"}], "model": "test"}
        normalize_body(body, "deepseek")
        # Same object, mutated
        self.assertEqual(body["system"], "x")


# ── thinking untouched ─────────────────────────────────────────────

class TestThinkingUntouched(unittest.TestCase):
    def test_thinking_preserved(self):
        body = {
            "system": [{"type": "text", "text": "x"}],
            "thinking": {"type": "enabled", "budget_tokens": 16000},
            "model": "test",
        }
        normalize_body(body, "deepseek")
        self.assertEqual(body["thinking"], {"type": "enabled", "budget_tokens": 16000})


# ── _strip_document_blocks (via normalize_body) ────────────────────

class TestStripDocumentBlocks(unittest.TestCase):
    def test_document_block_removed(self):
        body = {
            "messages": [
                {"role": "user", "content": [
                    {"type": "document", "source": {"type": "base64"}},
                    {"type": "text", "text": "describe this"},
                ]},
            ],
            "model": "test",
        }
        normalize_body(body, "kimi")
        content = body["messages"][0]["content"]
        types = [b["type"] for b in content]
        self.assertNotIn("document", types)
        self.assertIn("text", types)

    def test_all_document_replaced_with_placeholder(self):
        body = {
            "messages": [
                {"role": "user", "content": [
                    {"type": "document", "source": {"type": "base64"}},
                ]},
            ],
            "model": "test",
        }
        normalize_body(body, "kimi")
        content = body["messages"][0]["content"]
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["type"], "text")

    def test_no_document_unchanged(self):
        body = {
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "hello"},
                ]},
            ],
            "model": "test",
        }
        normalize_body(body, "kimi")
        self.assertEqual(body["messages"][0]["content"][0]["text"], "hello")

    def test_content_string_no_error(self):
        body = {
            "messages": [
                {"role": "user", "content": "plain string"},
            ],
            "model": "test",
        }
        normalize_body(body, "kimi")
        self.assertEqual(body["messages"][0]["content"], "plain string")

    def test_messages_missing_no_error(self):
        body = {"model": "test"}
        normalize_body(body, "kimi")
        self.assertNotIn("messages", body)


# ── _strip_beta_tool_fields (via normalize_body) ───────────────────

class TestStripBetaToolFields(unittest.TestCase):
    def test_defer_loading_removed(self):
        body = {
            "tools": [
                {"name": "Bash", "defer_loading": True,
                 "input_schema": {"type": "object"}},
            ],
            "model": "test",
        }
        normalize_body(body, "deepseek")
        self.assertNotIn("defer_loading", body["tools"][0])

    def test_eager_input_streaming_removed(self):
        body = {
            "tools": [
                {"name": "Read", "eager_input_streaming": True,
                 "input_schema": {"type": "object"}},
            ],
            "model": "test",
        }
        normalize_body(body, "deepseek")
        self.assertNotIn("eager_input_streaming", body["tools"][0])

    def test_clean_tools_unchanged(self):
        body = {
            "tools": [
                {"name": "Write", "input_schema": {"type": "object"}},
            ],
            "model": "test",
        }
        normalize_body(body, "deepseek")
        self.assertEqual(body["tools"][0]["name"], "Write")
        self.assertIn("input_schema", body["tools"][0])

    def test_tools_missing_no_error(self):
        body = {"model": "test"}
        normalize_body(body, "deepseek")
        self.assertNotIn("tools", body)

    def test_both_fields_removed_simultaneously(self):
        body = {
            "tools": [
                {"name": "Multi", "defer_loading": True,
                 "eager_input_streaming": True,
                 "input_schema": {"type": "object"}},
            ],
            "model": "test",
        }
        normalize_body(body, "deepseek")
        self.assertNotIn("defer_loading", body["tools"][0])
        self.assertNotIn("eager_input_streaming", body["tools"][0])
        self.assertEqual(body["tools"][0]["name"], "Multi")


# ── 转换 A：请求向（ADR-010）────────────────────────────────────────

class TestConversionARequest(unittest.TestCase):
    def _conv(self, **body):
        base = {"model": "gpt-4o-mini", "max_tokens": 100,
                "messages": [{"role": "user", "content": "hi"}]}
        base.update(body)
        return anthropic_to_openai_request(base)

    def test_basic_shape(self):
        out = self._conv(system="Be brief.")
        self.assertEqual(out["model"], "gpt-4o-mini")
        self.assertEqual(out["messages"][0],
                         {"role": "system", "content": "Be brief."})
        self.assertEqual(out["messages"][1],
                         {"role": "user", "content": "hi"})
        self.assertEqual(out["max_tokens"], 100)

    def test_system_blocks_flattened(self):
        out = self._conv(system=[{"type": "text", "text": "R1."},
                                 {"type": "text", "text": "R2."}])
        self.assertEqual(out["messages"][0]["content"], "R1.\nR2.")

    def test_max_tokens_field_by_model_family(self):
        self.assertEqual(openai_max_tokens_field("gpt-4o-mini"), "max_tokens")
        self.assertEqual(openai_max_tokens_field("gpt-5.2"), "max_completion_tokens")
        self.assertEqual(openai_max_tokens_field("o3-mini"), "max_completion_tokens")
        out = self._conv(model="gpt-5.2")
        self.assertNotIn("max_tokens", out)
        self.assertEqual(out["max_completion_tokens"], 100)

    def test_stop_sequences_capped_at_four(self):
        out = self._conv(stop_sequences=["a", "b", "c", "d", "e"])
        self.assertEqual(out["stop"], ["a", "b", "c", "d"])

    def test_stream_injects_include_usage(self):
        out = self._conv(stream=True)
        self.assertTrue(out["stream"])
        self.assertEqual(out["stream_options"], {"include_usage": True})
        out = self._conv()
        self.assertNotIn("stream", out)
        self.assertNotIn("stream_options", out)

    def test_tool_use_becomes_tool_calls(self):
        out = self._conv(messages=[
            {"role": "assistant", "content": [
                {"type": "text", "text": "calling"},
                {"type": "tool_use", "id": "tu_1", "name": "get_weather",
                 "input": {"city": "北京"}},
            ]},
        ])
        msg = out["messages"][0]
        self.assertEqual(msg["role"], "assistant")
        self.assertEqual(msg["content"], "calling")
        self.assertEqual(msg["tool_calls"], [{
            "id": "tu_1", "type": "function",
            "function": {"name": "get_weather",
                         "arguments": json.dumps({"city": "北京"},
                                                 ensure_ascii=False)},
        }])

    def test_tool_result_becomes_tool_role_before_text(self):
        out = self._conv(messages=[
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_1",
                 "content": [{"type": "text", "text": "22C"}]},
                {"type": "text", "text": "and then?"},
            ]},
        ])
        self.assertEqual(out["messages"][0],
                         {"role": "tool", "tool_call_id": "tu_1",
                          "content": "22C"})
        self.assertEqual(out["messages"][1],
                         {"role": "user", "content": "and then?"})

    def test_image_blocks_become_image_url_parts(self):
        out = self._conv(messages=[
            {"role": "user", "content": [
                {"type": "image", "source": {
                    "type": "base64", "media_type": "image/png",
                    "data": "QUJD"}},
                {"type": "image", "source": {
                    "type": "url", "url": "https://x/i.png"}},
                {"type": "text", "text": "look"},
            ]},
        ])
        content = out["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "image_url", "image_url": {
            "url": "data:image/png;base64,QUJD"}})
        self.assertEqual(content[1], {"type": "image_url", "image_url": {
            "url": "https://x/i.png"}})
        self.assertEqual(content[2], {"type": "text", "text": "look"})

    def test_single_text_part_uses_string_form(self):
        out = self._conv(messages=[
            {"role": "user", "content": [{"type": "text", "text": "hi"}]},
        ])
        self.assertEqual(out["messages"][0],
                         {"role": "user", "content": "hi"})

    def test_thinking_blocks_dropped(self):
        out = self._conv(messages=[
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "...", "signature": "s"},
                {"type": "text", "text": "answer"},
            ]},
        ])
        self.assertEqual(out["messages"][0]["content"], "answer")
        self.assertNotIn("tool_calls", out["messages"][0])

    def test_tools_and_tool_choice_mapping(self):
        out = self._conv(
            tools=[{"name": "bash", "description": "run",
                    "input_schema": {"type": "object"}}],
            tool_choice={"type": "any"},
        )
        self.assertEqual(out["tools"], [{
            "type": "function",
            "function": {"name": "bash", "description": "run",
                         "parameters": {"type": "object"}},
        }])
        self.assertEqual(out["tool_choice"], "required")
        out = self._conv(tool_choice={"type": "tool", "name": "bash"})
        self.assertEqual(out["tool_choice"],
                         {"type": "function",
                          "function": {"name": "bash"}})

    def test_temperature_top_p_passed_through(self):
        out = self._conv(temperature=0.7, top_p=0.9)
        self.assertEqual(out["temperature"], 0.7)
        self.assertEqual(out["top_p"], 0.9)

    def test_dropped_fields(self):
        out = self._conv(top_k=40, metadata={"user_id": "x"})
        self.assertNotIn("top_k", out)
        self.assertNotIn("metadata", out)


# ── 转换 A：响应向（非流式）─────────────────────────────────────────

class TestConversionAResponse(unittest.TestCase):
    def test_text_response(self):
        payload = {
            "id": "chatcmpl-1", "model": "gpt-4o-mini",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant",
                                     "content": "hello"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5,
                      "prompt_tokens_details": {"cached_tokens": 4}},
        }
        out = openai_chat_response_to_anthropic(payload, model="glm-5.3")
        self.assertEqual(out["type"], "message")
        self.assertEqual(out["model"], "glm-5.3")
        self.assertEqual(out["content"], [{"type": "text", "text": "hello"}])
        self.assertEqual(out["stop_reason"], "end_turn")
        self.assertEqual(out["usage"], {
            "input_tokens": 10, "output_tokens": 5,
            "cache_read_input_tokens": 4})

    def test_tool_calls_response(self):
        payload = {
            "id": "c2", "choices": [{"finish_reason": "tool_calls",
                                     "message": {"content": None,
                                                 "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "f",
                             "arguments": "{\"a\": 1}"}}]}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2},
        }
        out = openai_chat_response_to_anthropic(payload, model="m")
        self.assertEqual(out["stop_reason"], "tool_use")
        block = out["content"][0]
        self.assertEqual(block["type"], "tool_use")
        self.assertEqual(block["id"], "call_1")
        self.assertEqual(block["input"], {"a": 1})

    def test_bad_tool_arguments_fallback(self):
        payload = {"choices": [{"finish_reason": "tool_calls",
                                "message": {"tool_calls": [{
            "id": "c", "function": {"name": "f",
                                    "arguments": "{not json"}}]}}],
                   "usage": {}}
        out = openai_chat_response_to_anthropic(payload, model="m")
        self.assertEqual(out["content"][0]["input"], {"_raw": "{not json"})

    def test_error_conversion(self):
        out = openai_error_to_anthropic(
            {"error": {"message": "bad key", "type": "invalid_request_error"}},
            401)
        self.assertEqual(out["type"], "error")
        self.assertEqual(out["error"]["message"], "bad key")
        out2 = openai_error_to_anthropic(None, 503)
        self.assertIn("503", out2["error"]["message"])


# ── 转换 A：SSE 翻译器（流式）───────────────────────────────────────

def _oai_chunk(delta=None, finish_reason=None, usage=None):
    chunk = {"id": "c", "object": "chat.completion.chunk", "model": "m",
             "choices": [{"index": 0, "delta": delta or {},
                          "finish_reason": finish_reason}]}
    if usage is not None:
        chunk = {"id": "c", "object": "chat.completion.chunk", "model": "m",
                 "choices": [], "usage": usage}
    return ("data: " + json.dumps(chunk) + "\n\n").encode()


class TestConversionASSE(unittest.TestCase):
    def _run(self, chunks, model="m"):
        t = OpenAIChatToAnthropicSSE(model=model, stream_id="msg_test")
        out = b""
        for c in chunks:
            out += t.feed(c)
        out += t.finish()
        return out.decode()

    def _events(self, text):
        evts = []
        for block in text.split("\n\n"):
            for line in block.split("\n"):
                if line.startswith("data: "):
                    evts.append(json.loads(line[6:]))
        return evts

    def test_text_stream_full_sequence(self):
        out = self._run([
            _oai_chunk({"role": "assistant"}),
            _oai_chunk({"content": "Hel"}),
            _oai_chunk({"content": "lo"}),
            _oai_chunk(finish_reason="stop"),
            _oai_chunk(usage={"prompt_tokens": 7, "completion_tokens": 2}),
            b"data: [DONE]\n\n",
        ])
        types = [e["type"] for e in self._events(out)]
        self.assertEqual(types, ["message_start", "content_block_start",
                                 "content_block_delta",
                                 "content_block_delta",
                                 "content_block_stop", "message_delta",
                                 "message_stop"])
        deltas = [e for e in self._events(out)
                  if e["type"] == "content_block_delta"]
        self.assertEqual("".join(d["delta"]["text"] for d in deltas), "Hello")
        md = [e for e in self._events(out) if e["type"] == "message_delta"][0]
        self.assertEqual(md["delta"]["stop_reason"], "end_turn")
        self.assertEqual(md["usage"]["input_tokens"], 7)
        self.assertEqual(md["usage"]["output_tokens"], 2)

    def test_translated_stream_feeds_usage_extractor(self):
        raw = self._run([
            _oai_chunk({"content": "hi"}),
            _oai_chunk(finish_reason="stop"),
            _oai_chunk(usage={"prompt_tokens": 11, "completion_tokens": 3}),
            b"data: [DONE]\n\n",
        ]).encode()
        ex = UsageExtractor()
        ex.feed(raw)
        self.assertEqual(ex.input_tokens, 11)
        self.assertEqual(ex.output_tokens, 3)

    def test_tool_calls_stream(self):
        out = self._run([
            _oai_chunk({"role": "assistant"}),
            _oai_chunk({"tool_calls": [{"index": 0, "id": "call_1",
                                        "function": {"name": "f",
                                                     "arguments": ""}}]}),
            _oai_chunk({"tool_calls": [{"index": 0,
                                        "function": {"arguments": "{\"a\":"}}]}),
            _oai_chunk({"tool_calls": [{"index": 0,
                                        "function": {"arguments": "1}"}}]}),
            _oai_chunk(finish_reason="tool_calls"),
            b"data: [DONE]\n\n",
        ])
        evts = self._events(out)
        start = [e for e in evts if e["type"] == "content_block_start"][0]
        self.assertEqual(start["content_block"]["type"], "tool_use")
        self.assertEqual(start["content_block"]["id"], "call_1")
        json_frags = "".join(
            e["delta"]["partial_json"] for e in evts
            if e["type"] == "content_block_delta")
        self.assertEqual(json_frags, '{"a":1}')
        md = [e for e in evts if e["type"] == "message_delta"][0]
        self.assertEqual(md["delta"]["stop_reason"], "tool_use")

    def test_truncated_stream_finish_emits_close(self):
        # 上游异常截断（无 [DONE]）——finish() 补齐收尾事件
        out = self._run([_oai_chunk({"content": "par"})])
        types = [e["type"] for e in self._events(out)]
        self.assertEqual(types[-2:], ["message_delta", "message_stop"])

    def test_error_chunk_becomes_anthropic_error(self):
        out = self._run([
            b'data: {"error": {"message": "quota exceeded", "type": "insufficient_quota"}}\n\n',
        ])
        evts = self._events(out)
        self.assertEqual(evts[0]["type"], "error")
        self.assertEqual(evts[0]["error"]["message"], "quota exceeded")

    def test_byte_fragmentation(self):
        full = (_oai_chunk({"role": "assistant"})
                + _oai_chunk({"content": "abc"})
                + b"data: [DONE]\n\n")
        t = OpenAIChatToAnthropicSSE(model="m", stream_id="msg_f")
        out = b""
        for i in range(0, len(full), 3):  # 3 字节碎片喂入
            out += t.feed(full[i:i + 3])
        out += t.finish()
        evts = self._events(out.decode())
        types = [e["type"] for e in evts]
        self.assertIn("message_start", types)
        self.assertIn("message_stop", types)

    def test_feed_after_done_is_noop(self):
        t = OpenAIChatToAnthropicSSE(model="m", stream_id="msg_d")
        first = t.feed(_oai_chunk({"content": "x"}) + b"data: [DONE]\n\n")
        self.assertTrue(first)
        self.assertEqual(t.feed(_oai_chunk({"content": "y"})), b"")
        self.assertEqual(t.finish(), b"")


# ── OpenAI 入站形态的 system 提取 / 标签剥离（直通车道 seam）────────

from suanpan.compat import openai_strip_subagent_marker, openai_system_text


class TestOpenAIInboundHelpers(unittest.TestCase):
    def test_system_text_from_first_system_message(self):
        body = {"messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ]}
        self.assertEqual(openai_system_text(body), "be terse")

    def test_system_text_parts_joined(self):
        body = {"messages": [
            {"role": "system", "content": [
                {"type": "text", "text": "R1."},
                {"type": "text", "text": "R2."},
            ]},
        ]}
        self.assertEqual(openai_system_text(body), "R1.\nR2.")

    def test_system_text_missing(self):
        self.assertEqual(openai_system_text({"messages": [
            {"role": "user", "content": "hi"}]}), "")
        self.assertEqual(openai_system_text({}), "")

    def test_strip_marker_in_string_content(self):
        body = {"messages": [
            {"role": "system",
             "content": "before <SUBAGENT-MODEL>oai/m1</SUBAGENT-MODEL> after"},
        ]}
        openai_strip_subagent_marker(body)
        self.assertEqual(body["messages"][0]["content"],
                         "before  after")

    def test_strip_marker_in_parts(self):
        body = {"messages": [
            {"role": "system", "content": [
                {"type": "text", "text": "<SUBAGENT-MODEL>oai/m1</SUBAGENT-MODEL>"},
                {"type": "text", "text": "keep"},
            ]},
        ]}
        openai_strip_subagent_marker(body)
        self.assertEqual(body["messages"][0]["content"][0]["text"], "")
        self.assertEqual(body["messages"][0]["content"][1]["text"], "keep")


# ── Responses 入站形态助手（M3a seam）──────────────────────────────

from suanpan.compat import (
    responses_strip_subagent_marker,
    responses_system_text,
)


class TestResponsesInboundHelpers(unittest.TestCase):
    def test_system_text_from_instructions(self):
        self.assertEqual(
            responses_system_text({"instructions": "be terse"}), "be terse")
        self.assertEqual(responses_system_text({}), "")
        self.assertEqual(responses_system_text({"instructions": 42}), "")

    def test_strip_marker(self):
        body = {"instructions":
                "a <SUBAGENT-MODEL>oai/m1</SUBAGENT-MODEL> b"}
        responses_strip_subagent_marker(body)
        self.assertEqual(body["instructions"], "a  b")
