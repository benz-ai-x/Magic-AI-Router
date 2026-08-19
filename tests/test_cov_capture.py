"""Coverage push for the capture subsystem (ai_capture_addon / capture / capture_store).

Targets the specific uncovered branches reported by --cov-report=term-missing:
defensive paths (OSError handlers, JSON decode fallbacks), rarely-hit
normalize branches, and race-condition guards in capture_store.
"""
import json
import os
import stat
import types
import pytest
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock

import ai_capture_addon as addon
import capture
import capture_store

# Save references to real implementations before any autouse fixture mocks them.
_REAL_HOME_DIR = capture_store._home_dir


# ===========================================================================
# Shared fixtures
# ===========================================================================

@pytest.fixture(autouse=True)
def _sandbox_home(tmp_path, monkeypatch):
    """Isolate HOME so capture_store never touches the real ~/.magic-proxy-captures."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(capture_store, "DEFAULT_CAPTURE_DIR", str(tmp_path / "captures"))
    monkeypatch.setattr(capture_store, "_home_dir", lambda: os.path.realpath(tmp_path))
    monkeypatch.setattr(addon, "_DEFAULT_DIR", str(tmp_path / "captures"))


def _sse(*events):
    blocks = []
    for ev in events:
        name, data = ev if isinstance(ev, tuple) else (None, ev)
        lines = []
        if name:
            lines.append(f"event: {name}")
        lines.append("data: " + (data if isinstance(data, str) else json.dumps(data)))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks) + "\n\n"


# ===========================================================================
# ai_capture_addon — identify() non-chat return-None branches
# ===========================================================================

class TestIdentifyNonChatReturns:
    @pytest.mark.parametrize("host, path", [
        ("api.anthropic.com", "/v1/complete"),
        ("api.deepseek.com", "/v1/models"),
        ("ark.cn-beijing.volces.com", "/api/v3/embed"),
        ("dashscope.aliyuncs.com", "/compatible-mode/v1/embeddings"),
        ("api.minimaxi.com", "/v1/text/chatcompletion_v1"),
    ])
    def test_non_chat_returns_none(self, host, path):
        assert addon.identify(host, path) is None


# ===========================================================================
# ai_capture_addon — _part_to_text / _content_to_text edge branches
# ===========================================================================

class TestPartToTextEdges:
    def test_str_part_returns_directly(self):
        assert addon._part_to_text("hello") == "hello"

    def test_dashscope_bare_key_placeholder(self):
        assert addon._part_to_text({"audio": "data..."}) == "[audio]"

    def test_non_dict_non_str_returns_empty(self):
        assert addon._part_to_text(42) == ""
        assert addon._part_to_text(None) == ""

    def test_content_to_text_falls_through_to_str(self):
        assert addon._content_to_text(123) == "123"

    def test_content_to_text_none_returns_empty(self):
        assert addon._content_to_text(None) == ""


# ===========================================================================
# ai_capture_addon — extract_request edge branches
# ===========================================================================

class TestExtractRequestEdges:
    def test_responses_input_neither_str_nor_list(self):
        out = addon.extract_request("responses", {"model": "gpt", "input": 42})
        assert out["messages"] == []

    def test_dashscope_native_system_from_input_when_missing(self):
        body = {
            "model": "qwen",
            "input": {"messages": [{"role": "user", "content": "hi"}], "system": "sys-msg"},
            "parameters": {},
        }
        out = addon.extract_request("dashscope.native", body)
        assert out["system"] == "sys-msg"

    def test_unknown_variant_returns_empty_messages(self):
        out = addon.extract_request("unknown.variant", {"model": "x"})
        assert out["messages"] == []
        assert out["system"] is None


# ===========================================================================
# ai_capture_addon — SSE reassembler defensive branches
# ===========================================================================

class TestReassembleDefensive:
    def test_openai_stream_empty_choices_continue(self):
        body = _sse(
            {"usage": {"total_tokens": 1}},
            {"choices": [{"delta": {"content": "ok"}}]},
            "[DONE]",
        )
        out = addon.reassemble("chat.completions", True, body)
        assert out["reassembled"] == "ok"

    def test_anthropic_stream_bad_json_skipped(self):
        body = _sse(
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": "hi"}}),
            "not-valid-json",
            ("message_stop", {"type": "message_stop"}),
        )
        out = addon.reassemble("messages", True, body)
        assert out["reassembled"] == "hi"

    def test_responses_stream_bad_json_skipped(self):
        body = _sse(
            ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "x"}),
            "not-json",
            ("response.completed", {"type": "response.completed",
                                    "response": {"usage": {"total": 1}, "status": "completed"}}),
        )
        out = addon.reassemble("responses", True, body)
        assert out["reassembled"] == "x"

    def test_responses_stream_reasoning_summary_delta(self):
        body = _sse(
            ("response.reasoning_summary_text.delta",
             {"type": "response.reasoning_summary_text.delta", "delta": "summary"}),
            ("response.completed",
             {"type": "response.completed", "response": {"status": "completed", "usage": {"total": 1}}}),
        )
        out = addon.reassemble("responses", True, body)
        assert out["reasoning"] == "summary"

    def test_responses_stream_function_call_output_item_and_args(self):
        body = _sse(
            ("response.output_item.added",
             {"type": "response.output_item.added", "output_index": 0,
              "item": {"type": "function_call", "name": "get_weather", "arguments": ""}}),
            ("response.function_call_arguments.delta",
             {"type": "response.function_call_arguments.delta", "output_index": 0,
              "delta": '{"city":"SF"}'}),
            ("response.completed",
             {"type": "response.completed", "response": {"status": "completed", "usage": {"total": 1}}}),
        )
        out = addon.reassemble("responses", True, body)
        assert out["tool_calls"] == [{"name": "get_weather", "arguments": '{"city":"SF"}'}]

    def test_responses_stream_output_item_added_non_function(self):
        body = _sse(
            ("response.output_item.added",
             {"type": "response.output_item.added", "output_index": 0,
              "item": {"type": "message", "role": "assistant"}}),
            ("response.completed",
             {"type": "response.completed", "response": {"status": "completed", "usage": {"total": 1}}}),
        )
        out = addon.reassemble("responses", True, body)
        assert out["tool_calls"] == []

    def test_responses_stream_incomplete_status(self):
        body = _sse(
            ("response.incomplete",
             {"type": "response.incomplete", "response": {"status": "incomplete", "usage": {"total": 1}}}),
        )
        out = addon.reassemble("responses", True, body)
        assert out["finish_reason"] == "incomplete"

    def test_dashscope_stream_bad_json_skipped(self):
        body = _sse(
            {"output": {"text": "ok"}, "usage": {"total_tokens": 1}},
            "not-json",
            {"output": {"text": "ok2", "finish_reason": "stop"}},
        )
        out = addon.reassemble("dashscope.native", True, body)
        assert out["reassembled"] == "ok2"

    def test_dashscope_stream_switches_to_incremental_on_non_prefix(self):
        body = _sse(
            {"output": {"text": "part1"}},
            {"output": {"text": "part2"}},
        )
        out = addon.reassemble("dashscope.native", True, body)
        assert out["reassembled"] == "part1part2"

    def test_anthropic_non_stream_thinking_and_tool_use(self):
        body = json.dumps({
            "content": [
                {"type": "thinking", "thinking": "reasoning text"},
                {"type": "tool_use", "name": "calc", "input": {"x": 1}},
                {"type": "text", "text": "answer"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        })
        out = addon.reassemble("messages", False, body)
        assert out["reassembled"] == "answer"
        assert out["reasoning"] == "reasoning text"
        assert out["tool_calls"] == [{"name": "calc", "arguments": '{"x": 1}'}]

    def test_chat_completions_non_stream_reasoning_content(self):
        body = json.dumps({
            "choices": [{"message": {"content": "ans", "reasoning_content": "thinking"},
                         "finish_reason": "stop"}],
            "usage": {"total_tokens": 3},
        })
        out = addon.reassemble("chat.completions", False, body)
        assert out["reassembled"] == "ans"
        assert out["reasoning"] == "thinking"

    def test_reassemble_empty_body_returns_blank(self):
        out = addon.reassemble("chat.completions", True, "")
        assert out["reassembled"] == ""
        assert out["event_count"] == 0

    def test_non_stream_bad_json_returns_blank(self):
        out = addon.reassemble("chat.completions", False, "not-json")
        assert out["reassembled"] == ""


# ===========================================================================
# ai_capture_addon — AICaptureAddon hook defensive branches
# ===========================================================================

def _flow(host, path, method="POST", req_body=None, resp_text="", status=200):
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


class TestAddonHookDefensive:
    def _addon(self, tmp_path):
        a = addon.AICaptureAddon()
        a.capture_dir = str(tmp_path)
        return a

    def test_request_bad_json_body_falls_back_to_empty_dict(self, tmp_path):
        a = self._addon(tmp_path)
        flow = _flow("api.openai.com", "/v1/chat/completions", resp_text="{}")
        flow.request.content = b"not-json-at-all"
        flow.request.raw_content = b"not-json-at-all"
        a.request(flow)
        meta = flow.metadata["ai_capture"]
        assert meta["provider"] == "openai"
        assert meta["model"] is None

    def test_responseheaders_no_meta_returns(self, tmp_path):
        a = self._addon(tmp_path)
        a.preserve_streaming = True
        flow = _flow("example.com", "/whatever")
        a.responseheaders(flow)
        assert getattr(flow.response, "stream", None) is None

    def test_responseheaders_stream_false_returns_even_with_meta(self, tmp_path):
        a = self._addon(tmp_path)
        a.preserve_streaming = True
        flow = _flow("api.openai.com", "/v1/chat/completions",
                     req_body={"model": "gpt", "stream": False, "messages": []})
        a.request(flow)
        a.responseheaders(flow)
        assert getattr(flow.response, "stream", None) is None

    def test_response_hook_swallows_write_exception(self, tmp_path, monkeypatch):
        a = self._addon(tmp_path)
        flow = _flow("api.openai.com", "/v1/chat/completions",
                     req_body={"model": "gpt", "stream": True, "messages": []},
                     resp_text=_sse({"choices": [{"delta": {"content": "x"}}]}, "[DONE]"))
        a.request(flow)

        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(addon, "write_jsonl", boom)
        a.response(flow)
        assert not flow.metadata["ai_capture"].get("_written")

    def test_error_hook_no_meta_returns(self, tmp_path):
        a = self._addon(tmp_path)
        flow = _flow("example.com", "/whatever")
        a.error(flow)
        assert flow.metadata == {}

    def test_error_hook_already_written_returns(self, tmp_path):
        a = self._addon(tmp_path)
        flow = _flow("api.openai.com", "/v1/chat/completions",
                     req_body={"model": "gpt", "stream": True, "messages": []})
        a.request(flow)
        flow.metadata["ai_capture"]["_written"] = True
        a.error(flow)
        assert list(tmp_path.iterdir()) == []

    def test_error_hook_swallows_exception(self, tmp_path, monkeypatch):
        a = self._addon(tmp_path)
        flow = _flow("api.openai.com", "/v1/chat/completions",
                     req_body={"model": "gpt", "stream": True, "messages": []})
        a.request(flow)

        def boom(*a, **k):
            raise OSError("disk full")
        monkeypatch.setattr(addon, "write_jsonl", boom)
        a.error(flow)


# ===========================================================================
# ai_capture_addon — truncate utility edge cases
# ===========================================================================

class TestTruncateValue:
    def test_truncates_long_string(self):
        val, truncated = addon._truncate_value("x" * (addon.MAX_CAPTURE_TEXT_CHARS + 10))
        assert len(val) == addon.MAX_CAPTURE_TEXT_CHARS
        assert truncated is True

    def test_passthrough_short_string(self):
        val, truncated = addon._truncate_value("short")
        assert val == "short" and truncated is False

    def test_truncates_nested_list_and_dict(self):
        long = "x" * (addon.MAX_CAPTURE_TEXT_CHARS + 1)
        val, truncated = addon._truncate_value([{"content": long}])
        assert len(val[0]["content"]) == addon.MAX_CAPTURE_TEXT_CHARS
        assert truncated is True

    def test_non_container_passthrough(self):
        val, truncated = addon._truncate_value(42)
        assert val == 42 and truncated is False


# ===========================================================================
# capture_store — defensive guard branches
# ===========================================================================

class TestCaptureStoreGuards:
    def test_home_dir_returns_realpath(self):
        # Line 16 — call the real implementation, not the fixture mock.
        result = _REAL_HOME_DIR()
        assert os.path.isabs(result)

    def test_prepare_refuses_path_outside_home(self, tmp_path):
        with pytest.raises(OSError, match="主目录"):
            capture_store.prepare("/etc/some-capture-dir-that-does-not-exist")

    def test_prepare_refuses_wrong_owner(self, tmp_path, monkeypatch):
        d = tmp_path / "owned"
        d.mkdir()
        (d / capture_store.MARKER).write_text("")
        real_getuid = os.getuid()
        monkeypatch.setattr(os, "getuid", lambda: real_getuid + 5000)
        with pytest.raises(OSError, match="所有者"):
            capture_store.prepare(str(d))

    def test_prepare_refuses_unsafe_marker_symlink(self, tmp_path, monkeypatch):
        d = tmp_path / "marksy"
        d.mkdir()
        target = tmp_path / "marker-target"
        target.write_text("x")
        (d / capture_store.MARKER).symlink_to(target)
        monkeypatch.setattr(capture_store, "DEFAULT_CAPTURE_DIR", str(d))
        with pytest.raises(OSError, match="标记文件"):
            capture_store.prepare(str(d))

    def test_trim_store_skips_non_regular_jsonl(self, tmp_path):
        store = tmp_path / "store58"
        store.mkdir()
        (store / capture_store.MARKER).write_text("")
        (store / "2024-01-01.jsonl").mkdir()
        legit = store / "2024-01-02.jsonl"
        legit.write_text("data")
        capture_store._trim_store(str(store))
        assert (store / "2024-01-01.jsonl").exists()
        assert legit.exists()

    def test_trim_store_deletes_oldest_when_over_limit(self, tmp_path, monkeypatch):
        store = tmp_path / "bigstore"
        store.mkdir()
        (store / capture_store.MARKER).write_text("")
        old = store / "2024-01-01.jsonl"
        new = store / "2024-01-02.jsonl"
        old.write_text("A" * 200)
        new.write_text("B" * 200)
        monkeypatch.setattr(capture_store, "MAX_STORE_BYTES", 250)
        capture_store._trim_store(str(store))
        assert not old.exists()
        assert new.exists()

    def test_append_json_dir_fd_wrong_type_raises(self, tmp_path, monkeypatch):
        store = tmp_path / "evil_dir"
        store.mkdir()
        (store / capture_store.MARKER).write_text("")
        real_fstat = os.fstat

        def fake_fstat(fd):
            st = real_fstat(fd)
            fake = MagicMock()
            fake.st_mode = 0o100644  # S_IFREG, not dir
            fake.st_uid = st.st_uid
            return fake

        monkeypatch.setattr(os, "fstat", fake_fstat)
        with pytest.raises(OSError, match="所有者或类型"):
            capture_store.append_json({"x": 1}, str(store))

    def test_append_json_target_not_regular_raises(self, tmp_path, monkeypatch):
        store = tmp_path / "target_store"
        store.mkdir()
        (store / capture_store.MARKER).write_text("")
        real_stat = os.stat

        def fake_stat(name, *args, **kwargs):
            # Only intercept the dir_fd-relative stat of today's jsonl file;
            # let all other stat calls (e.g. os.path.isfile inside prepare) pass through.
            if "dir_fd" in kwargs and kwargs.get("dir_fd") is not None:
                fake = MagicMock()
                fake.st_mode = 0o120644  # S_IFLNK — not a regular file
                fake.st_uid = os.getuid()
                fake.st_size = 0
                return fake
            return real_stat(name, *args, **kwargs)

        monkeypatch.setattr(os, "stat", fake_stat)
        with pytest.raises(OSError, match="不是普通文件"):
            capture_store.append_json({"x": 1}, str(store))

    def test_append_json_fd_wrong_owner_raises(self, tmp_path, monkeypatch):
        store = tmp_path / "fd_store"
        store.mkdir()
        (store / capture_store.MARKER).write_text("")
        call_count = [0]
        real_fstat = os.fstat

        def fake_fstat(fd):
            call_count[0] += 1
            st = real_fstat(fd)
            if call_count[0] == 1:
                return st
            fake = MagicMock()
            fake.st_mode = stat.S_IFREG | 0o600
            fake.st_uid = st.st_uid + 9999
            fake.st_size = 0
            return fake

        monkeypatch.setattr(os, "fstat", fake_fstat)
        with pytest.raises(OSError, match="所有者或类型"):
            capture_store.append_json({"x": 1}, str(store))


# ===========================================================================
# capture — cleanup_expired_captures OSError handlers
# ===========================================================================

class TestCaptureOSErrorHandlers:
    def test_listdir_oserror_returns_zero(self, tmp_path, monkeypatch):
        d = tmp_path / "listdir-fail"
        d.mkdir()
        monkeypatch.setattr(os, "listdir", lambda p: (_ for _ in ()).throw(OSError("perm")))
        assert capture.cleanup_expired_captures(str(d), 7) == 0

    def test_remove_oserror_continues_and_counts_zero(self, tmp_path, monkeypatch):
        d = tmp_path / "remove-fail"
        d.mkdir()
        old_date = (datetime.now().date() - timedelta(days=30)).isoformat()
        (d / f"{old_date}.jsonl").write_text("{}")
        monkeypatch.setattr(os, "remove", lambda p: (_ for _ in ()).throw(OSError("busy")))
        deleted = capture.cleanup_expired_captures(str(d), 7)
        assert deleted == 0


# ===========================================================================
# capture — CaptureMonitor.start prepare_capture_dir OSError
# ===========================================================================

class TestCaptureStartPrepareFailure:
    def test_start_returns_false_when_prepare_raises_oserror(self, monkeypatch):
        def boom(path):
            raise OSError("unsafe dir")
        monkeypatch.setattr(capture, "prepare_capture_dir", boom)
        mon = capture.CaptureMonitor()
        try:
            result = mon.start(mitmdump_bin="mitmdump", addon_path="/x/a.py")
            assert result is False
            assert mon.status == "error"
            assert "unsafe dir" in mon.error_msg
        finally:
            mon.stop(blocking=False)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
