"""Tests for suanpan/compat.py — per-provider body normalization.

Seam under test: normalize_body(body, provider) → in-place mutation.
Pure function, no I/O — the highest-priority testing surface for the
compatibility layer.
"""
import unittest

from suanpan.compat import normalize_body


# ── _flatten_system (via normalize_body) ───────────────────────────

class TestFlattenSystem(unittest.TestCase):
    def test_string_system_unchanged(self):
        body = {"system": "You are helpful.", "model": "test"}
        normalize_body(body, "deepseek")
        self.assertEqual(body["system"], "You are helpful.")

    def test_array_system_flattened_to_string(self):
        body = {
            "system": [
                {"type": "text", "text": "You are helpful.",
                 "cache_control": {"type": "ephemeral"}},
            ],
            "model": "test",
        }
        normalize_body(body, "deepseek")
        self.assertEqual(body["system"], "You are helpful.")

    def test_multi_block_system_concatenated(self):
        body = {
            "system": [
                {"type": "text", "text": "Rule 1."},
                {"type": "text", "text": "Rule 2."},
            ],
            "model": "test",
        }
        normalize_body(body, "deepseek")
        self.assertEqual(body["system"], "Rule 1.\nRule 2.")

    def test_cache_control_dropped(self):
        body = {
            "system": [
                {"type": "text", "text": "prompt",
                 "cache_control": {"type": "ephemeral"}},
            ],
            "model": "test",
        }
        normalize_body(body, "deepseek")
        self.assertIsInstance(body["system"], str)
        self.assertNotIn("cache_control", body)

    def test_system_missing_no_error(self):
        body = {"model": "test"}
        normalize_body(body, "deepseek")
        self.assertNotIn("system", body)

    def test_system_none_no_error(self):
        body = {"system": None, "model": "test"}
        normalize_body(body, "deepseek")
        self.assertIsNone(body["system"])

    def test_non_text_block_skipped(self):
        body = {
            "system": [
                {"type": "image", "source": {"url": "data:..."}},
                {"type": "text", "text": "keep this"},
            ],
            "model": "test",
        }
        normalize_body(body, "deepseek")
        self.assertEqual(body["system"], "keep this")


# ── Cross-provider uniformity ──────────────────────────────────────

class TestProviderUniformity(unittest.TestCase):
    """Default-path normalizations are provider-agnostic; providers that
    natively accept Anthropic bodies opt out via anthropic_native."""

    def test_same_result_all_providers(self):
        body = {
            "system": [{"type": "text", "text": "hello"}],
            "model": "test",
        }
        for provider in ("deepseek", "glm", "kimi"):
            b = {k: v for k, v in body.items()}
            normalize_body(b, provider)
            self.assertEqual(b["system"], "hello")


# ── anthropic_native opt-out ───────────────────────────────────────

class TestAnthropicNative(unittest.TestCase):
    """anthropic_native=True skips all compatibility stripping: the backend
    natively accepts Anthropic body shapes, so cache_control markers (prompt
    caching), document blocks, and beta tool fields must survive (#5)."""

    def test_system_array_and_cache_control_preserved(self):
        system = [
            {"type": "text", "text": "You are helpful.",
             "cache_control": {"type": "ephemeral"}},
        ]
        body = {"system": system, "model": "test"}
        normalize_body(body, "anthropic", anthropic_native=True)
        self.assertEqual(body["system"], system)

    def test_document_blocks_preserved(self):
        body = {
            "model": "test",
            "messages": [{"role": "user", "content": [
                {"type": "document", "source": {"type": "base64"}},
                {"type": "text", "text": "see attached"},
            ]}],
        }
        normalize_body(body, "anthropic", anthropic_native=True)
        content = body["messages"][0]["content"]
        self.assertEqual([b["type"] for b in content], ["document", "text"])

    def test_beta_tool_fields_preserved(self):
        body = {
            "model": "test",
            "tools": [{"name": "t", "defer_loading": True,
                       "eager_input_streaming": True}],
        }
        normalize_body(body, "anthropic", anthropic_native=True)
        self.assertTrue(body["tools"][0]["defer_loading"])
        self.assertTrue(body["tools"][0]["eager_input_streaming"])

    def test_default_still_flattens_same_body(self):
        system = [{"type": "text", "text": "x",
                   "cache_control": {"type": "ephemeral"}}]
        body = {"system": system, "model": "test"}
        normalize_body(body, "deepseek")
        self.assertEqual(body["system"], "x")


