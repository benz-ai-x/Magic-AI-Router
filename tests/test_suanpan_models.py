"""Tests for GET /v1/models endpoint in suanpan/main.py."""
import unittest
from unittest.mock import MagicMock

from suanpan.config import AppConfig, ProviderConfig, RouterConfig
from suanpan.main import create_app


def _make_config(**overrides):
    defaults = {
        "providers": {
            "deepseek": ProviderConfig(
                base_url="https://api.deepseek.com/anthropic",
                api_key="k1",
                auth_header="Authorization",
                enabled=True,
                models=["deepseek-v4-flash", "deepseek-v4-pro"],
            ),
            "kimi": ProviderConfig(
                base_url="https://api.kimi.com/coding",
                api_key="k2",
                auth_header="Authorization",
                enabled=True,
                models=["kimi-for-coding", "k3"],
            ),
            "glm": ProviderConfig(
                base_url="https://open.bigmodel.cn/api/anthropic",
                api_key="k3",
                auth_header="x-api-key",
                enabled=False,
                models=["glm-5.2"],
            ),
        },
        "router": RouterConfig(default="deepseek/deepseek-v4-pro"),
    }
    defaults.update(overrides)
    return AppConfig(**defaults)


class TestListModels(unittest.TestCase):
    def setUp(self):
        self.app = create_app(_make_config())

    def test_route_registered(self):
        routes = [r for r in self.app.routes if getattr(r, "path", "") == "/v1/models"]
        self.assertEqual(len(routes), 1)
        self.assertIn("GET", routes[0].methods)

    def test_returns_anthropic_format(self):
        import asyncio
        import json

        async def call():
            scope = {
                "type": "http", "method": "GET", "path": "/v1/models",
                "headers": [], "query_string": b"",
                "server": ("127.0.0.1", 9527), "client": ("127.0.0.1", 0),
            }
            resp_body = b""

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                nonlocal resp_body
                if message["type"] == "http.response.body":
                    resp_body += message.get("body", b"")

            await self.app(scope, receive, send)
            return json.loads(resp_body)

        data = asyncio.run(call())
        self.assertEqual(data["object"], "list")
        self.assertFalse(data["has_more"])
        self.assertIn("data", data)
        self.assertIsInstance(data["data"], list)

    def test_excludes_disabled_providers(self):
        import asyncio
        import json

        async def call():
            scope = {
                "type": "http", "method": "GET", "path": "/v1/models",
                "headers": [], "query_string": b"",
                "server": ("127.0.0.1", 9527), "client": ("127.0.0.1", 0),
            }
            resp_body = b""

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                nonlocal resp_body
                if message["type"] == "http.response.body":
                    resp_body += message.get("body", b"")

            await self.app(scope, receive, send)
            return json.loads(resp_body)

        data = asyncio.run(call())
        model_ids = [m["id"] for m in data["data"]]
        # enabled providers: deepseek (2), kimi (2); disabled glm excluded
        self.assertEqual(len(model_ids), 4)
        self.assertIn("deepseek/deepseek-v4-flash", model_ids)
        self.assertIn("deepseek/deepseek-v4-pro", model_ids)
        self.assertIn("kimi/kimi-for-coding", model_ids)
        self.assertIn("kimi/k3", model_ids)
        self.assertNotIn("glm/glm-5.2", model_ids)

    def test_empty_providers(self):
        import asyncio
        import json

        app = create_app(_make_config(providers={}, router=RouterConfig()))

        async def call():
            scope = {
                "type": "http", "method": "GET", "path": "/v1/models",
                "headers": [], "query_string": b"",
                "server": ("127.0.0.1", 9527), "client": ("127.0.0.1", 0),
            }
            resp_body = b""

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                nonlocal resp_body
                if message["type"] == "http.response.body":
                    resp_body += message.get("body", b"")

            await app(scope, receive, send)
            return json.loads(resp_body)

        data = asyncio.run(call())
        self.assertEqual(data["data"], [])
        self.assertIsNone(data["first_id"])
        self.assertIsNone(data["last_id"])

    def test_entry_format(self):
        import asyncio
        import json

        app = create_app(_make_config())

        async def call():
            scope = {
                "type": "http", "method": "GET", "path": "/v1/models",
                "headers": [], "query_string": b"",
                "server": ("127.0.0.1", 9527), "client": ("127.0.0.1", 0),
            }
            resp_body = b""

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                nonlocal resp_body
                if message["type"] == "http.response.body":
                    resp_body += message.get("body", b"")

            await app(scope, receive, send)
            return json.loads(resp_body)

        data = asyncio.run(call())
        entry = data["data"][0]
        self.assertIn("id", entry)
        self.assertIn("type", entry)
        self.assertEqual(entry["type"], "model")
        self.assertIn("display_name", entry)
        self.assertIn("created_at", entry)


if __name__ == "__main__":
    unittest.main()
