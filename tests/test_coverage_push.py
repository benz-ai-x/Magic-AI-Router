"""Coverage push: suanpan/proxy forward + config_server write + suanpan/main handlers + host_key file ops."""
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch, AsyncMock
from pathlib import Path

# ── suanpan/proxy.py: forward_request + forward_count_tokens ──────

from suanpan.config import AppConfig, ProviderConfig, RouterConfig
from suanpan.router import RouteDecision
from suanpan import proxy as spproxy


def _cfg():
    return AppConfig(
        providers={"p1": ProviderConfig(
            base_url="https://api.test.com", api_key="sk-test",
            auth_header="x-api-key", enabled=True, models=["m1"])},
        router=RouterConfig(default="p1/m1"),
    )


def _decision(**kw):
    defaults = dict(provider="p1", target_model="m1", scenario="default", strip_marker=False)
    defaults.update(kw)
    return RouteDecision(**defaults)


def _async_iter(chunks):
    async def _gen():
        for c in chunks:
            yield c
    return _gen()


class TestForwardRequest(unittest.IsolatedAsyncioTestCase):
    async def test_success_streams_response(self):
        mock_request = MagicMock()
        mock_request.headers = {"host": "127.0.0.1"}
        body = {"model": "test", "max_tokens": 16, "messages": [{"role": "user", "content": "hi"}]}
        config = _cfg()
        logger = MagicMock()
        client = MagicMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.aiter_raw = AsyncMock()
        mock_resp.aiter_raw.return_value.__aiter__.return_value = [b"chunk1", b"chunk2"]
        mock_resp.aclose = AsyncMock()

        client.build_request.return_value = MagicMock()
        client.send = AsyncMock(return_value=mock_resp)

        result = await spproxy.forward_request(
            mock_request, body, _decision(), config, logger, client)
        self.assertEqual(result.status_code, 200)

    async def test_upstream_error_returns_502(self):
        mock_request = MagicMock()
        mock_request.headers = {}
        body = {"model": "test", "max_tokens": 16, "messages": []}
        config = _cfg()
        logger = MagicMock()
        client = MagicMock()
        client.build_request.return_value = MagicMock()
        client.send = AsyncMock(side_effect=__import__("httpx").ConnectError("fail"))

        result = await spproxy.forward_request(
            mock_request, body, _decision(), config, logger, client)
        self.assertEqual(result.status_code, 502)

    async def test_upstream_5xx_returns_502(self):
        mock_request = MagicMock()
        mock_request.headers = {}
        body = {"model": "test", "max_tokens": 16, "messages": []}
        config = _cfg()
        logger = MagicMock()
        client = MagicMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.aclose = AsyncMock()
        client.build_request.return_value = MagicMock()
        client.send = AsyncMock(return_value=mock_resp)

        result = await spproxy.forward_request(
            mock_request, body, _decision(), config, logger, client)
        self.assertEqual(result.status_code, 502)

    async def test_strip_marker_applied(self):
        mock_request = MagicMock()
        mock_request.headers = {}
        body = {"model": "test", "max_tokens": 16, "messages": [],
                "system": "text <SUBAGENT-MODEL>p1/m1</SUBAGENT-MODEL>"}
        config = _cfg()
        logger = MagicMock()
        client = MagicMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.aiter_raw = AsyncMock()
        mock_resp.aiter_raw.return_value.__aiter__.return_value = []
        mock_resp.aclose = AsyncMock()
        client.build_request.return_value = MagicMock()
        client.send = AsyncMock(return_value=mock_resp)

        await spproxy.forward_request(
            mock_request, body, _decision(strip_marker=True), config, logger, client)
        # marker should be stripped
        self.assertNotIn("SUBAGENT-MODEL", body.get("system", ""))

    async def test_gateway_key_not_forwarded_to_keyless_provider(self):
        """issue #9：keyless Provider 出站无条件剥除一切入站凭证——
        网关 gate key 也不得上游。"""
        mock_request = MagicMock()
        mock_request.headers = {"x-api-key": "gate-key"}
        body = {"model": "test", "max_tokens": 16, "messages": []}
        config = AppConfig(
            api_key="gate-key",
            providers={"p1": ProviderConfig(base_url="https://api.test.com")},
            router=RouterConfig(default="p1/m1"),
        )
        logger = MagicMock()
        client = MagicMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.aiter_raw = AsyncMock()
        mock_resp.aiter_raw.return_value.__aiter__.return_value = []
        mock_resp.aclose = AsyncMock()
        client.build_request.return_value = MagicMock()
        client.send = AsyncMock(return_value=mock_resp)

        await spproxy.forward_request(
            mock_request, body, _decision(), config, logger, client)

        sent_headers = client.build_request.call_args.kwargs["headers"]
        self.assertNotIn("x-api-key", sent_headers)
        self.assertNotIn("X-Api-Key", sent_headers)
        self.assertNotIn("gate-key", [str(v) for v in sent_headers.values()])

    async def test_anthropic_native_provider_keeps_system_array(self):
        """#5: a provider flagged anthropic_native must receive the system
        array (with cache_control) intact — the whole point of the flag."""
        mock_request = MagicMock()
        mock_request.headers = {}
        body = {"model": "test", "max_tokens": 16,
                "system": [{"type": "text", "text": "sys",
                            "cache_control": {"type": "ephemeral"}}],
                "messages": []}
        system = body["system"]
        config = AppConfig(
            providers={"p1": ProviderConfig(
                base_url="https://api.test.com", api_key="sk-test",
                anthropic_native=True)},
            router=RouterConfig(default="p1/m1"),
        )
        logger = MagicMock()
        client = MagicMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.aiter_raw = AsyncMock()
        mock_resp.aiter_raw.return_value.__aiter__.return_value = []
        mock_resp.aclose = AsyncMock()
        client.build_request.return_value = MagicMock()
        client.send = AsyncMock(return_value=mock_resp)

        await spproxy.forward_request(
            mock_request, body, _decision(), config, logger, client)
        sent_body = client.build_request.call_args.kwargs["json"]
        self.assertEqual(sent_body["system"], system)

    async def test_default_provider_system_flattened(self):
        mock_request = MagicMock()
        mock_request.headers = {}
        body = {"model": "test", "max_tokens": 16,
                "system": [{"type": "text", "text": "sys"}],
                "messages": []}
        config = _cfg()
        logger = MagicMock()
        client = MagicMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.aiter_raw = AsyncMock()
        mock_resp.aiter_raw.return_value.__aiter__.return_value = []
        mock_resp.aclose = AsyncMock()
        client.build_request.return_value = MagicMock()
        client.send = AsyncMock(return_value=mock_resp)

        await spproxy.forward_request(
            mock_request, body, _decision(), config, logger, client)
        sent_body = client.build_request.call_args.kwargs["json"]
        self.assertEqual(sent_body["system"], "sys")

    async def test_system_array_flattened_before_forwarding(self):
        """forward_request must call normalize_body — system array flattened to string."""
        mock_request = MagicMock()
        mock_request.headers = {}
        body = {"model": "test", "max_tokens": 16, "messages": [],
                "system": [{"type": "text", "text": "be helpful",
                            "cache_control": {"type": "ephemeral"}}]}
        config = _cfg()
        logger = MagicMock()
        client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.aiter_raw = AsyncMock()
        mock_resp.aiter_raw.return_value.__aiter__.return_value = []
        mock_resp.aclose = AsyncMock()
        client.build_request.return_value = MagicMock()
        client.send = AsyncMock(return_value=mock_resp)

        await spproxy.forward_request(
            mock_request, body, _decision(), config, logger, client)
        self.assertIsInstance(body["system"], str)
        self.assertEqual(body["system"], "be helpful")

    async def test_document_blocks_stripped_before_forwarding(self):
        """forward_request must call normalize_body — document blocks removed."""
        mock_request = MagicMock()
        mock_request.headers = {}
        body = {"model": "test", "max_tokens": 16, "messages": [
            {"role": "user", "content": [
                {"type": "document", "source": {"type": "base64"}},
                {"type": "text", "text": "describe this"},
            ]}]}
        config = _cfg()
        logger = MagicMock()
        client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.aiter_raw = AsyncMock()
        mock_resp.aiter_raw.return_value.__aiter__.return_value = []
        mock_resp.aclose = AsyncMock()
        client.build_request.return_value = MagicMock()
        client.send = AsyncMock(return_value=mock_resp)

        await spproxy.forward_request(
            mock_request, body, _decision(), config, logger, client)
        content = body["messages"][0]["content"]
        types = [b["type"] for b in content]
        self.assertNotIn("document", types)


    async def test_nonstream_json_response_usage_logged(self):
        """Non-streaming upstream responses carry usage in the JSON body,
        not SSE events — the extractor must be switched to json_mode via
        the response content-type or the row logs zero (#DeepSeek 8/15)."""
        import json as _json
        mock_request = MagicMock()
        mock_request.headers = {}
        body = {"model": "test", "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}], "stream": False}
        config = _cfg()
        logger = MagicMock()
        client = MagicMock()

        payload = _json.dumps({"usage": {
            "input_tokens": 85, "output_tokens": 29,
            "cache_read_input_tokens": 3328}}).encode()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.aiter_raw = MagicMock(
            return_value=_async_iter([payload[:10], payload[10:]]))
        mock_resp.aclose = AsyncMock()
        client.build_request.return_value = MagicMock()
        client.send = AsyncMock(return_value=mock_resp)

        result = await spproxy.forward_request(
            mock_request, body, _decision(), config, logger, client)
        # Drain the StreamingResponse generator so the finally-block logs
        async for _ in result.body_iterator:
            pass

        entry = logger.write.call_args.args[0]
        self.assertEqual(entry.input_tokens, 85)
        self.assertEqual(entry.output_tokens, 29)
        self.assertEqual(entry.cache_read_tokens, 3328)

    async def test_stream_response_still_uses_sse_mode(self):
        """Regression: text/event-stream responses keep the SSE scanner."""
        mock_request = MagicMock()
        mock_request.headers = {}
        body = {"model": "test", "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}], "stream": True}
        config = _cfg()
        logger = MagicMock()
        client = MagicMock()

        sse = (b'data: {"type":"message_start","message":{"usage":'
               b'{"input_tokens":7,"output_tokens":1}}}\n\n'
               b'data: {"type":"message_delta","usage":{"output_tokens":16}}\n\n')
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/event-stream"}
        mock_resp.aiter_raw = MagicMock(return_value=_async_iter([sse]))
        mock_resp.aclose = AsyncMock()
        client.build_request.return_value = MagicMock()
        client.send = AsyncMock(return_value=mock_resp)

        result = await spproxy.forward_request(
            mock_request, body, _decision(), config, logger, client)
        async for _ in result.body_iterator:
            pass

        entry = logger.write.call_args.args[0]
        self.assertEqual(entry.input_tokens, 7)
        self.assertEqual(entry.output_tokens, 16)


class TestForwardCountTokens(unittest.IsolatedAsyncioTestCase):
    async def test_success_returns_json(self):
        mock_request = MagicMock()
        mock_request.headers = {}
        body = {"model": "test", "messages": [{"role": "user", "content": "hi"}]}
        config = _cfg()
        client = MagicMock()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {"input_tokens": 5}
        mock_resp.aread = AsyncMock()
        client.build_request.return_value = MagicMock()
        client.send = AsyncMock(return_value=mock_resp)

        result = await spproxy.forward_count_tokens(
            mock_request, body, _decision(), config, client)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.body, b'{"input_tokens":5}')  # #44 验收：body 透传

    async def test_real_streamed_json_response_is_forwarded(self):
        """#44：生产路径经 _send_with_retry(stream=True) 拿到的是未读流式
        响应——MagicMock 双打不出 ResponseNotRead，必须用真 httpx 客户端
        （MockTransport）走完整 build→send→读链。"""
        import httpx

        async def upstream(request):
            async def gen():
                yield b'{"input_tokens":5}'
            return httpx.Response(200, content=gen(),
                                  headers={"content-type": "application/json"})

        real_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        mock_request = MagicMock()
        mock_request.headers = {}
        body = {"model": "test", "messages": [{"role": "user", "content": "hi"}]}
        try:
            result = await spproxy.forward_count_tokens(
                mock_request, body, _decision(), _cfg(), real_client)
        finally:
            await real_client.aclose()
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.body, b'{"input_tokens":5}')

    async def test_mid_read_error_returns_502_not_500(self):
        """#44 复核（c1）：读体中途的传输错误（ReadError 是 HTTPError 子类）
        必须走 502 塑形，不得逃出 except 链成裸 500。"""
        import httpx

        async def upstream(request):
            async def gen():
                yield b'{"partial'
                raise httpx.ReadError("mid-read boom")
            return httpx.Response(200, content=gen(),
                                  headers={"content-type": "application/json"})

        real_client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        mock_request = MagicMock()
        mock_request.headers = {}
        body = {"model": "test", "messages": []}
        try:
            result = await spproxy.forward_count_tokens(
                mock_request, body, _decision(), _cfg(), real_client)
        finally:
            await real_client.aclose()
        self.assertEqual(result.status_code, 502)
        self.assertIn(b"backend request failed", result.body)

    async def test_upstream_error_returns_502(self):
        mock_request = MagicMock()
        mock_request.headers = {}
        body = {"model": "test", "messages": []}
        config = _cfg()
        client = MagicMock()
        client.build_request.return_value = MagicMock()
        client.send = AsyncMock(side_effect=__import__("httpx").ConnectError("fail"))

        result = await spproxy.forward_count_tokens(
            mock_request, body, _decision(), config, client)
        self.assertEqual(result.status_code, 502)

    async def test_body_normalized_before_forward(self):
        """count_tokens must run the same compat normalization as /v1/messages
        — a content-block system or document blocks would 400 upstream."""
        mock_request = MagicMock()
        mock_request.headers = {}
        body = {
            "model": "test",
            "system": [{"type": "text", "text": "hi",
                        "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user",
                          "content": [{"type": "document", "source": {}}]}],
        }
        config = _cfg()
        client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {"input_tokens": 5}
        mock_resp.aread = AsyncMock()
        client.build_request.return_value = MagicMock()
        client.send = AsyncMock(return_value=mock_resp)

        result = await spproxy.forward_count_tokens(
            mock_request, body, _decision(), config, client)
        self.assertEqual(result.status_code, 200)
        sent = client.build_request.call_args[1]["json"]
        self.assertEqual(sent["system"], "hi")
        self.assertNotIn("document",
                         [b.get("type") for b in sent["messages"][0]["content"]])

    async def test_subagent_marker_stripped(self):
        mock_request = MagicMock()
        mock_request.headers = {}
        body = {"model": "test",
                "system": "prologue <SUBAGENT-MODEL>KIMI/k3</SUBAGENT-MODEL>",
                "messages": []}
        config = _cfg()
        client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {"input_tokens": 5}
        mock_resp.aread = AsyncMock()
        client.build_request.return_value = MagicMock()
        client.send = AsyncMock(return_value=mock_resp)

        result = await spproxy.forward_count_tokens(
            mock_request, body, _decision(strip_marker=True), config, client)
        self.assertEqual(result.status_code, 200)
        sent = client.build_request.call_args[1]["json"]
        self.assertNotIn("SUBAGENT-MODEL", sent["system"])