# ── In-place mutation ──────────────────────────────────────────────

class TestInPlaceMutation(unittest.TestCase):
    def test_returns_none(self):
        body = {"system": "x", "model": "test"}
        result = normalize_body(body, "deepseek")
        self.assertIsNone(result)

    def test_mutates_body_in_place(self):
        body = {"system": [{"type": "text", "text": "x"}], "model": "test"}
        normalize_body(body, "deepseek")
        # Same object, mutated
        self.assertEqual(body["system"], "x")


# ── thinking untouched ─────────────────────────────────────────────

class TestThinkingUntouched(unittest.TestCase):
    def test_thinking_preserved(self):
        body = {
            "system": [{"type": "text", "text": "x"}],
            "thinking": {"type": "enabled", "budget_tokens": 16000},
            "model": "test",
        }
        normalize_body(body, "deepseek")
        self.assertEqual(body["thinking"], {"type": "enabled", "budget_tokens": 16000})


# ── _strip_document_blocks (via normalize_body) ────────────────────

class TestStripDocumentBlocks(unittest.TestCase):
    def test_document_block_removed(self):
        body = {
            "messages": [
                {"role": "user", "content": [
                    {"type": "document", "source": {"type": "base64"}},
                    {"type": "text", "text": "describe this"},
                ]},
            ],
            "model": "test",
        }
        normalize_body(body, "kimi")
        content = body["messages"][0]["content"]
        types = [b["type"] for b in content]
        self.assertNotIn("document", types)
        self.assertIn("text", types)

    def test_all_document_replaced_with_placeholder(self):
        body = {
            "messages": [
                {"role": "user", "content": [
                    {"type": "document", "source": {"type": "base64"}},
                ]},
            ],
            "model": "test",
        }
        normalize_body(body, "kimi")
        content = body["messages"][0]["content"]
        self.assertEqual(len(content), 1)
        self.assertEqual(content[0]["type"], "text")

    def test_no_document_unchanged(self):
        body = {
            "messages": [
                {"role": "user", "content": [
                    {"type": "text", "text": "hello"},
                ]},
            ],
            "model": "test",
        }
        normalize_body(body, "kimi")
        self.assertEqual(body["messages"][0]["content"][0]["text"], "hello")

    def test_content_string_no_error(self):
        body = {
            "messages": [
                {"role": "user", "content": "plain string"},
            ],
            "model": "test",
        }
        normalize_body(body, "kimi")
        self.assertEqual(body["messages"][0]["content"], "plain string")

    def test_messages_missing_no_error(self):
        body = {"model": "test"}
        normalize_body(body, "kimi")
        self.assertNotIn("messages", body)


# ── _strip_beta_tool_fields (via normalize_body) ───────────────────

class TestStripBetaToolFields(unittest.TestCase):
    def test_defer_loading_removed(self):
        body = {
            "tools": [
                {"name": "Bash", "defer_loading": True,
                 "input_schema": {"type": "object"}},
            ],
            "model": "test",
        }
        normalize_body(body, "deepseek")
        self.assertNotIn("defer_loading", body["tools"][0])

    def test_eager_input_streaming_removed(self):
        body = {
            "tools": [
                {"name": "Read", "eager_input_streaming": True,
                 "input_schema": {"type": "object"}},
            ],
            "model": "test",
        }
        normalize_body(body, "deepseek")
        self.assertNotIn("eager_input_streaming", body["tools"][0])

    def test_clean_tools_unchanged(self):
        body = {
            "tools": [
                {"name": "Write", "input_schema": {"type": "object"}},
            ],
            "model": "test",
        }
        normalize_body(body, "deepseek")
        self.assertEqual(body["tools"][0]["name"], "Write")
        self.assertIn("input_schema", body["tools"][0])

    def test_tools_missing_no_error(self):
        body = {"model": "test"}
        normalize_body(body, "deepseek")
        self.assertNotIn("tools", body)

    def test_both_fields_removed_simultaneously(self):
        body = {
            "tools": [
                {"name": "Multi", "defer_loading": True,
                 "eager_input_streaming": True,
                 "input_schema": {"type": "object"}},
            ],
            "model": "test",
        }
        normalize_body(body, "deepseek")
        self.assertNotIn("defer_loading", body["tools"][0])
        self.assertNotIn("eager_input_streaming", body["tools"][0])
        self.assertEqual(body["tools"][0]["name"], "Multi")
