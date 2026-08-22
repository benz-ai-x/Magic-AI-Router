"""Coverage tests for Suanpan gateway: middleware, config, router, compat, usage_log.

Targets the specific uncovered lines reported by ``--cov-report=term-missing``
for the five files in ``suanpan/``.
"""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.requests import Request
from starlette.responses import PlainTextResponse

from suanpan.compat import normalize_body
from suanpan.config import (
    AppConfig,
    ProviderConfig,
    load_config_raw,
)
from suanpan.middleware import BodyLimitMiddleware
from suanpan.router import strip_marker
from suanpan.usage_log import UsageEntry, UsageLogger


# ── suanpan/middleware.py: 47-48, 59-76 ─────────────────────────────


class TestBodyLimitInvalidContentLength(unittest.TestCase):
    """Line 47-48: invalid Content-Length header returns 400."""

    def test_non_numeric_content_length_returns_400(self):
        middleware = BodyLimitMiddleware(app=None, max_bytes=100)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [(b"content-length", b"not-a-number")],
        }

        async def raw_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        request = Request(scope, receive=raw_receive)

        async def call_next(req):
            return PlainTextResponse("ok")

        response = asyncio.run(middleware.dispatch(request, call_next))
        self.assertEqual(response.status_code, 400)


class TestBodyLimitChunkedUnderLimit(unittest.TestCase):
    """Lines 59-72: slow path (no Content-Length) with body under the limit."""

    def test_chunked_body_within_limit_passes(self):
        middleware = BodyLimitMiddleware(app=None, max_bytes=100)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],  # No content-length -> slow path
        }

        async def raw_receive():
            return {"type": "http.request", "body": b"hello", "more_body": False}

        request = Request(scope, receive=raw_receive)

        async def call_next(req):
            # Downstream app reads the body through the wrapped receive
            await req.receive()
            return PlainTextResponse("ok")

        response = asyncio.run(middleware.dispatch(request, call_next))
        self.assertEqual(response.status_code, 200)


class TestBodyLimitChunkedOverLimit(unittest.TestCase):
    """Lines 68-69, 73-76: slow path body exceeds limit -> _BodyTooLarge -> 413."""

    def test_chunked_body_exceeds_limit_returns_413(self):
        middleware = BodyLimitMiddleware(app=None, max_bytes=10)

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],  # No content-length -> slow path
        }

        async def raw_receive():
            return {"type": "http.request", "body": b"x" * 100, "more_body": False}

        request = Request(scope, receive=raw_receive)

        async def call_next(req):
            # This triggers limited_receive -> raises _BodyTooLarge
            await req.receive()
            return PlainTextResponse("ok")

        response = asyncio.run(middleware.dispatch(request, call_next))
        self.assertEqual(response.status_code, 413)


# ── suanpan/config.py: 33, 106, 154-155, 198 ───────────────────────


class TestProviderConfigAuthValidator(unittest.TestCase):
    """Line 33: api_key_env without auth_header raises ValueError."""

    def test_api_key_env_without_auth_header_raises(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError) as ctx:
            ProviderConfig(base_url="http://x", api_key_env="MY_KEY")
        self.assertIn("auth_header is null", str(ctx.exception))


class TestConfigRouteTargetSlashEdge(unittest.TestCase):
    """Line 106: target starting with '/' yields empty provider from partition,
    triggering the comma-split fallback."""

    def test_target_starting_with_slash_triggers_comma_fallback(self):
        from pydantic import ValidationError

        with self.assertRaises(ValidationError) as ctx:
            AppConfig.model_validate({
                "providers": {"p": {"base_url": "http://x"}},
                "router": {"default": "/something"},
            })
        # The error should reference the (still invalid) provider
        self.assertIn("unknown provider", str(ctx.exception))


class TestLoadConfigRawCorruptYaml(unittest.TestCase):
    """Lines 154-155: corrupted YAML file returns empty dict."""

    def test_corrupt_yaml_returns_empty_dict(self):
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("{ invalid yaml [[[{")
            path = f.name
        try:
            result = load_config_raw(path)
            self.assertEqual(result, {})
        finally:
            Path(path).unlink(missing_ok=True)


class TestStripMarkerListSystem(unittest.TestCase):
    """Lines 103-106: strip_marker on a list-format system field."""

    def test_strip_marker_from_list_system(self):
        body = {
            "system": [
                {
                    "type": "text",
                    "text": "before <SUBAGENT-MODEL>p/m</SUBAGENT-MODEL> after",
                },
                {
                    "type": "text",
                    "text": "clean text",
                },
            ]
        }
        strip_marker(body)
        self.assertNotIn("SUBAGENT-MODEL", body["system"][0]["text"])
        self.assertEqual(body["system"][0]["text"], "before  after")
        self.assertEqual(body["system"][1]["text"], "clean text")

    def test_strip_marker_list_with_non_text_item(self):
        """Non-text dict items in the list are skipped (line 105 guard)."""
        body = {
            "system": [
                {"type": "image", "source": {"url": "data:..."}},
                {"type": "text", "text": "<SUBAGENT-MODEL>p/m</SUBAGENT-MODEL>"},
            ]
        }
        strip_marker(body)
        # Image block untouched
        self.assertEqual(body["system"][0]["type"], "image")
        # Text block cleaned
        self.assertNotIn("SUBAGENT-MODEL", body["system"][1]["text"])


