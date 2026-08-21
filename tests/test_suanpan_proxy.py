"""Tests for suanpan proxy + usage extractor + header handling + key resolution.

Seams under test:
- UsageExtractor: feed(chunk) → .input_tokens/.output_tokens/.cache_read_tokens/.cache_creation_tokens
- ProviderConfig.build_outbound_headers: incoming dict + key → filtered dict with auth
- filter_response_headers: httpx.Headers → dict without hop-by-hop
- ProviderConfig.resolve_api_key: → key string or None
- _send_with_retry: transport-level errors retried once; timeouts never
"""
import json
import unittest
from unittest.mock import MagicMock, AsyncMock

import httpx

from suanpan.usage_extractor import UsageExtractor
from suanpan.proxy import drain_and_log, filter_response_headers, _send_with_retry
from suanpan.config import ProviderConfig


# ── UsageExtractor ──────────────────────────────────────────────────

class TestUsageExtractorEmpty(unittest.TestCase):
    def test_no_feed_all_zeros(self):
        ext = UsageExtractor()
        self.assertEqual(ext.input_tokens, 0)
        self.assertEqual(ext.output_tokens, 0)
        self.assertEqual(ext.cache_read_tokens, 0)
        self.assertEqual(ext.cache_creation_tokens, 0)


class TestUsageExtractorMessageStart(unittest.TestCase):
    def _feed_message_start(self, usage):
        ext = UsageExtractor()
        event = f"event: message_start\ndata: {json.dumps({'type': 'message_start', 'message': {'usage': usage}})}\n\n"
        ext.feed(event.encode())
        return ext

    def test_input_tokens_extracted(self):
        ext = self._feed_message_start({"input_tokens": 1234})
        self.assertEqual(ext.input_tokens, 1234)

    def test_cache_read_tokens_extracted(self):
        ext = self._feed_message_start({"input_tokens": 100, "cache_read_input_tokens": 500})
        self.assertEqual(ext.cache_read_tokens, 500)

    def test_cache_creation_tokens_extracted(self):
        ext = self._feed_message_start({"input_tokens": 100, "cache_creation_input_tokens": 200})
        self.assertEqual(ext.cache_creation_tokens, 200)

    def test_missing_fields_keep_default_zero(self):
        ext = self._feed_message_start({"input_tokens": 50})
        self.assertEqual(ext.cache_read_tokens, 0)
        self.assertEqual(ext.cache_creation_tokens, 0)


class TestUsageExtractorMessageDelta(unittest.TestCase):
    def test_output_tokens_extracted(self):
        ext = UsageExtractor()
        event = "data: " + json.dumps({"type": "message_delta", "usage": {"output_tokens": 567}}) + "\n\n"
        ext.feed(event.encode())
        self.assertEqual(ext.output_tokens, 567)

    def test_delta_without_output_tokens_ignored(self):
        ext = UsageExtractor()
        ext.input_tokens = 100  # pre-set
        event = "data: " + json.dumps({"type": "message_delta", "usage": {"stop_reason": "end_turn"}}) + "\n\n"
        ext.feed(event.encode())
        self.assertEqual(ext.output_tokens, 0)


class TestUsageExtractorChunked(unittest.TestCase):
    def test_event_split_across_feeds(self):
        ext = UsageExtractor()
        event = (
            "data: " + json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 42}}}) + "\n\n"
        ).encode()
        mid = len(event) // 2
        ext.feed(event[:mid])
        # Not fully fed yet — should still be zeros
        self.assertEqual(ext.input_tokens, 0)
        ext.feed(event[mid:])
        self.assertEqual(ext.input_tokens, 42)

    def test_multiple_events_in_one_chunk(self):
        ext = UsageExtractor()
        start = "data: " + json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 10}}}) + "\n\n"
        delta = "data: " + json.dumps({"type": "message_delta", "usage": {"output_tokens": 5}}) + "\n\n"
        ext.feed((start + delta).encode())
        self.assertEqual(ext.input_tokens, 10)
        self.assertEqual(ext.output_tokens, 5)


