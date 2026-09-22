"""Tests for suanpan/main.py — middleware + routes via TestClient."""
import unittest
from unittest.mock import patch, AsyncMock, MagicMock

from suanpan.config import AppConfig, ProviderConfig, RouterConfig
from suanpan.main import create_app


def _config(api_key=None):
    return AppConfig(
        listen="127.0.0.1:9527",
        api_key=api_key,
        providers={
            "test": ProviderConfig(
                base_url="https://api.test.com",
                api_key="sk-test",
                auth_header="x-api-key",
                enabled=True,
                models=["test-model"],
            )
        },
        router=RouterConfig(default="test/test-model"),
    )


class TestAPIKeyMiddleware(unittest.TestCase):
    def test_no_key_required_when_not_configured(self):
        from starlette.testclient import TestClient
        app = create_app(_config(api_key=None))
        with TestClient(app) as client:
            r = client.get("/health")
            self.assertEqual(r.status_code, 200)

    def test_key_required_when_configured(self):
        from starlette.testclient import TestClient
        app = create_app(_config(api_key="secret"))
        with TestClient(app) as client:
            # No key → 401
            r = client.get("/health")
            self.assertEqual(r.status_code, 200)  # health is public
            # With wrong key to /v1/models → 401
            r = client.get("/v1/models")
            self.assertEqual(r.status_code, 401)



    def test_non_ascii_credentials_401_not_500(self):
        """#69 R7：非 ASCII 凭证（真实栈以 latin-1 解码进 request.
        headers）compare_digest 不接 str——编码 bytes 后 401，不裸抛
        TypeError 500。直接 dispatch（TestClient 的 h11 会先在入站拒
        非 ASCII，够不到被测路径）。"""
        import asyncio
        from unittest.mock import MagicMock
        from suanpan.middleware import APIKeyMiddleware

        mw = APIKeyMiddleware(MagicMock(), api_key="gate-key")
        request = MagicMock()
        request.headers = {"x-api-key": "sk-tést"}  # latin-1 解码形态
        request.url.path = "/v1/messages"

        async def call_next(_):
            return MagicMock()

        resp = asyncio.run(mw.dispatch(request, call_next))
        self.assertEqual(resp.status_code, 401)


class TestRoutes(unittest.TestCase):
    def test_health(self):
        from starlette.testclient import TestClient
        app = create_app(_config())
        with TestClient(app) as client:
            r = client.get("/health")
            self.assertEqual(r.json()["status"], "ok")

    def test_models_list(self):
        from starlette.testclient import TestClient
        app = create_app(_config())
        with TestClient(app) as client:
            r = client.get("/v1/models")
            self.assertEqual(r.status_code, 200)
            data = r.json()
            self.assertIn("data", data)
            self.assertEqual(len(data["data"]), 1)

    def test_messages_invalid_json(self):
        from starlette.testclient import TestClient
        app = create_app(_config())
        with TestClient(app) as client:
            r = client.post("/v1/messages",
                            data="not json",
                            headers={"Content-Type": "application/json"})
            self.assertEqual(r.status_code, 400)


def _config_no_route(api_key=None):
    """Config with no default route and no rules -> decide_route raises."""
    return AppConfig(
        listen="127.0.0.1:9527",
        api_key=api_key,
        providers={
            "test": ProviderConfig(
                base_url="https://api.test.com",
                api_key="sk-test",
                auth_header="x-api-key",
                enabled=True,
                models=["test-model"],
            )
        },
        router=RouterConfig(default=None),
        rules=[],
    )


class TestAPIKeyMiddlewareBearer(unittest.TestCase):
    def test_bearer_token_accepted(self):
        from starlette.testclient import TestClient
        app = create_app(_config(api_key="secret"))
        with TestClient(app) as client:
            r = client.get("/v1/models",
                           headers={"Authorization": "Bearer secret"})
            self.assertEqual(r.status_code, 200)

    def test_x_api_key_header_accepted(self):
        from starlette.testclient import TestClient
        app = create_app(_config(api_key="secret"))
        with TestClient(app) as client:
            r = client.get("/v1/models", headers={"x-api-key": "secret"})
            self.assertEqual(r.status_code, 200)


