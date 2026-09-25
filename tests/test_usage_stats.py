"""Tests for usage_stats.py — 本地 usage.jsonl 的 CST 聚合（自
balance_usage 三刀切迁出，R5；用例语义不变，仅换导入归宿）。"""
import json
import os
import tempfile
import unittest
from datetime import datetime
from unittest.mock import patch

from services import usage_stats


def _usage_record(**overrides):
    record = {
        "ts": "2026-08-19T12:00:00+08:00",
        "provider": "p",
        "scenario": "default",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "status": 200,
        "latency_ms": 1,
    }
    record.update(overrides)
    return record


def _frozen_datetime():
    """datetime double pinned at 2026-08-19 12:00 CST (matches _usage_record)."""
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 8, 19, 12, 0, tzinfo=usage_stats.CST)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    return FrozenDateTime


class TestFetchUsage(unittest.TestCase):
    def test_missing_log_returns_zero(self):
        result = usage_stats.fetch_usage({"usage_log": {"path": "/nonexistent/x.jsonl"}})
        self.assertEqual(result["total"]["calls"], 0)
        self.assertEqual(result["providers"], {})

    def test_missing_usage_log_config_reads_schema_default_path(self):
        entry = json.dumps(_usage_record(input_tokens=1, output_tokens=1)) + "\n"
        with tempfile.TemporaryDirectory() as home:
            log_dir = os.path.join(home, ".suanpan", "logs")
            os.makedirs(log_dir)
            with open(os.path.join(log_dir, "usage.jsonl"), "w") as f:
                f.write(entry)
            with patch.dict(os.environ, {"HOME": home}):
                result = usage_stats.fetch_usage({})
        self.assertEqual(result["total"]["calls"], 1)

    def test_aggregates_jsonl_entries(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(_usage_record(
                provider="deepseek", input_tokens=10, output_tokens=5,
                status=200, latency_ms=100)) + "\n")
            f.write(json.dumps(_usage_record(
                provider="deepseek", input_tokens=20, output_tokens=5,
                status=500, latency_ms=200)) + "\n")
            f.write("not-json-line\n")  # skipped
            path = f.name
        try:
            result = usage_stats.fetch_usage({"usage_log": {"path": path}})
        finally:
            os.unlink(path)
        self.assertEqual(result["total"]["calls"], 2)
        self.assertEqual(result["total"]["input_tokens"], 30)
        self.assertEqual(result["total"]["errors"], 1)
        self.assertEqual(result["total"]["avg_latency_ms"], 150)
        self.assertEqual(result["providers"]["deepseek"]["calls"], 2)

    def test_aggregates_four_token_buckets_and_cache_hit_rate(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(_usage_record(
                provider="deepseek", input_tokens=10, cache_read_tokens=90,
                output_tokens=5, status=200, latency_ms=100)) + "\n")
            f.write(json.dumps(_usage_record(
                provider="glm", input_tokens=20, cache_read_tokens=30,
                cache_creation_tokens=50, output_tokens=15, status=500,
                latency_ms=300)) + "\n")
            path = f.name
        try:
            result = usage_stats.fetch_usage({"usage_log": {"path": path}})
        finally:
            os.unlink(path)

        self.assertEqual(result["total"]["cache_read_tokens"], 120)
        self.assertEqual(result["total"]["cache_creation_tokens"], 50)
        self.assertEqual(result["total"]["cache_hit_rate"], 0.6)
        self.assertEqual(result["total"]["errors"], 1)
        self.assertEqual(result["total"]["avg_latency_ms"], 200)
        self.assertEqual(result["providers"]["deepseek"]["cache_hit_rate"], 0.9)
        self.assertEqual(result["providers"]["glm"]["cache_hit_rate"], 0.3)

    def test_groups_usage_by_cst_day_and_route_source(self):
        entries = [
            {
                "ts": "2026-08-18T09:00:00+08:00", "provider": "deepseek",
                "scenario": "rule", "input_tokens": 10,
                "cache_read_tokens": 10, "cache_creation_tokens": 0,
                "output_tokens": 4, "status": 200, "latency_ms": 100,
            },
            {
                "ts": "2026-08-18T10:00:00+08:00", "provider": "deepseek",
                "scenario": "default", "input_tokens": 0,
                "cache_read_tokens": 0, "cache_creation_tokens": 0,
                "output_tokens": 1, "status": 502, "latency_ms": 300,
            },
            {
                "ts": "2026-08-19T11:00:00+08:00", "provider": "glm",
                "scenario": "inline", "input_tokens": 20,
                "cache_read_tokens": 30, "cache_creation_tokens": 50,
                "output_tokens": 2, "status": 200, "latency_ms": 500,
            },
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            for entry in entries:
                f.write(json.dumps(entry) + "\n")
            path = f.name
        try:
            result = usage_stats.fetch_usage({"usage_log": {"path": path}})
        finally:
            os.unlink(path)

        self.assertEqual([day["date"] for day in result["daily"]],
                         ["2026-08-18", "2026-08-19"])
        self.assertEqual(result["daily"][0]["calls"], 2)
        self.assertEqual(result["daily"][0]["errors"], 1)
        self.assertEqual(result["daily"][0]["cache_hit_rate"], 0.5)
        self.assertEqual(result["scenarios"]["rule"]["cache_hit_rate"], 0.5)
        self.assertIsNone(result["scenarios"]["default"]["cache_hit_rate"])
        self.assertEqual(result["scenarios"]["inline"]["calls"], 1)

    def test_today_and_seven_day_ranges_use_cst_calendar_boundaries(self):
        timestamps = [
            "2026-08-19T00:00:00+08:00",  # today, exact lower boundary
            "2026-08-18T16:30:00Z",       # today after conversion to CST
            "2026-08-13T00:00:00+08:00",  # seventh calendar day, included
            "2026-08-12T23:59:59+08:00",  # just outside seven days
            "2026-08-20T00:00:00+08:00",  # future, excluded from rolling ranges
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            for ts in timestamps:
                f.write(json.dumps({
                    "ts": ts, "provider": "p", "scenario": "rule",
                    "input_tokens": 1, "output_tokens": 1,
                    "cache_read_tokens": 0, "cache_creation_tokens": 0,
                    "status": 200, "latency_ms": 1,
                }) + "\n")
            path = f.name
        try:
            with patch.object(usage_stats, "datetime", _frozen_datetime()):
                today = usage_stats.fetch_usage(
                    {"usage_log": {"path": path}}, "today")
                seven_days = usage_stats.fetch_usage(
                    {"usage_log": {"path": path}}, "7d")
                all_time = usage_stats.fetch_usage(
                    {"usage_log": {"path": path}}, "all")
        finally:
            os.unlink(path)

        self.assertEqual(today["total"]["calls"], 2)
        self.assertEqual([d["date"] for d in today["daily"]], ["2026-08-19"])
        self.assertEqual(seven_days["total"]["calls"], 3)
        self.assertEqual(all_time["total"]["calls"], 5)

    def test_month_range_uses_cst_calendar_month(self):
        timestamps = [
            "2026-08-01T00:00:00+08:00",  # month lower boundary, included
            "2026-08-19T12:00:00+08:00",  # today
            "2026-07-31T23:59:59+08:00",  # last month, excluded
            "2026-08-31T00:00:00+08:00",  # future day, same month — excluded
        ]
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            for ts in timestamps:
                f.write(json.dumps(_usage_record(ts=ts)) + "\n")
            path = f.name
        try:
            with patch.object(usage_stats, "datetime", _frozen_datetime()):
                month = usage_stats.fetch_usage(
                    {"usage_log": {"path": path}}, "month")
        finally:
            os.unlink(path)

        self.assertEqual(month["total"]["calls"], 2)
        self.assertEqual([d["date"] for d in month["daily"]],
                         ["2026-08-01", "2026-08-19"])

    def test_corrupt_and_non_object_lines_are_skipped(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("not-json\n")
            f.write(json.dumps(["not", "a", "usage entry"]) + "\n")
            f.write(json.dumps({
                "provider": "bad", "input_tokens": "unknown",
                "output_tokens": 1, "status": "broken",
            }) + "\n")
            f.write(json.dumps(_usage_record(
                scenario="rule", input_tokens=1, output_tokens=2,
                latency_ms=3)) + "\n")
            path = f.name
        try:
            result = usage_stats.fetch_usage({"usage_log": {"path": path}})
        finally:
            os.unlink(path)
        self.assertEqual(result["total"]["calls"], 1)

    def test_invalid_utf8_line_is_skipped(self):
        valid = (json.dumps(_usage_record(input_tokens=1)) + "\n").encode()
        with tempfile.NamedTemporaryFile("wb", suffix=".jsonl", delete=False) as f:
            f.write(b"\xff\xfe\n")
            f.write(valid)
            path = f.name
        try:
            result = usage_stats.fetch_usage(
                {"usage_log": {"path": path}}, "all")
        finally:
            os.unlink(path)
        self.assertEqual(result["total"]["calls"], 1)

    def test_incomplete_object_and_invalid_timestamp_are_skipped(self):
        valid = {
            "ts": "2026-08-19T12:00:00+08:00", "provider": "p",
            "scenario": "rule", "input_tokens": 1, "output_tokens": 2,
            "cache_read_tokens": 0, "cache_creation_tokens": 0,
            "status": 200, "latency_ms": 3,
        }
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write("{}\n")
            f.write(json.dumps({**valid, "ts": "not-a-date"}) + "\n")
            f.write(json.dumps(valid) + "\n")
            path = f.name
        try:
            result = usage_stats.fetch_usage(
                {"usage_log": {"path": path}}, "all")
        finally:
            os.unlink(path)
        self.assertEqual(result["total"]["calls"], 1)
        self.assertEqual(set(result["providers"]), {"p"})
        self.assertEqual([day["date"] for day in result["daily"]],
                         ["2026-08-19"])

    def test_arbitrarily_large_integer_fields_do_not_crash(self):
        huge = 10 ** 400
        entry = {
            "ts": "2026-08-19T12:00:00+08:00", "provider": "p",
            "scenario": "default", "input_tokens": huge,
            "output_tokens": 0, "cache_read_tokens": 0,
            "cache_creation_tokens": 0, "status": 200, "latency_ms": huge,
        }
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps(entry) + "\n")
            path = f.name
        try:
            result = usage_stats.fetch_usage(
                {"usage_log": {"path": path}}, "all")
        finally:
            os.unlink(path)
        self.assertEqual(result["total"]["calls"], 1)
        self.assertEqual(result["total"]["input_tokens"], huge)
        self.assertEqual(result["total"]["avg_latency_ms"], huge)

    def test_empty_log_returns_complete_empty_shape(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            result = usage_stats.fetch_usage({"usage_log": {"path": path}})
        finally:
            os.unlink(path)
        self.assertEqual(result["total"]["calls"], 0)
        self.assertIsNone(result["total"]["cache_hit_rate"])
        self.assertEqual(result["providers"], {})
        self.assertEqual(result["daily"], [])
        self.assertEqual(result["scenarios"], {})

    def test_rotated_log_is_not_included(self):
        entry = json.dumps(_usage_record(input_tokens=1, output_tokens=1)) + "\n"
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "usage.jsonl")
            with open(path, "w") as current:
                current.write(entry)
            with open(path + ".1", "w") as rotated:
                rotated.write(entry * 5)
            result = usage_stats.fetch_usage({"usage_log": {"path": path}})
        self.assertEqual(result["total"]["calls"], 1)


if __name__ == "__main__":
    unittest.main()