class TestUsageExtractorIgnoresJunk(unittest.TestCase):
    def test_non_data_lines_ignored(self):
        ext = UsageExtractor()
        event = "event: ping\ndata: {}\n\n"
        ext.feed(event.encode())
        self.assertEqual(ext.input_tokens, 0)

    def test_invalid_json_skipped(self):
        ext = UsageExtractor()
        event = b"data: not-json-at-all\n\n"
        ext.feed(event)
        self.assertEqual(ext.input_tokens, 0)

    def test_unknown_event_type_ignored(self):
        ext = UsageExtractor()
        event = "data: " + json.dumps({"type": "content_block_start", "index": 0}) + "\n\n"
        ext.feed(event.encode())
        self.assertEqual(ext.input_tokens, 0)
        self.assertEqual(ext.output_tokens, 0)


# ── build_outbound_headers ──────────────────────────────────────────

class TestBuildOutboundHeaders(unittest.TestCase):
    def test_strips_hop_by_hop_headers(self):
        incoming = {
            "Host": "localhost:9527",
            "Content-Length": "100",
            "Connection": "keep-alive",
            "Authorization": "Bearer old",
            "X-Api-Key": "old-key",
            "Content-Type": "application/json",
            "X-Custom": "keep-me",
        }
        cfg = ProviderConfig(base_url="http://x", api_key="new-key", auth_header="x-api-key")
        result = cfg.build_outbound_headers(incoming, "new-key")
        self.assertNotIn("Host", result)
        self.assertNotIn("host", result)
        self.assertNotIn("Content-Length", result)
        self.assertNotIn("Connection", result)
        self.assertIn("Content-Type", result)
        self.assertIn("X-Custom", result)

    def test_x_api_key_auth_injected(self):
        cfg = ProviderConfig(base_url="http://x", api_key="sk-123", auth_header="x-api-key")
        result = cfg.build_outbound_headers({"Content-Type": "application/json"}, "sk-123")
        self.assertEqual(result["x-api-key"], "sk-123")

    def test_bearer_auth_injected(self):
        cfg = ProviderConfig(base_url="http://x", api_key="sk-456", auth_header="Authorization")
        result = cfg.build_outbound_headers({}, "sk-456")
        self.assertEqual(result["Authorization"], "Bearer sk-456")

    def test_no_api_key_passes_through_original_auth(self):
        """issue #9 契约反转：keyless 出站剥除一切入站凭证。"""
        cfg = ProviderConfig(base_url="http://x")
        incoming = {"Authorization": "Bearer oauth-token", "Content-Type": "application/json"}
        result = cfg.build_outbound_headers(incoming, None)
        self.assertNotIn("authorization", result)
        self.assertEqual(result["Content-Type"], "application/json")

    def test_gateway_key_not_passed_through(self):
        """The gateway's own gate key must never reach a keyless backend."""
        cfg = ProviderConfig(base_url="http://x")
        incoming = {"x-api-key": "gate-key", "Content-Type": "application/json"}
        result = cfg.build_outbound_headers(incoming, None, gateway_key="gate-key")
        self.assertNotIn("x-api-key", result)
        self.assertNotIn("X-Api-Key", result)
        self.assertEqual(result["Content-Type"], "application/json")


# ── filter_response_headers ─────────────────────────────────────────

class TestFilterResponseHeaders(unittest.TestCase):
    def test_drops_transport_headers(self):
        class FakeHeaders:
            """Minimal duck-type of httpx.Headers for testing."""
            def items(self):
                return [
                    ("content-length", "1234"),
                    ("transfer-encoding", "chunked"),
                    ("connection", "close"),
                    ("content-type", "application/json"),
                    ("x-custom", "val"),
                ]
        result = filter_response_headers(FakeHeaders())
        self.assertNotIn("content-length", result)
        self.assertNotIn("transfer-encoding", result)
        self.assertNotIn("connection", result)
        self.assertEqual(result["content-type"], "application/json")
        self.assertEqual(result["x-custom"], "val")


# ── resolve_api_key ─────────────────────────────────────────────────

