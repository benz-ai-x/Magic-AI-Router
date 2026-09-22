"""Protocol adaptation for the Suanpan gateway — 协议适配唯一归宿（ADR-010）.

Two families live here, both deterministic pure logic:

1. Anthropic body normalization (``normalize_body``) — strips fields that
   non-native Anthropic-compatible backends reject (unchanged behavior).
2. Conversion A: Anthropic Messages ⇄ OpenAI Chat Completions — lets
   Anthropic-protocol clients (Claude Code) use openai-protocol backends
   (OpenAI, OpenRouter, 通义, 硅基流动 …). The SSE translator emits a
   valid Anthropic event stream, so ``UsageExtractor`` never needs to
   know OpenAI's chunk grammar.
"""
from __future__ import annotations

import itertools
import json
import re
from typing import Any

_STREAM_SEQ = itertools.count()


def extract_system_text(body: dict[str, Any]) -> str:
    """Extract the ``system`` prompt as a string, regardless of format.

    Handles three shapes: plain string (returned as-is), content-block
    array (text blocks joined with newlines), and missing/None ("").
    This is the single source of truth for "read system from an
    Anthropic Messages body" — used by routing decisions and by
    ``_flatten_system`` for provider compatibility.
    """
    system = body.get("system")
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts: list[str] = []
        for item in system:
            if isinstance(item, dict) and item.get("type") == "text":
                t = item.get("text")
                if isinstance(t, str):
                    parts.append(t)
        return "\n".join(parts)
    return ""


def normalize_body(body: dict[str, Any], provider: str,
                   *, anthropic_native: bool = False) -> None:
    """Normalize request body in-place before forwarding to *provider*.

    The default path applies uniformly to all providers (no per-provider
    branching) because every transformation removes a field no known
    provider supports. ``anthropic_native=True`` opts a provider out of all
    stripping: the backend natively accepts Anthropic body shapes, so the
    ``cache_control`` markers Claude Code relies on for prompt caching
    (they live on the system content blocks), document blocks, and beta
    tool fields must survive the hop.
    """
    if anthropic_native:
        return
    _flatten_system(body)
    _strip_document_blocks(body)
    _strip_beta_tool_fields(body)


def _flatten_system(body: dict[str, Any]) -> None:
    """Convert ``system`` from content-block array to plain string.

    Claude Code v2.1.154+ sends ``system`` as an array of content blocks
    with ``cache_control`` markers::

        [{"type": "text", "text": "...", "cache_control": {"type": "ephemeral"}}]

    DeepSeek rejects this format (deepseek-ai/DeepSeek-V3#1369). We flatten
    it to a plain string, preserving all text content, dropping
    ``cache_control``.
    """
    if isinstance(body.get("system"), list):
        body["system"] = extract_system_text(body)


def _strip_document_blocks(body: dict[str, Any]) -> None:
    """Remove ``document`` content blocks from messages.

    KIMI rejects ``document`` content blocks outright
    (MoonshotAI/Kimi-K2#129). We remove them from every message's
    ``content`` array. If a message would be left with empty content,
    we insert a single empty text block as a placeholder to avoid
    provider errors on empty content arrays.
    """
    messages = body.get("messages")
    if not isinstance(messages, list):
        return
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        filtered = [b for b in content
                    if not (isinstance(b, dict) and b.get("type") == "document")]
        if len(filtered) != len(content):
            if not filtered:
                filtered = [{"type": "text", "text": ""}]
            msg["content"] = filtered


def _strip_beta_tool_fields(body: dict[str, Any]) -> None:
    """Remove Anthropic beta tool-schema fields from tools.

    Claude Code 5 sends ``defer_loading`` and ``eager_input_streaming``
    on tool objects. These are Anthropic-specific beta fields whose
    compatibility with every upstream provider is unknown. We strip
    them defensively — this is the gateway-side complement to the
    client-side ``CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`` env var.
    """
    tools = body.get("tools")
    if not isinstance(tools, list):
        return
    for tool in tools:
        if isinstance(tool, dict):
            tool.pop("defer_loading", None)
            tool.pop("eager_input_streaming", None)