class TestMessagesRouting(unittest.TestCase):
    def test_no_route_matched_returns_400(self):
        from starlette.testclient import TestClient
        app = create_app(_config_no_route())
        with TestClient(app) as client:
            r = client.post("/v1/messages",
                            json={"model": "unknown-model"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("no route matched", r.json()["error"])

    def test_count_tokens_invalid_json(self):
        from starlette.testclient import TestClient
        app = create_app(_config())
        with TestClient(app) as client:
            r = client.post("/v1/messages/count_tokens",
                            data="not json",
                            headers={"Content-Type": "application/json"})
            self.assertEqual(r.status_code, 400)

    def test_count_tokens_no_route_matched(self):
        from starlette.testclient import TestClient
        app = create_app(_config_no_route())
        with TestClient(app) as client:
            r = client.post("/v1/messages/count_tokens",
                            json={"model": "unknown-model"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("no route matched", r.json()["error"])


class TestRunFromConfigPath(unittest.TestCase):
    def test_run_from_config_path_launches_uvicorn(self):
        import suanpan.main as main_mod
        cfg = _config()
        with patch.object(main_mod, "load_config", return_value=cfg), \
             patch("uvicorn.run") as mock_run:
            main_mod.run_from_config_path("./sp.yaml")
        mock_run.assert_called_once()
        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs["host"], "127.0.0.1")
        self.assertEqual(kwargs["port"], 9527)


class TestModelsDisabledProvider(unittest.TestCase):
    def test_disabled_provider_models_skipped(self):
        from starlette.testclient import TestClient
        cfg = AppConfig(
            listen="127.0.0.1:9527",
            providers={
                "on": ProviderConfig(base_url="https://a", api_key="k",
                                     auth_header="x-api-key", enabled=True,
                                     models=["m1"]),
                "off": ProviderConfig(base_url="https://b", api_key="k",
                                      auth_header="x-api-key", enabled=False,
                                      models=["m2"]),
            },
            router=RouterConfig(default="on/m1"),
        )
        app = create_app(cfg)
        with TestClient(app) as client:
            r = client.get("/v1/models")
            ids = [m["id"] for m in r.json()["data"]]
            self.assertIn("on/m1", ids)
            self.assertNotIn("off/m2", ids)


class TestMessagesForward(unittest.TestCase):
    def test_messages_delegates_to_forward_request(self):
        from starlette.testclient import TestClient
        from fastapi.responses import JSONResponse
        app = create_app(_config())
        with TestClient(app) as client:
            with patch("suanpan.main.forward_request",
                       new=AsyncMock(return_value=JSONResponse({"ok": True}))) as fwd:
                r = client.post("/v1/messages", json={"model": "test-model"})
            fwd.assert_awaited_once()
            self.assertEqual(r.status_code, 200)

    def test_count_tokens_delegates_to_forward_count_tokens(self):
        from starlette.testclient import TestClient
        from fastapi.responses import JSONResponse
        app = create_app(_config())
        with TestClient(app) as client:
            with patch("suanpan.main.forward_count_tokens",
                       new=AsyncMock(return_value=JSONResponse({"input_tokens": 1}))) as fwd:
                r = client.post("/v1/messages/count_tokens", json={"model": "test-model"})
            fwd.assert_awaited_once()
            self.assertEqual(r.status_code, 200)


class TestBodyLimitContentLength(unittest.TestCase):
    def test_oversized_content_length_returns_413(self):
        from starlette.testclient import TestClient
        cfg = _config()
        cfg.body_limit_mb = 0  # max_bytes = 0 -> any body exceeds
        app = create_app(cfg)
        with TestClient(app) as client:
            r = client.post("/v1/messages", json={"model": "x"})
            self.assertEqual(r.status_code, 413)


# ── ADR-010 转换 A：openai 出站车道（/v1/messages 入站 × openai 端点）──

import json as _json

import httpx


def _openai_config():
    return AppConfig(
        providers={
            "oai": ProviderConfig(
                base_url="https://api.openai.com/v1",
                api_key="sk-openai",
                protocol="openai",
                enabled=True,
                models=["gpt-4o-mini"],
            )
        },
        router=RouterConfig(default="oai/gpt-4o-mini"),
    )


def _mock_client(response):
    client = MagicMock()
    client.build_request.side_effect = (
        lambda method, url, json=None, headers=None:
            httpx.Request(method, url, json=json, headers=headers))
    client.send = AsyncMock(return_value=response)
    return client


class TestOpenAIOutboundLane(unittest.TestCase):

    def test_nonstream_request_converted_and_response_translated(self):
        from starlette.testclient import TestClient
        app = create_app(_openai_config())
        upstream = httpx.Response(
            200, headers={"content-type": "application/json"},
            json={"id": "c1", "model": "gpt-4o-mini",
                  "choices": [{"finish_reason": "stop",
                               "message": {"content": "hello"}}],
                  "usage": {"prompt_tokens": 10, "completion_tokens": 5}})
        with TestClient(app) as tc:
            app.state.http_client = _mock_client(upstream)
            r = tc.post("/v1/messages", json={
                "model": "claude-sonnet-4-5", "max_tokens": 64,
                "system": "be brief",
                "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["x-suanpan-provider"], "oai")
        body = r.json()
        self.assertEqual(body["type"], "message")
        self.assertEqual(body["content"][0]["text"], "hello")
        self.assertEqual(body["usage"]["input_tokens"], 10)
        # 出站请求：URL/协议头/转换后 body
        sent = app.state.http_client.send.call_args.args[0]
        self.assertEqual(str(sent.url),
                         "https://api.openai.com/v1/chat/completions")
        self.assertEqual(sent.headers["authorization"], "Bearer sk-openai")
        sent_body = _json.loads(sent.content)
        self.assertEqual(sent_body["model"], "gpt-4o-mini")
        self.assertEqual(sent_body["messages"][0],
                         {"role": "system", "content": "be brief"})
        self.assertEqual(sent_body["max_tokens"], 64)

    def test_stream_translated_to_anthropic_sse(self):
        from starlette.testclient import TestClient
        app = create_app(_openai_config())

        def chunk(delta=None, finish=None, usage=None):
            obj = {"id": "c", "object": "chat.completion.chunk",
                   "model": "gpt-4o-mini",
                   "choices": [{"index": 0, "delta": delta or {},
                                "finish_reason": finish}]}
            if usage is not None:
                obj = {"id": "c", "object": "chat.completion.chunk",
                       "model": "gpt-4o-mini", "choices": [], "usage": usage}
            return ("data: " + _json.dumps(obj) + "\n\n").encode()

        parts = [chunk({"role": "assistant"}),
                 chunk({"content": "Hel"}),
                 chunk({"content": "lo"}),
                 chunk(finish="stop"),
                 chunk(usage={"prompt_tokens": 9, "completion_tokens": 2}),
                 b"data: [DONE]\n\n"]

        async def stream_gen():
            for p in parts:
                yield p

        upstream = httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=stream_gen())
        with TestClient(app) as tc:
            app.state.http_client = _mock_client(upstream)
            r = tc.post("/v1/messages", json={
                "model": "claude-sonnet-4-5", "max_tokens": 64,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 200)
        text = r.text
        self.assertIn("event: message_start", text)
        self.assertIn("event: message_stop", text)
        deltas = [_json.loads(line[6:]) for line in text.split("\n")
                  if line.startswith("data: ")
                  and _json.loads(line[6:]).get("type") == "content_block_delta"]
        joined = "".join(e["delta"].get("text", "")
                         for e in deltas if "text" in e["delta"])
        self.assertEqual(joined, "Hello")
        # stream_options 已注入（末块用量依赖它）
        sent = app.state.http_client.send.call_args.args[0]
        sent_body = _json.loads(sent.content)
        self.assertEqual(sent_body["stream_options"], {"include_usage": True})

    def test_upstream_5xx_becomes_502(self):
        from starlette.testclient import TestClient
        app = create_app(_openai_config())
        upstream = httpx.Response(503, json={"error": {"message": "down"}})
        with TestClient(app) as tc:
            app.state.http_client = _mock_client(upstream)
            r = tc.post("/v1/messages", json={
                "model": "claude-sonnet-4-5", "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.headers["x-suanpan-provider"], "oai")

    def test_upstream_4xx_translated_to_anthropic_error(self):
        from starlette.testclient import TestClient
        app = create_app(_openai_config())
        upstream = httpx.Response(
            401, headers={"content-type": "application/json"},
            json={"error": {"message": "bad key"}})
        with TestClient(app) as tc:
            app.state.http_client = _mock_client(upstream)
            r = tc.post("/v1/messages", json={
                "model": "claude-sonnet-4-5", "max_tokens": 8,
                "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["type"], "error")
        self.assertEqual(r.json()["error"]["message"], "bad key")

    def test_count_tokens_rejected_for_openai_protocol(self):
        from starlette.testclient import TestClient
        app = create_app(_openai_config())
        with TestClient(app) as tc:
            r = tc.post("/v1/messages/count_tokens", json={
                "model": "claude-sonnet-4-5",
                "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("count_tokens", r.json()["error"]["message"])


# ── ADR-010 直通车道：/v1/chat/completions（openai 入站 × openai 端点）──

class TestChatCompletionsLane(unittest.TestCase):

    def test_passthrough_nonstream_bytes_identical(self):
        from starlette.testclient import TestClient
        app = create_app(_openai_config())
        upstream_payload = {"id": "c9", "object": "chat.completion",
                            "choices": [{"finish_reason": "stop",
                                         "message": {"content": "yo"}}],
                            "usage": {"prompt_tokens": 8,
                                      "completion_tokens": 1}}
        raw = _json.dumps(upstream_payload).encode()

        async def _gen():
            yield raw

        upstream = httpx.Response(
            200, headers={"content-type": "application/json"},
            content=_gen())
        with TestClient(app) as tc:
            app.state.http_client = _mock_client(upstream)
            r = tc.post("/v1/chat/completions", json={
                "model": "anything", "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["x-suanpan-provider"], "oai")
        self.assertEqual(r.json(), upstream_payload)  # 字节直通
        sent = app.state.http_client.send.call_args.args[0]
        self.assertEqual(str(sent.url),
                         "https://api.openai.com/v1/chat/completions")
        sent_body = _json.loads(sent.content)
        self.assertEqual(sent_body["model"], "gpt-4o-mini")  # 路由改写

    def test_passthrough_stream_injects_include_usage(self):
        from starlette.testclient import TestClient
        app = create_app(_openai_config())
        parts = [b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n',
                 b'data: {"choices":[],"usage":{"prompt_tokens":3,'
                 b'"completion_tokens":1}}\n\n',
                 b'data: [DONE]\n\n']

        async def stream_gen():
            for p in parts:
                yield p

        upstream = httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=stream_gen())
        with TestClient(app) as tc:
            app.state.http_client = _mock_client(upstream)
            r = tc.post("/v1/chat/completions", json={
                "model": "anything", "stream": True,
                "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"".join(parts))  # SSE 字节直通
        sent_body = _json.loads(
            app.state.http_client.send.call_args.args[0].content)
        self.assertEqual(sent_body["stream_options"], {"include_usage": True})

    def test_subagent_marker_routes_and_stripped(self):
        from starlette.testclient import TestClient
        app = create_app(_openai_config())
        async def _gen():
            yield b'{"choices": [], "usage": {}}'

        upstream = httpx.Response(200, headers={
            "content-type": "application/json"}, content=_gen())
        with TestClient(app) as tc:
            app.state.http_client = _mock_client(upstream)
            r = tc.post("/v1/chat/completions", json={
                "model": "whatever",
                "messages": [
                    {"role": "system",
                     "content": "x <SUBAGENT-MODEL>oai/gpt-4o-mini"
                                "</SUBAGENT-MODEL> y"},
                    {"role": "user", "content": "hi"},
                ]})
        self.assertEqual(r.status_code, 200)
        sent_body = _json.loads(
            app.state.http_client.send.call_args.args[0].content)
        self.assertEqual(sent_body["messages"][0]["content"], "x  y")

    def test_anthropic_protocol_provider_rejected(self):
        from starlette.testclient import TestClient
        app = create_app(_config())  # test 供应商 = anthropic 协议
        with TestClient(app) as tc:
            r = tc.post("/v1/chat/completions", json={
                "model": "test-model",
                "messages": [{"role": "user", "content": "hi"}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("Anthropic 端点", r.json()["error"]["message"])

    def test_no_route_matched_openai_error_shape(self):
        from starlette.testclient import TestClient
        cfg = AppConfig(
            providers={"oai": ProviderConfig(
                base_url="https://api.openai.com/v1", api_key="k",
                protocol="openai", models=["m"])},
            router=RouterConfig(default=None))
        app = create_app(cfg)
        with TestClient(app) as tc:
            r = tc.post("/v1/chat/completions", json={
                "model": "m", "messages": [{"role": "user", "content": "x"}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("no route matched", r.json()["error"]["message"])

    def test_invalid_json_openai_error_shape(self):
        from starlette.testclient import TestClient
        app = create_app(_openai_config())
        with TestClient(app) as tc:
            r = tc.post("/v1/chat/completions",
                        content=b"{not json",
                        headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["type"], "invalid_request_error")


# ── ADR-010 M3a：/v1/responses（Codex 直通车道）────────────────────

def _responses_config(**extra):
    return AppConfig(
        providers={
            "oai": ProviderConfig(
                base_url="https://api.openai.com/v1",
                api_key="sk-openai",
                protocol="openai",
                responses_base_url="https://api.openai.com/v1",
                models=["gpt-5.2"], **extra),
        },
        router=RouterConfig(default="oai/gpt-5.2"),
    )


class TestResponsesLane(unittest.TestCase):

    def test_passthrough_stream_to_native_responses_endpoint(self):
        from starlette.testclient import TestClient
        app = create_app(_responses_config())
        parts = [
            b'event: response.created\ndata: {"type":"response.created"}\n\n',
            b'event: response.completed\ndata: {"type":"response.completed",'
            b'"response":{"usage":{"input_tokens":30,"output_tokens":4}}}\n\n',
        ]

        async def stream_gen():
            for p in parts:
                yield p

        upstream = httpx.Response(
            200, headers={"content-type": "text/event-stream"},
            content=stream_gen())
        with TestClient(app) as tc:
            app.state.http_client = _mock_client(upstream)
            r = tc.post("/v1/responses", json={
                "model": "codex-any", "stream": True, "store": False,
                "instructions": "you are codex",
                "input": [{"role": "user",
                           "content": "hi"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"".join(parts))  # 字节直通
        self.assertEqual(r.headers["x-suanpan-provider"], "oai")
        sent = app.state.http_client.send.call_args.args[0]
        self.assertEqual(str(sent.url),
                         "https://api.openai.com/v1/responses")
        self.assertEqual(sent.headers["authorization"], "Bearer sk-openai")
        sent_body = _json.loads(sent.content)
        self.assertEqual(sent_body["model"], "gpt-5.2")  # 路由改写
        self.assertEqual(sent_body["store"], False)      # 其余字段原样

    def test_without_responses_base_url_rejected(self):
        from starlette.testclient import TestClient
        app = create_app(_openai_config())  # 无 responses_base_url
        with TestClient(app) as tc:
            r = tc.post("/v1/responses", json={
                "model": "x", "input": []})
        self.assertEqual(r.status_code, 400)
        self.assertIn("暂不支持 Responses", r.json()["error"]["message"])

    def test_invalid_json(self):
        from starlette.testclient import TestClient
        app = create_app(_responses_config())
        with TestClient(app) as tc:
            r = tc.post("/v1/responses", content=b"{bad",
                        headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["error"]["type"], "invalid_request_error")
