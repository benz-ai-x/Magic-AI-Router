"""SIT for ai_capture_addon — composes the addon with REAL mitmproxy flow
objects (mitmproxy.test.tflow builds actual Request/Response/HTTPFlow) and the
real filesystem JSONL sink. This validates the duck-typed flow assumptions in
the addon hooks against the genuine mitmproxy API, plus the end-to-end
request -> reassemble -> JSONL path for ≥2 providers and non-AI passthrough.

Run under the mitmproxy venv:
    <venv>/bin/python -m pytest tests/sit/test_ai_capture_sit.py -v

Upstream payloads are local fixtures modeled on documented provider wire
formats (no live keys / no network / no cost); the mitmproxy composition and
the JSONL output are real.
"""
import json

import pytest

pytest.importorskip("mitmproxy")
from mitmproxy.test import tflow, tutils  # noqa: E402

from capture import ai_capture_addon as addon  # noqa: E402
from capture import capture_store  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_capture_home(tmp_path, monkeypatch):
    monkeypatch.setattr(capture_store, "_home_dir", lambda: str(tmp_path))
    monkeypatch.setattr(capture_store, "DEFAULT_CAPTURE_DIR", str(tmp_path))


def _real_flow(host, path, req_body, resp_text, status=200, ctype="text/event-stream"):
    req = tutils.treq(
        method=b"POST", host=host, port=443, path=path.encode(),
        content=json.dumps(req_body).encode(),
        headers=[(b"content-type", b"application/json"), (b"host", host.encode())],
    )
    resp = tutils.tresp(status_code=status, content=resp_text.encode(),
                        headers=[(b"content-type", ctype.encode())])
    return tflow.tflow(req=req, resp=resp)


def _run(tmpdir, flow):
    a = addon.AICaptureAddon()
    a.capture_dir = str(tmpdir)
    a.request(flow)
    a.response(flow)
    return a


def _records(tmpdir):
    files = list(tmpdir.glob("*.jsonl"))
    if not files:
        return []
    return [json.loads(x) for x in files[0].read_text(encoding="utf-8").splitlines()]


def test_sit_openai_chat_completions_stream(tmp_path):
    body = {"model": "gpt-4o", "stream": True, "messages": [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "用一句话解释 TLS 握手"}]}
    sse = (
        'data: {"choices":[{"delta":{"role":"assistant"},"index":0}]}\n\n'
        'data: {"choices":[{"delta":{"content":"TLS 握手"},"index":0}]}\n\n'
        'data: {"choices":[{"delta":{"content":"是加密协商过程。"},"index":0}]}\n\n'
        'data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}],'
        '"usage":{"prompt_tokens":20,"completion_tokens":8,"total_tokens":28}}\n\n'
        'data: [DONE]\n\n'
    )
    flow = _real_flow("api.openai.com", "/v1/chat/completions", body, sse)
    _run(tmp_path, flow)
    rec = _records(tmp_path)[0]
    assert rec["provider"] == "openai"
    assert rec["request"]["system"] == "You are a helpful assistant."
    assert rec["request"]["messages"][1] == {"role": "user", "content": "用一句话解释 TLS 握手"}
    assert rec["response"]["reassembled"] == "TLS 握手是加密协商过程。"
    assert rec["usage"]["total_tokens"] == 28
    assert rec["capture_error"] is None


def test_sit_anthropic_messages_stream(tmp_path):
    body = {"model": "claude-opus-4-8", "stream": True, "system": "You are Claude.",
            "messages": [{"role": "user", "content": "你好"}]}
    sse = (
        'event: message_start\n'
        'data: {"type":"message_start","message":{"usage":{"input_tokens":10,"output_tokens":1}}}\n\n'
        'event: content_block_start\n'
        'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"你好"}}\n\n'
        'event: content_block_delta\n'
        'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"！很高兴见到你。"}}\n\n'
        'event: content_block_stop\n'
        'data: {"type":"content_block_stop","index":0}\n\n'
        'event: message_delta\n'
        'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":9}}\n\n'
        'event: message_stop\n'
        'data: {"type":"message_stop"}\n\n'
    )
    flow = _real_flow("api.anthropic.com", "/v1/messages", body, sse)
    _run(tmp_path, flow)
    rec = _records(tmp_path)[0]
    assert rec["provider"] == "anthropic"
    assert rec["request"]["system"] == "You are Claude."
    assert rec["request"]["messages"][0] == {"role": "user", "content": "你好"}
    assert rec["response"]["reassembled"] == "你好！很高兴见到你。"
    assert rec["response"]["finish_reason"] == "end_turn"
    assert rec["usage"]["input_tokens"] == 10 and rec["usage"]["output_tokens"] == 9


def test_sit_deepseek_non_stream_reasoning(tmp_path):
    body = {"model": "deepseek-reasoner", "stream": False,
            "messages": [{"role": "user", "content": "1+1=?"}]}
    resp = json.dumps({"choices": [{"message": {"role": "assistant",
            "reasoning_content": "简单加法", "content": "2"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 12}})
    flow = _real_flow("api.deepseek.com", "/v1/chat/completions", body, resp, ctype="application/json")
    _run(tmp_path, flow)
    rec = _records(tmp_path)[0]
    assert rec["provider"] == "deepseek"
    assert rec["response"]["reassembled"] == "2"
    assert rec["response"]["reasoning"] == "简单加法"


def test_sit_non_ai_traffic_passes_through(tmp_path):
    flow = _real_flow("example.com", "/api/things", {"q": "x"},
                      json.dumps({"ok": True}), ctype="application/json")
    _run(tmp_path, flow)
    assert _records(tmp_path) == []  # nothing captured, no JSONL file created
