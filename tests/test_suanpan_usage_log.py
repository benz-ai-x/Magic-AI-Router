"""Tests for suanpan/usage_log.py — UsageLogger write + rotate."""
import json
import tempfile
import unittest
from pathlib import Path

from suanpan.usage_log import (
    UsageEntry, UsageLogger, usage_entry_from_wire,
)


def _entry(**overrides):
    defaults = dict(
        provider="p", source_model="m", target_model="m2", scenario="default",
        input_tokens=10, output_tokens=5, cache_read_tokens=0,
        cache_creation_tokens=0, latency_ms=100, status=200, error=None,
    )
    defaults.update(overrides)
    return UsageEntry(**defaults)


class TestUsageLoggerWrite(unittest.TestCase):
    def test_write_appends_jsonl(self):
        with tempfile.TemporaryDirectory() as d:
            logger = UsageLogger(enabled=True, path=str(Path(d) / "u.jsonl"))
            logger.write(_entry(provider="deepseek"))
            logger.write(_entry(provider="kimi"))
            data = (Path(d) / "u.jsonl").read_text().strip().split("\n")
            self.assertEqual(len(data), 2)
            self.assertEqual(json.loads(data[0])["provider"], "deepseek")
            self.assertEqual(json.loads(data[1])["provider"], "kimi")

    def test_disabled_does_not_write(self):
        with tempfile.TemporaryDirectory() as d:
            logger = UsageLogger(enabled=False, path=str(Path(d) / "u.jsonl"))
            logger.write(_entry())
            self.assertFalse((Path(d) / "u.jsonl").exists())


class TestUsageLoggerRotate(unittest.TestCase):
    def test_rotate_renames_old_file(self):
        with tempfile.TemporaryDirectory() as d:
            logger = UsageLogger(enabled=True, path=str(Path(d) / "u.jsonl"))
            # Force small max for testing
            logger._MAX_BYTES = 100
            logger.write(_entry())
            self.assertTrue((Path(d) / "u.jsonl").exists())
            # Write enough to trigger rotation
            for i in range(20):
                logger.write(_entry())
            # Old file should be rotated
            self.assertTrue((Path(d) / "u.jsonl.1").exists())


class TestUsageLoggerExpandUser(unittest.TestCase):
    def test_path_expanded(self):
        logger = UsageLogger(enabled=False, path="~/test.jsonl")
        self.assertNotIn("~", str(logger.path))


class TestUsageEntryFromWire(unittest.TestCase):
    """wire→entry 单一转换归宿（R5）：Anthropic 线格式 usage dict 的键名
    映射 + 缺键兜底 0；身份字段经 overrides 直传。"""

    def test_wire_keys_mapped(self):
        e = usage_entry_from_wire(
            {"input_tokens": 7, "output_tokens": 3,
             "cache_read_input_tokens": 5, "cache_creation_input_tokens": 2},
            provider="p", source_model="m", target_model="m2",
            scenario="default", latency_ms=1, status=200, error=None)
        self.assertEqual(
            (e.input_tokens, e.output_tokens,
             e.cache_read_tokens, e.cache_creation_tokens), (7, 3, 5, 2))

    def test_missing_keys_default_zero(self):
        e = usage_entry_from_wire(
            {"input_tokens": 1},
            provider="p", source_model="m", target_model="m2",
            scenario="default", latency_ms=1, status=200, error=None)
        self.assertEqual(e.output_tokens, 0)
        self.assertEqual(e.cache_read_tokens, 0)
        self.assertEqual(e.cache_creation_tokens, 0)

    def test_none_usage_is_zero_row(self):
        e = usage_entry_from_wire(
            None, provider="p", source_model="m", target_model="m2",
            scenario="default", latency_ms=1, status=502, error="x")
        self.assertEqual((e.input_tokens, e.output_tokens), (0, 0))
