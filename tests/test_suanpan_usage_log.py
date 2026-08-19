"""Tests for suanpan/usage_log.py — UsageLogger write + rotate."""
import json
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from suanpan.usage_log import UsageLogger, UsageEntry


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

    def test_rolling_totals_accumulate(self):
        with tempfile.TemporaryDirectory() as d:
            logger = UsageLogger(enabled=True, path=str(Path(d) / "u.jsonl"))
            logger.write(_entry(input_tokens=100, output_tokens=50))
            logger.write(_entry(input_tokens=200, output_tokens=30))
            self.assertEqual(logger.rolling["calls"], 2)
            self.assertEqual(logger.rolling["input_tokens"], 300)
            self.assertEqual(logger.rolling["output_tokens"], 80)

    def test_error_counted_in_rolling(self):
        with tempfile.TemporaryDirectory() as d:
            logger = UsageLogger(enabled=True, path=str(Path(d) / "u.jsonl"))
            logger.write(_entry(status=500))
            self.assertEqual(logger.rolling["errors"], 1)


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