class TestResolveApiKey(unittest.TestCase):
    def test_explicit_key_returned(self):
        cfg = ProviderConfig(base_url="http://x", api_key="sk-direct")
        self.assertEqual(cfg.resolve_api_key(), "sk-direct")

    def test_env_var_resolved(self):
        import os
        os.environ["TEST_SP_KEY"] = "sk-from-env"
        try:
            cfg = ProviderConfig(base_url="http://x", api_key_env="TEST_SP_KEY", auth_header="x-api-key")
            self.assertEqual(cfg.resolve_api_key(), "sk-from-env")
        finally:
            del os.environ["TEST_SP_KEY"]

    def test_neither_returns_none(self):
        cfg = ProviderConfig(base_url="http://x")
        self.assertIsNone(cfg.resolve_api_key())

    def test_explicit_key_takes_priority_over_env(self):
        import os
        os.environ["TEST_SP_KEY2"] = "sk-env"
        try:
            cfg = ProviderConfig(base_url="http://x", api_key="sk-direct", api_key_env="TEST_SP_KEY2", auth_header="x-api-key")
            self.assertEqual(cfg.resolve_api_key(), "sk-direct")
        finally:
            del os.environ["TEST_SP_KEY2"]


# ── auth header building (behavior through build_outbound_headers) ──

class TestApplyAuth(unittest.TestCase):
    def test_x_api_key_sets_header(self):
        cfg = ProviderConfig(base_url="http://x", auth_header="x-api-key")
        headers = cfg.build_outbound_headers({}, "sk-test")
        self.assertEqual(headers["x-api-key"], "sk-test")

    def test_authorization_sets_bearer(self):
        cfg = ProviderConfig(base_url="http://x", auth_header="Authorization")
        headers = cfg.build_outbound_headers({}, "sk-test")
        self.assertEqual(headers["Authorization"], "Bearer sk-test")

    def test_none_auth_header_no_change(self):
        cfg = ProviderConfig(base_url="http://x")
        headers = cfg.build_outbound_headers({"X": "y"}, None)
        self.assertEqual(headers, {"X": "y"})


# ── make_502 ───────────────────────────────────────────────────────

class TestMake502(unittest.TestCase):
    def test_returns_502_with_provider_header(self):
        import time
        from suanpan.proxy import make_502
        logger = MagicMock()
        resp = make_502("deepseek", "claude-sonnet-4", "deepseek-v4-flash",
                        "rule", "timeout", time.monotonic() - 1.0, logger)
        self.assertEqual(resp.status_code, 502)
        self.assertEqual(resp.headers["x-suanpan-provider"], "deepseek")

    def test_response_body_contains_error(self):
        import time
        from suanpan.proxy import make_502
        logger = MagicMock()
        resp = make_502("p1", "m1", "m2", "default", "conn refused",
                        time.monotonic(), logger)
        import json
        body = json.loads(resp.body)
        self.assertEqual(body["error"], "backend request failed")
        self.assertEqual(body["provider"], "p1")
        self.assertEqual(body["last_error"], "conn refused")

    def test_logs_usage_entry(self):
        import time
        from suanpan.proxy import make_502
        logger = MagicMock()
        started = time.monotonic()
        make_502("p1", "m1", "m2", "default", "err", started, logger)
        logger.write.assert_called_once()
        entry = logger.write.call_args[0][0]
        self.assertEqual(entry.provider, "p1")
        self.assertEqual(entry.status, 502)
        self.assertEqual(entry.error, "err")
        self.assertEqual(entry.input_tokens, 0)
        self.assertEqual(entry.output_tokens, 0)


# ── transport retry ─────────────────────────────────────────────────