# ── 转换 A：Anthropic Messages ⇄ OpenAI Chat Completions（ADR-010）──
# 服务「Anthropic 入站 × openai 端点」象限：Claude Code 等纯 Anthropic
# 客户端使用 OpenAI/OpenRouter/通义/硅基流动等 openai 协议厂商。
# 全部为确定性纯函数/纯状态机——前缀稳定性契约（ADR-004）不适用于
# openai 车道（cache_control 在请求向被丢弃，OpenAI 协议无此概念）。

# OpenAI 新推理系模型拒绝 max_tokens（要求 max_completion_tokens）；
# 其余厂商兼容面以 max_tokens 为准（deepseek/qwen/siliconflow 文档口径）
_NEEDS_COMPLETION_TOKENS = re.compile(r"^(gpt-5|o[134](\b|-))")


def openai_max_tokens_field(model: str) -> str:
    """OpenAI 系端点的最大输出参数名（转换层与测试探针共消费）。"""
    return ("max_completion_tokens"
            if _NEEDS_COMPLETION_TOKENS.match(str(model)) else "max_tokens")


def openai_system_text(body: dict[str, Any]) -> str:
    """OpenAI Chat body 的 system 提取（router ``system_text`` seam 的
    openai 形态实现）：messages 首条 system 消息（字符串或多部分）。"""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [p.get("text", "") for p in content
                     if isinstance(p, dict) and p.get("type") == "text"]
            return "\n".join(parts)
        return ""
    return ""


def openai_strip_subagent_marker(body: dict[str, Any]) -> None:
    """OpenAI Chat body 的 <SUBAGENT-MODEL> 标签剥离（router.strip_marker
    的 openai 形态镜像——改首条 system 消息的字符串内容）。"""
    from suanpan.router import SUBAGENT_RE
    messages = body.get("messages")
    if not isinstance(messages, list):
        return
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = SUBAGENT_RE.sub("", content)
        elif isinstance(content, list):
            for p in content:
                if isinstance(p, dict) and isinstance(p.get("text"), str):
                    p["text"] = SUBAGENT_RE.sub("", p["text"])
        return


def responses_system_text(body: dict[str, Any]) -> str:
    """OpenAI Responses body 的 system 提取：``instructions`` 字段（纯串）。"""
    instr = body.get("instructions")
    return instr if isinstance(instr, str) else ""


def responses_strip_subagent_marker(body: dict[str, Any]) -> None:
    """Responses body 的 <SUBAGENT-MODEL> 标签剥离（instructions 串）。"""
    from suanpan.router import SUBAGENT_RE
    instr = body.get("instructions")
    if isinstance(instr, str):
        body["instructions"] = SUBAGENT_RE.sub("", instr)

_FINISH_TO_STOP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "refusal",
}


def _block_text(blocks: list) -> str:
    """content-block 数组 → 纯文本（tool_result 内容提取用）。"""
    parts: list[str] = []
    for b in blocks:
        if isinstance(b, dict) and b.get("type") == "text":
            t = b.get("text")
            if isinstance(t, str):
                parts.append(t)
        elif isinstance(b, str):
            parts.append(b)
    return "\n".join(parts)


def _image_part(block: dict) -> dict | None:
    """Anthropic image block → OpenAI image_url part（两种 source 形态）。"""
    source = block.get("source")
    if not isinstance(source, dict):
        return None
    if source.get("type") == "base64":
        url = f"data:{source.get('media_type', 'image/png')};base64,{source.get('data', '')}"
    elif source.get("type") == "url":
        url = source.get("url", "")
    else:
        return None
    return {"type": "image_url", "image_url": {"url": url}}


def _user_blocks_to_parts(content: list) -> list[dict]:
    """user content blocks → OpenAI content parts（text/image_url）。"""
    parts: list[dict] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            parts.append({"type": "text", "text": b.get("text", "")})
        elif b.get("type") == "image":
            part = _image_part(b)
            if part:
                parts.append(part)
        # document/unknown 块丢弃（openai chat v1 无等价形态）
    return parts


