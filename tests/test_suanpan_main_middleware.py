"""Tests for suanpan/main.py — middleware + routes via TestClient."""
import unittest
from unittest.mock import patch, AsyncMock

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