class TestRetryPolicy(unittest.IsolatedAsyncioTestCase):
    """issue #7：RetryPolicy——无法证明请求未送达时，非幂等请求绝不重放。"""

    def _counting_send(self, error, fail_times=1):
        calls = []
        async def send(req, stream=False):
            calls.append(req)
            if len(calls) <= fail_times:
                raise error
            return httpx.Response(200, json={"ok": True}, request=req)
        return send, calls

    async def _sends(self, method, error):
        req = httpx.Request(method, "https://api.example.com/v1/messages")
        client = MagicMock(spec=httpx.AsyncClient)
        send, calls = self._counting_send(error)
        client.send = send
        try:
            await _send_with_retry(client, req)
        except Exception:
            pass
        return len(calls)

    async def test_post_connect_error_retried(self):
        self.assertEqual(await self._sends("POST", httpx.ConnectError("refused")), 2)

    async def test_post_read_error_never_retried(self):
        self.assertEqual(
            await self._sends("POST", httpx.ReadError("reset mid-response")), 1,
            "ReadError 可能发生在上游已处理之后——POST 不得重放")

    async def test_post_remote_protocol_error_never_retried(self):
        self.assertEqual(
            await self._sends("POST", httpx.RemoteProtocolError("terminated")), 1)

    async def test_post_write_error_never_retried(self):
        self.assertEqual(
            await self._sends("POST", httpx.WriteError("partial send")), 1)

    async def test_post_uncertain_timeout_never_retried(self):
        self.assertEqual(
            await self._sends("POST", httpx.ReadTimeout("upstream slow")), 1)
        self.assertEqual(
            await self._sends("POST", httpx.WriteTimeout("send stall")), 1)

    async def test_post_connect_timeout_retried(self):
        self.assertEqual(
            await self._sends("POST", httpx.ConnectTimeout("connect slow")), 2)

    async def test_get_transport_errors_bounded_retry(self):
        self.assertEqual(
            await self._sends("GET", httpx.ReadError("reset")), 2,
            "GET 幂等——传输错误有界重试一次")
        self.assertEqual(
            await self._sends("GET", httpx.RemoteProtocolError("terminated")), 2)

    async def test_get_ambiguous_timeout_still_not_retried(self):
        self.assertEqual(await self._sends("GET", httpx.ReadTimeout("slow")), 1)

    async def test_idempotent_post_with_key_retries(self):
        req = httpx.Request("POST", "https://api.example.com/v1/messages")
        client = MagicMock(spec=httpx.AsyncClient)
        send, calls = self._counting_send(httpx.ReadError("reset"))
        client.send = send
        resp = await _send_with_retry(client, req, idempotent=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(calls), 2, "显式幂等键的 POST 按幂等策略重试")

    async def test_attempt_and_reason_logged(self):
        from unittest.mock import patch as _patch
        req = httpx.Request("GET", "https://api.example.com/m")
        client = MagicMock(spec=httpx.AsyncClient)
        send, calls = self._counting_send(httpx.ConnectError("refused"))
        client.send = send
        with _patch("suanpan.proxy._log") as log:
            await _send_with_retry(client, req)
        # structlog 风格：事件名为首个位置参数
        retry_events = [c for c in log.warning.call_args_list
                        if c.args and c.args[0] == "transport_retry"]
        self.assertTrue(retry_events)
        self.assertIn(retry_events[0].kwargs.get("reason"),
                      ("pre-send-proven", "idempotent-transport"))
        self.assertEqual(retry_events[0].kwargs.get("attempt"), 1)

    async def test_second_failure_propagates_no_loop(self):
        req = httpx.Request("GET", "https://api.example.com/m")
        client = MagicMock(spec=httpx.AsyncClient)
        send, calls = self._counting_send(httpx.ReadError("reset"),
                                          fail_times=99)
        client.send = send
        with self.assertRaises(httpx.ReadError):
            await _send_with_retry(client, req)
        self.assertEqual(len(calls), 2, "至多一次重试，不循环")

    async def test_proxy_error_retried(self):
        self.assertEqual(
            await self._sends("POST", httpx.ProxyError("proxy refused")), 2)

    async def test_unsupported_protocol_retried(self):
        self.assertEqual(
            await self._sends("POST", httpx.UnsupportedProtocol("h2c")), 2)

    async def test_pool_timeout_retried(self):
        # 等连接池超时 = 发送前
        self.assertEqual(
            await self._sends("POST", httpx.PoolTimeout("pool wait")), 2)