def _user_message_to_openai(content) -> list[dict]:
    """Anthropic user 消息 → OpenAI 消息序列。

    tool_result 块必须先于其余内容（Anthropic 语义即如此）且各自独立成
    ``role:tool`` 消息；剩余 text/image 组合为一条 user 消息。
    """
    out: list[dict] = []
    rest: list[dict] = []
    if isinstance(content, str):
        return [{"role": "user", "content": content}] if content else []
    if not isinstance(content, list):
        return []
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "tool_result":
            inner = b.get("content")
            text = inner if isinstance(inner, str) else _block_text(
                inner if isinstance(inner, list) else [])
            out.append({"role": "tool",
                        "tool_call_id": str(b.get("tool_use_id", "")),
                        "content": text})
        else:
            rest.append(b)
    if rest:
        parts = _user_blocks_to_parts(rest)
        if parts:
            # 单一 text part 用纯字符串形态（最大兼容面）
            if len(parts) == 1 and parts[0].get("type") == "text":
                out.append({"role": "user", "content": parts[0]["text"]})
            else:
                out.append({"role": "user", "content": parts})
    return out


def _assistant_message_to_openai(content) -> list[dict]:
    """Anthropic assistant 消息 → 单条 OpenAI assistant 消息。

    text 块拼接为 content；tool_use 块映射为 tool_calls；thinking 块
    丢弃（推理不可跨协议移植，重推理成本经用量可见）。
    """
    if isinstance(content, str):
        return [{"role": "assistant", "content": content}]
    if not isinstance(content, list):
        return [{"role": "assistant", "content": ""}]
    texts: list[str] = []
    tool_calls: list[dict] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text" and isinstance(b.get("text"), str):
            texts.append(b["text"])
        elif b.get("type") == "tool_use":
            tool_calls.append({
                "id": str(b.get("id", "")),
                "type": "function",
                "function": {
                    "name": str(b.get("name", "")),
                    "arguments": json.dumps(b.get("input") or {},
                                            ensure_ascii=False),
                },
            })
    msg: dict[str, Any] = {"role": "assistant"}
    if tool_calls:
        msg["content"] = "\n".join(texts) if texts else None
        msg["tool_calls"] = tool_calls
    else:
        msg["content"] = "\n".join(texts)
    return [msg]


def anthropic_to_openai_request(body: dict[str, Any]) -> dict[str, Any]:
    """Anthropic Messages 请求体 → OpenAI Chat Completions 请求体（新 dict）。

    调用方应已完成 ``model`` 改写（路由目标）。映射要点：system（含
    blocks）→ 首条 system 消息；tool_use/tool_result → tool_calls/tool
    角色；stop_sequences → stop（OpenAI 上限 4）；tools 的 input_schema →
    parameters；cache_control/top_k/metadata 丢弃；stream 时注入
    stream_options.include_usage（末块用量，转换层与统计依赖它）。
    """
    out: dict[str, Any] = {"model": body.get("model", "")}

    messages: list[dict] = []
    system_text = extract_system_text(body)
    if system_text:
        messages.append({"role": "system", "content": system_text})
    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "assistant":
            messages.extend(_assistant_message_to_openai(msg.get("content")))
        else:
            messages.extend(_user_message_to_openai(msg.get("content")))
    out["messages"] = messages

    max_tokens = body.get("max_tokens")
    if isinstance(max_tokens, int):
        out[openai_max_tokens_field(out["model"])] = max_tokens

    stop = body.get("stop_sequences")
    if isinstance(stop, list) and stop:
        out["stop"] = [str(s) for s in stop[:4]]

    for src, dst in (("temperature", "temperature"), ("top_p", "top_p")):
        v = body.get(src)
        if isinstance(v, (int, float)):
            out[dst] = v

    if body.get("stream"):
        out["stream"] = True
        out["stream_options"] = {"include_usage": True}

    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        oai_tools = []
        for t in tools:
            if not isinstance(t, dict):
                continue
            fn: dict[str, Any] = {"name": str(t.get("name", "")),
                                  "parameters": t.get("input_schema") or {}}
            if isinstance(t.get("description"), str):
                fn["description"] = t["description"]
            oai_tools.append({"type": "function", "function": fn})
        if oai_tools:
            out["tools"] = oai_tools

    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        t = tc.get("type")
        if t == "auto":
            out["tool_choice"] = "auto"
        elif t == "any":
            out["tool_choice"] = "required"
        elif t == "tool":
            out["tool_choice"] = {"type": "function",
                                  "function": {"name": str(tc.get("name", ""))}}

    return out


