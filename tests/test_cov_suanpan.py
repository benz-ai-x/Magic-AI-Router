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
    save_config_dict,
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


class TestSaveConfigDictWriteFailure(unittest.TestCase):
    """Line 198: atomic_write returning False yields a failure result."""

    def test_atomic_write_failure_returns_error(self):
        valid = {
            "providers": {
                "p": {
                    "base_url": "http://x",
                    "api_key": "k",
                    "auth_header": "x-api-key",
                },
            },
            "router": {"default": "p/m"},
        }
        with patch("config_store.atomic_write", return_value=False):
            ok, err = save_config_dict(valid, "/tmp/nonexistent_path/test_sp.yaml")
        self.assertFalse(ok)
        self.assertIn("写入失败", err)


# ── suanpan/router.py: 103-106 ──────────────────────────────────────


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