def _mock_sse_response(chunks: list[bytes], status_code: int = 200):
    """Build a mock httpx.Response whose aiter_raw yields real SSE bytes."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.headers = {"content-type": "text/event-stream"}
    resp.aclose = MagicMock()

    class _RawIter:
        def __init__(self):
            self._i = iter(chunks)

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._i)
            except StopIteration:
                raise StopAsyncIteration

    resp.aiter_raw = lambda: _RawIter()
    resp.aclose = AsyncMock()
    return resp



class TestDrainAndLog(unittest.IsolatedAsyncioTestCase):
    """The SSE→extractor→usage chain, tested with real provider byte arrays."""

    async def _consume(self, gen):
        """Drain an async generator, returning collected bytes."""
        collected = []
        async for chunk in gen:
            collected.append(chunk)
        return b"".join(collected)

    async def test_anthropic_sse_parsed_correctly(self):
        """Standard Anthropic format: 'data: ' (with space)."""
        sse = [
            b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":100,"cache_creation_input_tokens":50}}}\n\n',
            b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":200}}\n\n',
        ]
        resp = _mock_sse_response(sse)
        extractor = UsageExtractor()
        logger = MagicMock()

        gen = drain_and_log(resp, extractor, logger, provider="anthropic",
                            source_model="claude-sonnet-5", target_model="claude-sonnet-5",
                            scenario="default", started=0.0)
        await self._consume(gen)

        self.assertEqual(extractor.input_tokens, 100)
        self.assertEqual(extractor.output_tokens, 200)
        self.assertEqual(extractor.cache_creation_tokens, 50)
        logger.write.assert_called_once()
        entry = logger.write.call_args[0][0]
        self.assertEqual(entry.input_tokens, 100)
        self.assertEqual(entry.output_tokens, 200)
        self.assertEqual(entry.status, 200)

    async def test_kimi_sse_no_space_after_data(self):
        """KIMI format: 'data:' (no space) — the bug that was fixed reactively."""
        sse = [
            b'event: message_start\ndata:{"type":"message_start","message":{"usage":{"input_tokens":80}}}\n\n',
            b'event: message_delta\ndata:{"type":"message_delta","usage":{"output_tokens":150}}\n\n',
        ]
        resp = _mock_sse_response(sse)
        extractor = UsageExtractor()
        logger = MagicMock()

        gen = drain_and_log(resp, extractor, logger, provider="kimi",
                            source_model="claude-sonnet-5", target_model="kimi-k3",
                            scenario="rule", started=0.0)
        await self._consume(gen)

        self.assertEqual(extractor.input_tokens, 80)
        self.assertEqual(extractor.output_tokens, 150)

    async def test_chunks_split_across_sse_boundary(self):
        """A single SSE event split across two raw chunks — buffer must reassemble."""
        sse = [
            b'event: message_start\ndata: {"type":"message_start","mes',
            b'sage":{"usage":{"input_tokens":42}}}\n\n',
        ]
        resp = _mock_sse_response(sse)
        extractor = UsageExtractor()
        logger = MagicMock()

        gen = drain_and_log(resp, extractor, logger, provider="deepseek",
                            source_model="claude-sonnet-5", target_model="deepseek-v4",
                            scenario="default", started=0.0)
        await self._consume(gen)

        self.assertEqual(extractor.input_tokens, 42)

    async def test_usage_logged_even_on_consumer_disconnect(self):
        """If the caller stops iterating early, the finally block still logs."""
        sse = [
            b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":99}}}\n\n',
            b'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":1}}\n\n',
        ]
        resp = _mock_sse_response(sse)
        extractor = UsageExtractor()
        logger = MagicMock()

        gen = drain_and_log(resp, extractor, logger, provider="glm",
                            source_model="claude-sonnet-5", target_model="glm-5.2",
                            scenario="default", started=0.0)
        # Consume only the first chunk, then close the generator
        async for chunk in gen:
            break  # consumer disconnects after first chunk
        await gen.aclose()  # triggers GeneratorExit → finally block

        logger.write.assert_called_once()
        resp.aclose.assert_called_once()

    async def test_4xx_response_still_logged(self):
        """4xx responses are streamed through — usage logged with error status."""
        sse = [
            b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"rate limited"}}\n\n',
        ]
        resp = _mock_sse_response(sse, status_code=429)
        extractor = UsageExtractor()
        logger = MagicMock()

        gen = drain_and_log(resp, extractor, logger, provider="kimi",
                            source_model="claude-sonnet-5", target_model="kimi-k3",
                            scenario="default", started=0.0)
        await self._consume(gen)

        logger.write.assert_called_once()
        entry = logger.write.call_args[0][0]
        self.assertEqual(entry.status, 429)

    async def test_midstream_upstream_failure_recorded_as_error(self):
        """An exception from aiter_raw must not be logged as a clean 200."""
        resp = _mock_sse_response([b"partial"])

        class _Boom:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise httpx.RemoteProtocolError("connection reset")

        resp.aiter_raw = lambda: _Boom()
        extractor = UsageExtractor()
        logger = MagicMock()

        gen = drain_and_log(resp, extractor, logger, provider="kimi",
                            source_model="claude-sonnet-5", target_model="kimi-k3",
                            scenario="default", started=0.0)
        with self.assertRaises(httpx.RemoteProtocolError):
            await self._consume(gen)
        logger.write.assert_called_once()
        entry = logger.write.call_args[0][0]
        self.assertIsNotNone(entry.error)
        self.assertIn("RemoteProtocolError", entry.error)

    async def test_logger_write_failure_does_not_truncate_stream(self):
        """A failing usage logger must never interrupt the client stream."""
        sse = [
            b'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":7}}}\n\n',
        ]
        resp = _mock_sse_response(sse)
        extractor = UsageExtractor()
        logger = MagicMock()
        logger.write.side_effect = OSError("disk full")

        gen = drain_and_log(resp, extractor, logger, provider="kimi",
                            source_model="claude-sonnet-5", target_model="kimi-k3",
                            scenario="default", started=0.0)
        collected = await self._consume(gen)  # must not raise
        self.assertTrue(collected)




class TestCountingUpstreamIntegration(unittest.IsolatedAsyncioTestCase):
    """issue #7 验收：计数 upstream 证明失败响应不触发第二次 POST。"""

    async def test_failing_post_reaches_counting_upstream_once(self):
        posts = []

        def handler(request: httpx.Request) -> httpx.Response:
            posts.append(request)
            raise httpx.ReadError("reset after upstream processed")

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            req = client.build_request(
                "POST", "https://api.example.com/v1/messages",
                json={"m": 1}, headers={"content-type": "application/json"})
            with self.assertRaises(httpx.ReadError):
                await _send_with_retry(client, req)
        self.assertEqual(len(posts), 1,
                         "送达后不明的失败绝不重放 POST（重复推理/计费）")

    async def test_pre_send_failure_reaches_upstream_twice(self):
        posts = []

        def handler(request: httpx.Request) -> httpx.Response:
            posts.append(request)
            if len(posts) == 1:
                raise httpx.ConnectError("refused")
            return httpx.Response(200, json={"ok": True})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport) as client:
            req = client.build_request(
                "POST", "https://api.example.com/v1/messages",
                json={"m": 1}, headers={"content-type": "application/json"})
            resp = await _send_with_retry(client, req)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(posts), 2, "连接建立失败（证明未送达）安全重试")


class TestRetryBoundEdge(unittest.IsolatedAsyncioTestCase):
    """pre-send 证明也受有界约束——连接持续失败不得无限重试。"""

    async def test_persistent_connect_error_stops_after_bound(self):
        calls = []
        async def send(req, stream=False):
            calls.append(req)
            raise httpx.ConnectError("refused forever")
        client = MagicMock(spec=httpx.AsyncClient)
        client.send = send
        req = httpx.Request("POST", "https://api.example.com/v1/messages")
        with self.assertRaises(httpx.ConnectError):
            await _send_with_retry(client, req)
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