def _usage_from_openai(usage: dict | None) -> dict:
    """OpenAI usage 对象 → Anthropic usage 形态（cached_tokens 顺带保留）。"""
    usage = usage or {}
    cached = 0
    details = usage.get("prompt_tokens_details")
    if isinstance(details, dict):
        cached = details.get("cached_tokens") or 0
    return {
        "input_tokens": usage.get("prompt_tokens") or 0,
        "output_tokens": usage.get("completion_tokens") or 0,
        "cache_read_input_tokens": cached,
    }


def openai_chat_response_to_anthropic(payload: dict[str, Any],
                                       *, model: str) -> dict[str, Any]:
    """OpenAI Chat 非流式响应 → Anthropic Messages 响应（新 dict）。"""
    choice = (payload.get("choices") or [{}])[0] or {}
    message = choice.get("message") or {}
    content: list[dict] = []
    raw_content = message.get("content")
    if isinstance(raw_content, str) and raw_content:
        content.append({"type": "text", "text": raw_content})
    elif isinstance(raw_content, list):
        for part in raw_content:
            if isinstance(part, dict) and part.get("type") == "text":
                content.append({"type": "text", "text": part.get("text", "")})
    for tc in message.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {"_raw": fn.get("arguments")}
        content.append({"type": "tool_use", "id": str(tc.get("id", "")),
                        "name": str(fn.get("name", "")), "input": args})
    return {
        "id": str(payload.get("id", "")),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": _FINISH_TO_STOP.get(choice.get("finish_reason"),
                                           "end_turn"),
        "stop_sequence": None,
        "usage": _usage_from_openai(payload.get("usage")),
    }


def openai_error_to_anthropic(payload: dict[str, Any] | None,
                              status_code: int) -> dict[str, Any]:
    """OpenAI 形态错误响应 → Anthropic 错误形态（客户端 SDK 才认得）。"""
    message = ""
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            message = str(err.get("message", ""))
        elif isinstance(err, str):
            message = err
        elif payload.get("message"):
            message = str(payload["message"])
    if not message:
        message = f"upstream error (HTTP {status_code})"
    return {"type": "error",
            "error": {"type": "api_error", "message": message}}


def _sse_event(event: dict[str, Any]) -> bytes:
    """Anthropic SSE 帧序列化（event: + data: 双行，与官方线格式一致）。"""
    lines = [f"event: {event['type']}", f"data: {json.dumps(event, ensure_ascii=False)}"]
    return ("\n".join(lines) + "\n\n").encode()