# ── config_server.py / config_store.py: _write_mp + sp save ────────

# ── suanpan/main.py: BodyLimitMiddleware + handlers ───────────────

class TestBodyLimitMiddleware(unittest.TestCase):
    def test_rejects_oversize_content_length(self):
        from starlette.testclient import TestClient
        from suanpan.config import AppConfig, ProviderConfig, RouterConfig
        config = AppConfig(
            body_limit_mb=1,
            providers={"p": ProviderConfig(
                base_url="https://x.com", api_key="k",
                auth_header="x-api-key", enabled=True, models=["m"])},
            router=RouterConfig(default="p/m"),
        )
        app = spproxy.create_app if hasattr(spproxy, "create_app") else None
        from suanpan.main import create_app
        app = create_app(config)
        with TestClient(app) as client:
            # 2MB body with 1MB limit
            big = "x" * (2 * 1024 * 1024)
            r = client.post("/v1/messages",
                            content=big,
                            headers={"Content-Type": "application/json",
                                     "Content-Length": str(len(big))})
            self.assertEqual(r.status_code, 413)


class TestMainHandlers(unittest.TestCase):
    def test_messages_handler_forwards(self):
        from starlette.testclient import TestClient
        from suanpan.main import create_app
        config = _cfg()
        app = create_app(config)
        with patch("suanpan.main.forward_request", new=AsyncMock(
                return_value=__import__("fastapi").responses.JSONResponse(
                    content={"ok": True}, status_code=200))):
            with TestClient(app) as client:
                r = client.post("/v1/messages",
                                json={"model": "p1/m1", "max_tokens": 16,
                                      "messages": [{"role": "user", "content": "hi"}]})
                self.assertEqual(r.status_code, 200)

    def test_count_tokens_handler_forwards(self):
        from starlette.testclient import TestClient
        from suanpan.main import create_app
        config = _cfg()
        app = create_app(config)
        with patch("suanpan.main.forward_count_tokens", new=AsyncMock(
                return_value=__import__("fastapi").responses.JSONResponse(
                    content={"input_tokens": 5}, status_code=200))):
            with TestClient(app) as client:
                r = client.post("/v1/messages/count_tokens",
                                json={"model": "p1/m1",
                                      "messages": [{"role": "user", "content": "hi"}]})
                self.assertEqual(r.status_code, 200)