# ── suanpan/compat.py: 82 ───────────────────────────────────────────


class TestStripDocumentBlocksNonDictMessage(unittest.TestCase):
    """Line 82: non-dict message in messages array is skipped via continue."""

    def test_non_dict_message_skipped(self):
        body = {
            "messages": [
                "not a dict",
                42,
                {"role": "user", "content": [
                    {"type": "document", "source": {"type": "base64"}},
                    {"type": "text", "text": "hello"},
                ]},
            ],
            "model": "test",
        }
        # Should not crash; only the dict message's document block is stripped
        normalize_body(body, "kimi")
        self.assertEqual(body["messages"][0], "not a dict")
        self.assertEqual(body["messages"][1], 42)
        types = [b["type"] for b in body["messages"][2]["content"]]
        self.assertNotIn("document", types)
        self.assertIn("text", types)


# ── suanpan/usage_log.py: 68-69 ─────────────────────────────────────


class TestUsageLoggerRotateOSError(unittest.TestCase):
    """Lines 68-69: OSError during rotation is swallowed (best-effort)."""

    def test_oserror_during_rotation_does_not_raise(self):
        with tempfile.TemporaryDirectory() as d:
            logger = UsageLogger(enabled=True, path=str(Path(d) / "u.jsonl"))
            logger.path.write_text("existing content")
            logger._MAX_BYTES = 0  # Force rotation condition

            # rename fails -> OSError swallowed
            with patch.object(Path, "rename", side_effect=OSError("permission denied")):
                logger._maybe_rotate()

            # Original file is still intact (rename failed)
            self.assertTrue(logger.path.exists())
            self.assertEqual(logger.path.read_text(), "existing content")

    def test_oserror_during_write_does_not_block(self):
        """Full write() call should complete even if rotation hits OSError."""
        entry = UsageEntry(
            provider="p", source_model="m", target_model="m2", scenario="default",
            input_tokens=10, output_tokens=5, cache_read_tokens=0,
            cache_creation_tokens=0, latency_ms=100, status=200, error=None,
        )
        with tempfile.TemporaryDirectory() as d:
            logger = UsageLogger(enabled=True, path=str(Path(d) / "u.jsonl"))
            # Pre-write a file so it exists for rotation check
            logger.path.write_text("x" * 200)
            logger._MAX_BYTES = 10  # Force rotation

            with patch.object(Path, "rename", side_effect=OSError("test")):
                logger.write(entry)  # Should not raise; entry still written

            # The write should have appended to the original file
            content = logger.path.read_text()
            self.assertIn('"provider": "p"', content)


if __name__ == "__main__":
    unittest.main()


class TestExampleConfigValidates(unittest.TestCase):
    """#47 T4a：发货样例必须过自己的 schema——菜单「复制配置样例」发给
    用户的文件，复制即用不得踩雷（样例↔schema 永久锁死）。"""

    def test_example_yaml_loads(self):
        from suanpan.config import load_config
        from pathlib import Path
        example = Path(__file__).resolve().parents[1] / "docs" / "examples" / "suanpan.example.yaml"
        cfg = load_config(str(example))
        self.assertEqual(cfg.listen_port, 9527)
        self.assertIn("anthropic", cfg.providers)
        self.assertIn("deepseek", cfg.providers)


class TestNullSectionTolerance(unittest.TestCase):
    """#47 T4b：「节标题 + 下面全注释」= YAML null——最常见手编姿势，
    显式 null 应等价于缺省（缺失 providers 键仍报错，不放宽必填）。"""

    def _load(self, text):
        from suanpan.config import load_config
        import tempfile
        import os
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write(text)
            p = f.name
        try:
            return load_config(p)
        finally:
            os.unlink(p)

    def test_null_rules_router_usage_log_tolerated(self):
        cfg = self._load(
            "providers:\n"
            "  p:\n    base_url: https://x.example\n    api_key: k\n"
            "rules:\n"
            "router:\n"
            "usage_log:\n")
        self.assertEqual(cfg.rules, [])
        self.assertEqual(cfg.router.default, None)
        self.assertTrue(cfg.usage_log.enabled)

    def test_null_providers_tolerated(self):
        cfg = self._load("providers:\n")
        self.assertEqual(cfg.providers, {})

    def test_missing_providers_still_rejected(self):
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            self._load("listen_port: 9527\n")