class OpenAIChatToAnthropicSSE:
    """Incremental OpenAI chat SSE → Anthropic Messages SSE translator.

    ``feed(chunk)`` 返回可直接下发的 Anthropic SSE 字节（可能为空）；
    ``finish()`` 冲洗尾部并补齐收尾事件（上游异常截断、无 ``[DONE]``）。
    输出即合法 Anthropic 事件流——UsageExtractor 无需感知 openai 分块
    文法（用量来自 include_usage 的末块，注入 message_delta）。
    """

    def __init__(self, *, model: str, stream_id: str | None = None) -> None:
        self._model = model
        self._id = stream_id or f"msg_suanpan{next(_STREAM_SEQ):016d}"
        self._buf = bytearray()
        self._started = False
        self._block_index = -1      # 当前打开的 content block 下标
        self._block_type: str | None = None
        self._tool_block_for: dict[int, int] = {}  # openai tool index → block index
        self._finish_reason: str | None = None
        self._usage: dict | None = None
        self._done = False

    # ── public ──
    def feed(self, chunk: bytes) -> bytes:
        if self._done:
            return b""
        self._buf += chunk
        out = bytearray()
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            line = bytes(self._buf[:nl])
            del self._buf[:nl + 1]
            line = line.rstrip(b"\r")
            if not line.startswith(b"data:"):
                continue  # event:/注释/空行无 payload
            data = line[5:]
            if data.startswith(b" "):
                data = data[1:]
            if data.strip() == b"[DONE]":
                out += self._close()
                return bytes(out)
            try:
                evt = json.loads(data)
            except ValueError:
                continue
            if isinstance(evt, dict):
                out += self._on_chunk(evt)
        return bytes(out)

    def finish(self) -> bytes:
        if self._done:
            return b""
        self._buf.clear()
        return self._close()

    # ── internals ──
    def _on_chunk(self, evt: dict) -> bytes:
        if isinstance(evt.get("error"), dict):
            # 上游流中错误：转 Anthropic error 事件并终止翻译
            self._done = True
            err = evt["error"]
            return _sse_event({"type": "error", "error": {
                "type": str(err.get("type", "api_error")),
                "message": str(err.get("message", ""))}})
        if isinstance(evt.get("usage"), dict) and evt["usage"]:
            self._usage = evt["usage"]
        choices = evt.get("choices")
        if not isinstance(choices, list) or not choices:
            return b""
        choice = choices[0]
        if not isinstance(choice, dict):
            return b""
        if choice.get("finish_reason"):
            self._finish_reason = choice["finish_reason"]

        out = bytearray()
        if not self._started:
            self._started = True
            out += _sse_event({"type": "message_start", "message": {
                "id": self._id, "type": "message", "role": "assistant",
                "model": self._model, "content": [], "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}}})

        delta = choice.get("delta")
        if not isinstance(delta, dict):
            return bytes(out)

        text = delta.get("content")
        if isinstance(text, str) and text:
            if self._block_type != "text":
                out += self._close_block()
                self._block_index += 1
                self._block_type = "text"
                out += _sse_event({
                    "type": "content_block_start", "index": self._block_index,
                    "content_block": {"type": "text", "text": ""}})
            out += _sse_event({
                "type": "content_block_delta", "index": self._block_index,
                "delta": {"type": "text_delta", "text": text}})

        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            idx = tc.get("index", 0)
            if isinstance(idx, bool) or not isinstance(idx, int):
                idx = 0
            fn = tc.get("function") or {}
            if idx not in self._tool_block_for:
                out += self._close_block()
                self._block_index += 1
                self._tool_block_for[idx] = self._block_index
                self._block_type = "tool_use"
                out += _sse_event({
                    "type": "content_block_start", "index": self._block_index,
                    "content_block": {"type": "tool_use",
                                      "id": str(tc.get("id", "")),
                                      "name": str(fn.get("name", "")),
                                      "input": {}}})
            arguments = fn.get("arguments")
            if isinstance(arguments, str) and arguments:
                out += _sse_event({
                    "type": "content_block_delta",
                    "index": self._tool_block_for[idx],
                    "delta": {"type": "input_json_delta",
                              "partial_json": arguments}})
        return bytes(out)

    def _close_block(self) -> bytes:
        if self._block_type is None:
            return b""
        evt = _sse_event({"type": "content_block_stop",
                          "index": self._block_index})
        self._block_type = None
        return evt

    def _close(self) -> bytes:
        self._done = True
        out = bytearray()
        if not self._started:
            # 上游在首个 data 前就结束（异常流）——仍产出完整事件序列，
            # 客户端 SDK 才不会挂在等 message_start 上
            out += _sse_event({"type": "message_start", "message": {
                "id": self._id, "type": "message", "role": "assistant",
                "model": self._model, "content": [], "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0}}})
            self._started = True
        out += self._close_block()
        usage = _usage_from_openai(self._usage)
        out += _sse_event({
            "type": "message_delta",
            "delta": {"stop_reason": _FINISH_TO_STOP.get(self._finish_reason,
                                                         "end_turn"),
                      "stop_sequence": None},
            "usage": usage})
        out += _sse_event({"type": "message_stop"})
        return bytes(out)