# ── host_key.py: accept + replace with real files ─────────────────

from tunnel import host_key
class TestHostKeyAccept(unittest.TestCase):
    def test_accept_empty_returns_false(self):
        self.assertFalse(host_key.accept(""))


class TestHostKeyReplace(unittest.TestCase):
    def test_replace_empty_host_returns_false(self):
        self.assertFalse(host_key.replace({"ssh_host": ""}, "keys"))


# ── suanpan_runtime.py: start error paths ──────────────────────────

from services.suanpan_runtime import SuanpanRuntime


class TestSuanpanRuntimeImport(unittest.TestCase):
    def test_missing_deps_returns_false(self):
        rt = SuanpanRuntime()
        with patch("builtins.__import__", side_effect=ImportError("fastapi")):
            result = rt.start()
        self.assertFalse(result)
        self.assertIn("依赖", rt.error)

    def test_ensure_config_creates_file(self):
        with tempfile.TemporaryDirectory() as d:
            rt = SuanpanRuntime()
            rt._config_path = str(Path(d) / "sp.yaml")
            rt._ensure_config()
            self.assertTrue(os.path.exists(rt._config_path))

    def test_reload_when_not_running(self):
        rt = SuanpanRuntime()
        self.assertTrue(rt.reload())  # no-op when not running

    def test_config_path_property(self):
        rt = SuanpanRuntime()
        self.assertIsInstance(rt.config_path, str)
