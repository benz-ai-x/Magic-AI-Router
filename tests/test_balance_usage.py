"""Tests for balance_usage.py — balance normalization + usage aggregation.

Migrated from test_config_server.TestNormalizeBalance (the functions moved to
balance_usage.py), plus new tests that exercise fetch_balance / fetch_usage
directly — now possible because they take a config dict instead of reading
~/.suanpan.yaml internally.
"""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from datetime import datetime
from unittest.mock import patch

from services import balance_usage
from services.authenticated_http import AuthenticatedHttpClient
def _sp(providers):
    return {"providers": providers}


def _models_payload(*ids):
    return json.dumps({"data": [{"id": i} for i in ids]}).encode()


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


class _FakeResp:
    """Context-manager response double for urllib.request.urlopen."""

    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b""))


def _frozen_datetime():
    """datetime double pinned at 2026-08-19 12:00 CST (matches _usage_record)."""
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = cls(2026, 8, 19, 12, 0, tzinfo=balance_usage.CST)
            return value.astimezone(tz) if tz else value.replace(tzinfo=None)
    return FrozenDateTime


class TestFetchModels(unittest.TestCase):
    """fetch_models(sp_raw, name) → {"models": [...]} | {"error": ...}."""

    PROVIDERS = {
        "deepseek": {
            "base_url": "https://api.deepseek.com/anthropic",
            "api_key": "sk-test",
        },
    }

    def _fetch(self, sp=None, name="deepseek"):
        return balance_usage.fetch_models(sp if sp is not None else _sp(self.PROVIDERS), name)

    def test_unknown_provider_error(self):
        r = self._fetch(name="ghost")
        self.assertIn("error", r)

    def test_missing_key_error(self):
        sp = _sp({"deepseek": {"base_url": "https://api.deepseek.com"}})
        r = self._fetch(sp)
        self.assertIn("error", r)

    def test_parses_data_ids_deduped_in_order(self):
        with patch.object(AuthenticatedHttpClient, "open", return_value=_models_payload("a", "b", "a")) as m:
            r = self._fetch()
        self.assertEqual(r, {"models": ["a", "b"]})
        self.assertEqual(m.call_args[0][0], "https://api.deepseek.com/anthropic/v1/models")

    def test_404_falls_back_to_models_without_v1(self):
        def side_effect(url, headers=None, data=None, method=None, timeout=None):
            if url.endswith("/v1/models"):
                raise _http_error(404)
            return _models_payload("m1")

        with patch.object(AuthenticatedHttpClient, "open", side_effect=side_effect) as m:
            r = self._fetch()
        self.assertEqual(r, {"models": ["m1"]})
        self.assertEqual(m.call_args_list[-1][0][0], "https://api.deepseek.com/anthropic/models")

    def test_404_falls_back_to_models_without_v1_second(self):
        def side_effect(url, headers=None, data=None, method=None, timeout=None):
            if url.endswith("/v1/models"):
                raise _http_error(404)
            return _models_payload("m1")

        with patch.object(AuthenticatedHttpClient, "open", side_effect=side_effect) as m:
            r = self._fetch()
        self.assertEqual(r, {"models": ["m1"]})
        self.assertEqual(m.call_args_list[-1][0][0], "https://api.deepseek.com/anthropic/models")

    def test_404_falls_back_to_origin_root(self):
        """Providers whose base_url has a path prefix (e.g. /anthropic) may only
        serve /models at the origin root."""
        def side_effect(url, headers=None, data=None, method=None, timeout=None):
            if "/anthropic/" in url:
                raise _http_error(404)
            return _models_payload("deepseek-chat")

        with patch.object(AuthenticatedHttpClient, "open", side_effect=side_effect) as m:
            r = self._fetch()
        self.assertEqual(r, {"models": ["deepseek-chat"]})
        self.assertEqual(m.call_args_list[-1][0][0], "https://api.deepseek.com/v1/models")

    def test_all_candidates_404_returns_error(self):
        with patch.object(AuthenticatedHttpClient, "open", side_effect=_http_error(404)):
            r = self._fetch()
        self.assertIn("error", r)

    def test_x_api_key_auth_header(self):
        sp = _sp({"deepseek": {**self.PROVIDERS["deepseek"], "auth_header": "x-api-key"}})
        with patch.object(AuthenticatedHttpClient, "open", return_value=_models_payload("a")) as m:
            self._fetch(sp)
        headers = m.call_args.kwargs["headers"]
        # 直传 dict 键为字面小写（urllib 规范化大小写已成历史）
        self.assertEqual(headers.get("x-api-key"), "sk-test")
        self.assertIn("anthropic-version", headers)

    def test_bearer_auth_header_by_default(self):
        with patch.object(AuthenticatedHttpClient, "open", return_value=_models_payload("a")) as m:
            self._fetch()
        self.assertEqual(m.call_args.kwargs["headers"].get("Authorization"), "Bearer sk-test")

    def test_non_404_http_error(self):
        with patch.object(AuthenticatedHttpClient, "open", side_effect=_http_error(401)):
            r = self._fetch()
        self.assertIn("error", r)

    def test_network_error(self):
        with patch.object(AuthenticatedHttpClient, "open", side_effect=urllib.error.URLError("boom")):
            r = self._fetch()
        self.assertIn("error", r)

    def test_malformed_response_error(self):
        with patch.object(AuthenticatedHttpClient, "open", return_value=b'{"nope": 1}'):
            r = self._fetch()
        self.assertIn("error", r)


class TestNormalizeBalance(unittest.TestCase):
    # ── simple balance providers (no quotas) ──
    def test_deepseek_format(self):
        raw = {"balance_infos": [{"total_balance": "39.98", "topped_up_balance": "40.00", "currency": "CNY"}]}
        result = balance_usage.normalize_balance(raw, "余额")
        self.assertIn("¥39.98", result["primary"])
        self.assertIn("充值", result["secondary"])
        self.assertNotIn("quotas", result)

    def test_glm_account_balance(self):
        raw = {"data": {"balance": 0.05, "totalSpendAmount": 199.95}}
        result = balance_usage.normalize_balance(raw, "账户余额")
        self.assertIn("¥0.05", result["primary"])
        self.assertIn("199.95", result["secondary"])
        self.assertNotIn("quotas", result)

    def test_unknown_format(self):
        raw = {"unknown": "data"}
        result = balance_usage.normalize_balance(raw, "test")
        self.assertEqual(result["primary"], "—")
        self.assertNotIn("quotas", result)

    def test_unknown_format_redacts_raw_values(self):
        """Fallback must never echo raw provider response values into the UI
        (a provider could reflect the API key back).  Key names survive."""
        raw = {"error": {"code": 401, "api_key": "sk-leak-me"},
               "message": "unauthorized sk-leak-me"}
        result = balance_usage.normalize_balance(raw, "test")
        rendered = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("sk-leak-me", rendered)
        self.assertNotIn("unauthorized", rendered)
        self.assertIn("error", rendered)  # structure hint stays for debugging
        self.assertIn("message", rendered)

    def test_deepseek_has_no_pct(self):
        raw = {"balance_infos": [{"total_balance": "39.98", "topped_up_balance": "40.00", "currency": "CNY"}]}
        self.assertIsNone(balance_usage.normalize_balance(raw, "余额").get("pct"))

    # ── GLM Coding Plan (quotas from limits[]) ──
    def test_glm_coding_plan(self):
        raw = {"data": {"level": "pro", "limits": [{"unit": 5, "percentage": 85}]}}
        result = balance_usage.normalize_balance(raw, "Coding Plan")
        self.assertEqual(result["primary"], "PRO")
        self.assertEqual(result["label"], "Coding Plan")
        self.assertEqual(result["pct"], 85)
        qs = result["quotas"]
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0]["period"], "每月")
        self.assertEqual(qs[0]["pct"], 85)

    def test_glm_coding_plan_includes_current_detail(self):
        raw = {"data": {"level": "pro", "limits": [
            {"unit": 5, "percentage": 85, "currentValue": 1700, "usage": 2000}]}}
        result = balance_usage.normalize_balance(raw, "Coding Plan")
        q = result["quotas"][0]
        self.assertEqual(q["period"], "每月")
        self.assertEqual(q["used"], 1700)
        self.assertEqual(q["limit"], 2000)
        self.assertIsNone(q["reset"])

    def test_glm_coding_plan_pct_from_limits(self):
        # pct = 各窗口 percentage 的最大值（最紧的那个窗口决定颜色）
        raw = {"data": {"level": "pro", "limits": [
            {"unit": 5, "percentage": 3},
            {"unit": 3, "percentage": 86}]}}
        result = balance_usage.normalize_balance(raw, "Coding Plan")
        self.assertEqual(result["pct"], 86)

    def test_glm_coding_plan_sorts_by_duration(self):
        """Quotas sorted ascending by time-window length: 5小时 < 每周 < 每月."""
        raw = {"data": {"level": "pro", "limits": [
            {"unit": 5, "percentage": 50},   # 每月
            {"unit": 3, "percentage": 20},   # 5小时
            {"unit": 6, "percentage": 30},   # 每周
        ]}}
        result = balance_usage.normalize_balance(raw, "Coding Plan")
        periods = [q["period"] for q in result["quotas"]]
        self.assertEqual(periods, ["5小时", "每周", "每月"])

    def test_glm_time_limit_entries_labeled_tool_quota(self):
        """TIME_LIMIT entries are 工具时长 quotas (usageDetails: search-prime /
        web-reader / zread), not token usage — the period says so."""
        raw = {"data": {"level": "max", "limits": [
            {"type": "TIME_LIMIT", "unit": 5, "percentage": 0,
             "usage": 4000, "currentValue": 7},
            {"type": "TOKENS_LIMIT", "unit": 3, "percentage": 1},
        ]}}
        result = balance_usage.normalize_balance(raw, "Coding Plan")
        periods = [q["period"] for q in result["quotas"]]
        self.assertEqual(periods, ["5小时", "每月·工具"])

    def test_glm_coding_plan_no_detail_fields(self):
        """When a GLM limit has only percentage (no currentValue/usage),
        used/limit are None."""
        raw = {"data": {"level": "pro", "limits": [{"unit": 5, "percentage": 85}]}}
        result = balance_usage.normalize_balance(raw, "Coding Plan")
        q = result["quotas"][0]
        self.assertIsNone(q["used"])
        self.assertIsNone(q["limit"])

    def test_glm_coding_plan_reset_from_next_reset_time(self):
        """GLM limits[] carry nextResetTime (epoch ms) — surfaced as reset
        (CST). Entries without it keep reset=None."""
        raw = {"data": {"level": "pro", "limits": [
            {"unit": 3, "percentage": 1, "usage": 12000, "currentValue": 12,
             "nextResetTime": 1788192000000},  # 2026-09-01 00:00 CST
            {"unit": 6, "percentage": 5, "usage": 60000, "currentValue": 3322},
        ]}}
        result = balance_usage.normalize_balance(raw, "Coding Plan")
        qs = result["quotas"]
        self.assertEqual(qs[0]["reset"], "9月1日 00:00")
        self.assertIsNone(qs[1]["reset"])

    def test_reset_times_render_in_cst(self):
        """Provider reset timestamps are UTC — display converts to CST."""
        raw = {"usage": {"limit": "100", "used": "42",
                         "resetTime": "2026-08-16T03:00:46Z"},
               "user": {"membership": {"level": "LEVEL_PRO"}}}
        result = balance_usage.normalize_balance(raw, "Coding Plan")
        self.assertEqual(result["quotas"][0]["reset"], "8月16日 11:00")

    # ── KIMI Coding Plan (quotas: 5h window + weekly + optional monthly) ──
    # Windows sort ascending by duration: 5小时 < 每周 < 每月 (same as GLM).
    def test_kimi_format(self):
        raw = {"usage": {"limit": "100", "used": "42"},
               "user": {"membership": {"level": "LEVEL_PRO"}}}
        result = balance_usage.normalize_balance(raw, "Coding Plan")
        self.assertEqual(result["primary"], "Pro")
        qs = result["quotas"]
        self.assertEqual(len(qs), 1)
        self.assertEqual(qs[0]["period"], "每周")
        self.assertEqual(qs[0]["used"], 42)
        self.assertEqual(qs[0]["limit"], 100)
        self.assertEqual(qs[0]["pct"], 42)
        self.assertIsNone(qs[0]["reset"])

    def test_kimi_includes_reset_time(self):
        raw = {"usage": {"limit": "100", "used": "42", "resetTime": "2026-09-01T00:00:00Z"},
               "user": {"membership": {"level": "LEVEL_PRO"}}}
        result = balance_usage.normalize_balance(raw, "Coding Plan")
        self.assertEqual(result["quotas"][0]["period"], "每周")
        self.assertIn("9月1日", result["quotas"][0]["reset"])

    def test_kimi_limits_window_shows_5h_quota(self):
        # Real API response shape: usage=周额度, limits[]=windowed quotas
        raw = {
            "user": {"membership": {"level": "LEVEL_ADVANCED"}},
            "usage": {"limit": "100", "used": "37", "resetTime": "2026-08-16T03:00:46Z"},
            "limits": [{
                "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                "detail": {"limit": "100", "used": "33",
                           "resetTime": "2026-08-10T13:00:46Z"},
            }],
        }
        result = balance_usage.normalize_balance(raw, "Coding Plan")
        qs = result["quotas"]
        self.assertEqual(len(qs), 2)
        # ascending by duration: 5小时 first, then 每周
        self.assertEqual(qs[0]["period"], "5小时")
        self.assertEqual(qs[0]["used"], 33)
        self.assertEqual(qs[0]["limit"], 100)
        self.assertEqual(qs[0]["pct"], 33)
        self.assertIn("8月10日", qs[0]["reset"])
        self.assertEqual(qs[1]["period"], "每周")
        self.assertEqual(qs[1]["used"], 37)
        self.assertEqual(qs[1]["limit"], 100)
        self.assertEqual(qs[1]["pct"], 37)
        self.assertIn("8月16日", qs[1]["reset"])
        self.assertEqual(result["pct"], 37)  # max across all windows

    def test_kimi_total_quota_shows_monthly(self):
        """totalQuota is the monthly membership pool (plan-dependent); when
        populated it becomes the 每月 row, sorted after 每周."""
        raw = {
            "user": {"membership": {"level": "LEVEL_ALLEGRO"}},
            "usage": {"limit": "100", "used": "20",
                      "resetTime": "2026-08-16T03:00:46Z"},
            "totalQuota": {"limit": "500", "used": "130",
                           "resetTime": "2026-09-01T00:00:00Z"},
        }
        result = balance_usage.normalize_balance(raw, "Coding Plan")
        qs = result["quotas"]
        self.assertEqual([q["period"] for q in qs], ["每周", "每月"])
        self.assertEqual(result["primary"], "Allegro")
        m = qs[1]
        self.assertEqual(m["used"], 130)
        self.assertEqual(m["limit"], 500)
        self.assertEqual(m["pct"], 26)
        self.assertIn("9月1日", m["reset"])
        self.assertEqual(result["pct"], 26)  # max(20, 26)

    def test_kimi_empty_total_quota_no_monthly(self):
        """Our Advanced account returns "totalQuota": {} — no monthly row."""
        raw = {"usage": {"limit": "100", "used": "42"},
               "user": {"membership": {"level": "LEVEL_PRO"}},
               "totalQuota": {}}
        result = balance_usage.normalize_balance(raw, "Coding Plan")
        self.assertEqual([q["period"] for q in result["quotas"]], ["每周"])

    def test_kimi_pct_field_drives_color(self):
        raw = {"usage": {"limit": "100", "used": "85"},
               "user": {"membership": {"level": "LEVEL_PRO"}}}
        self.assertEqual(balance_usage.normalize_balance(raw, "x")["pct"], 85)

    def test_kimi_pct_max_across_windows(self):
        """pct = max percentage across main + windowed quotas."""
        raw = {
            "usage": {"limit": "100", "used": "20"},
            "user": {"membership": {"level": "LEVEL_PRO"}},
            "limits": [{
                "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                "detail": {"limit": "100", "used": "90"},
            }],
        }
        result = balance_usage.normalize_balance(raw, "Coding Plan")
        self.assertEqual(result["pct"], 90)  # 90 > 20


class TestFetchBalance(unittest.TestCase):
    def test_disabled_provider_skipped(self):
        sp = {"providers": {"x": {"enabled": False}}}
        self.assertEqual(balance_usage.fetch_balance(sp),
                         [{"provider": "x", "enabled": False}])

    def test_unsupported_provider_marked(self):
        sp = {"providers": {"x": {"base_url": "https://unknown.example.com"}}}
        result = balance_usage.fetch_balance(sp)
        self.assertFalse(result[0]["supported"])

    def test_supported_provider_without_key(self):
        sp = {"providers": {"x": {"base_url": "https://api.deepseek.com"}}}
        result = balance_usage.fetch_balance(sp)
        self.assertEqual(result[0]["error"], "未配置 API Key")


class TestFetchBalanceLocalMonthly(unittest.TestCase):
    """Plan providers whose API reports no monthly token window get a local
    fallback row aggregated from the gateway usage log (source="local";
    zero-filled when the log has nothing — 三行永远齐). An API-reported
    monthly token pool (Kimi totalQuota) wins; GLM's 每月·工具 (TIME_LIMIT)
    is a different unit and does not suppress the local token row."""

    GLM_QUOTA_URL = "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
    GLM_ACCOUNT_URL = ("https://www.bigmodel.cn/api/biz/account/"
                       "query-customer-account-report")
    KIMI_URL = "https://api.kimi.com/coding/v1/usages"

    GLM_PRO_QUOTA = {"data": {"level": "pro", "limits": [
        {"type": "CREDIT_LIMIT", "unit": 3, "percentage": 1,
         "usage": 12000, "currentValue": 12},
        {"type": "CREDIT_LIMIT", "unit": 6, "percentage": 5,
         "usage": 60000, "currentValue": 3322}]}}
    GLM_MAX_QUOTA = {"data": {"level": "max", "limits": [
        {"type": "TOKENS_LIMIT", "unit": 3, "percentage": 0},
        {"type": "TOKENS_LIMIT", "unit": 6, "percentage": 0},
        {"type": "TIME_LIMIT", "unit": 5, "percentage": 0,
         "usage": 4000, "currentValue": 7}]}}
    GLM_ACCOUNT = {"data": {"balance": 1.0, "totalSpendAmount": 9.0}}
    KIMI_PAYLOAD = {
        "usage": {"limit": "100", "used": "46"},
        "user": {"membership": {"level": "LEVEL_ADVANCED"}},
        "limits": [{"window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                    "detail": {"limit": "100", "used": "1"}}],
        "totalQuota": {},
    }

    def _run(self, by_url, sp):
        def side_effect(url, headers=None, data=None, method=None,
                        timeout=None):
            return json.dumps(by_url[url]).encode()

        with patch.object(AuthenticatedHttpClient, "open",
                          side_effect=side_effect), \
                patch.object(balance_usage, "datetime", _frozen_datetime()):
            return balance_usage.fetch_balance(sp)

    def _write_log(self, entries):
        f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        for e in entries:
            f.write(json.dumps(e) + "\n")
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def _glm_sp(self, log_path):
        return {
            "usage_log": {"path": log_path},
            "providers": {"glm": {
                "base_url": "https://open.bigmodel.cn/api/paas/v4",
                "api_key": "k"}},
        }

    def test_pro_without_api_monthly_gets_local_row(self):
        log = self._write_log([
            _usage_record(provider="glm", input_tokens=10, output_tokens=5,
                          cache_read_tokens=90),
            _usage_record(provider="glm", input_tokens=20, output_tokens=15,
                          cache_read_tokens=30, cache_creation_tokens=50),
        ])
        result = self._run(
            {self.GLM_QUOTA_URL: self.GLM_PRO_QUOTA,
             self.GLM_ACCOUNT_URL: self.GLM_ACCOUNT}, self._glm_sp(log))
        apis = result[0]["apis"]
        qs = apis[0]["quotas"]
        self.assertEqual([q["period"] for q in qs], ["5小时", "每周", "每月"])
        m = qs[2]
        self.assertEqual(m["used"], 220)  # 10+5+90 + 20+15+30+50
        self.assertEqual(m["calls"], 2)
        self.assertIsNone(m["limit"])
        self.assertIsNone(m["pct"])
        self.assertEqual(m["source"], "local")
        # 账户余额 API has no quotas — untouched
        self.assertNotIn("quotas", apis[1])

    def test_api_monthly_wins_over_local(self):
        """Kimi-style totalQuota (period exactly 每月) suppresses the local row."""
        log = self._write_log([_usage_record(provider="kimi", input_tokens=9)])
        sp = {
            "usage_log": {"path": log},
            "providers": {"kimi": {
                "base_url": "https://api.kimi.com/anthropic", "api_key": "k"}},
        }
        payload = dict(self.KIMI_PAYLOAD,
                       totalQuota={"limit": "500", "used": "130"})
        result = self._run({self.KIMI_URL: payload}, sp)
        qs = result[0]["apis"][0]["quotas"]
        self.assertEqual([q["period"] for q in qs], ["5小时", "每周", "每月"])
        m = qs[2]
        self.assertEqual(m["used"], 130)   # API 口径，不是本地聚合
        self.assertEqual(m["limit"], 500)
        self.assertNotIn("source", m)

    def test_glm_max_tool_time_monthly_does_not_suppress_local_tokens(self):
        """GLM Max unit 5 is TIME_LIMIT (工具时长), not token usage: it is
        labeled 每月·工具 and the local token row is still appended."""
        log = self._write_log([_usage_record(provider="glm", input_tokens=9,
                                             output_tokens=1)])
        result = self._run(
            {self.GLM_QUOTA_URL: self.GLM_MAX_QUOTA,
             self.GLM_ACCOUNT_URL: self.GLM_ACCOUNT}, self._glm_sp(log))
        qs = result[0]["apis"][0]["quotas"]
        self.assertEqual([q["period"] for q in qs],
                         ["5小时", "每周", "每月·工具", "每月"])
        tool = qs[2]
        self.assertEqual(tool["used"], 7)
        self.assertEqual(tool["limit"], 4000)
        self.assertNotIn("source", tool)
        local = qs[3]
        self.assertEqual(local["source"], "local")
        self.assertEqual(local["used"], 10)

    def test_kimi_empty_total_quota_gets_local_row(self):
        log = self._write_log([_usage_record(
            provider="kimi", input_tokens=100, output_tokens=50)])
        sp = {
            "usage_log": {"path": log},
            "providers": {"kimi": {
                "base_url": "https://api.kimi.com/anthropic", "api_key": "k"}},
        }
        result = self._run({self.KIMI_URL: self.KIMI_PAYLOAD}, sp)
        qs = result[0]["apis"][0]["quotas"]
        self.assertEqual([q["period"] for q in qs], ["5小时", "每周", "每月"])
        self.assertEqual(qs[2]["source"], "local")
        self.assertEqual(qs[2]["used"], 150)

    def test_missing_log_still_shows_zero_local_row(self):
        """「三行永远齐」：本地无数据时显示 0 行而非消失。"""
        result = self._run(
            {self.GLM_QUOTA_URL: self.GLM_PRO_QUOTA,
             self.GLM_ACCOUNT_URL: self.GLM_ACCOUNT},
            self._glm_sp("/nonexistent/usage.jsonl"))
        qs = result[0]["apis"][0]["quotas"]
        self.assertEqual([q["period"] for q in qs], ["5小时", "每周", "每月"])
        m = qs[2]
        self.assertEqual(m["source"], "local")
        self.assertEqual(m["used"], 0)
        self.assertEqual(m["calls"], 0)

    def test_no_local_entries_for_provider_still_shows_zero_row(self):
        log = self._write_log([_usage_record(provider="other", input_tokens=1)])
        result = self._run(
            {self.GLM_QUOTA_URL: self.GLM_PRO_QUOTA,
             self.GLM_ACCOUNT_URL: self.GLM_ACCOUNT}, self._glm_sp(log))
        qs = result[0]["apis"][0]["quotas"]
        self.assertEqual([q["period"] for q in qs], ["5小时", "每周", "每月"])
        self.assertEqual(qs[2]["used"], 0)
        self.assertEqual(qs[2]["source"], "local")

    def test_simple_balance_provider_untouched(self):
        log = self._write_log([
            _usage_record(provider="deepseek", input_tokens=1)])
        sp = {
            "usage_log": {"path": log},
            "providers": {"deepseek": {
                "base_url": "https://api.deepseek.com/anthropic",
                "api_key": "k"}},
        }
        result = self._run(
            {"https://api.deepseek.com/user/balance": {"balance_infos": [
                {"total_balance": "9.0", "topped_up_balance": "9.0",
                 "currency": "CNY"}]}}, sp)
        self.assertNotIn("quotas", result[0]["apis"][0])


class TestFetchUsage(unittest.TestCase):
    def test_missing_log_returns_zero(self):
        result = balance_usage.fetch_usage({"usage_log": {"path": "/nonexistent/x.jsonl"}})
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
                result = balance_usage.fetch_usage({})
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
            result = balance_usage.fetch_usage({"usage_log": {"path": path}})
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
            result = balance_usage.fetch_usage({"usage_log": {"path": path}})
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
            result = balance_usage.fetch_usage({"usage_log": {"path": path}})
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
        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 8, 19, 12, 0, tzinfo=balance_usage.CST)
                return value.astimezone(tz) if tz else value.replace(tzinfo=None)

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
            with patch.object(balance_usage, "datetime", FrozenDateTime):
                today = balance_usage.fetch_usage(
                    {"usage_log": {"path": path}}, "today")
                seven_days = balance_usage.fetch_usage(
                    {"usage_log": {"path": path}}, "7d")
                all_time = balance_usage.fetch_usage(
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
            with patch.object(balance_usage, "datetime", _frozen_datetime()):
                month = balance_usage.fetch_usage(
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
            result = balance_usage.fetch_usage({"usage_log": {"path": path}})
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
            result = balance_usage.fetch_usage(
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
            result = balance_usage.fetch_usage(
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
            result = balance_usage.fetch_usage(
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
            result = balance_usage.fetch_usage({"usage_log": {"path": path}})
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
            result = balance_usage.fetch_usage({"usage_log": {"path": path}})
        self.assertEqual(result["total"]["calls"], 1)


class TestTestProviderUnknown(unittest.TestCase):
    def test_unknown_provider_returns_error(self):
        result = balance_usage.test_provider(_sp({}), "nonexistent")
        self.assertNotIn("ok", result)
        self.assertIn("不存在", result["error"])


class TestTestProviderValidation(unittest.TestCase):
    def test_empty_base_url_returns_error(self):
        result = balance_usage.test_provider(
            _sp({"p": {"base_url": "", "api_key": "k", "models": ["m"]}}), "p")
        self.assertIn("base_url", result["error"])

    def test_no_api_key_returns_error(self):
        result = balance_usage.test_provider(
            _sp({"p": {"base_url": "https://x.com", "api_key": None, "models": ["m"]}}), "p")
        self.assertIn("API Key", result["error"])

    def test_no_models_returns_error(self):
        result = balance_usage.test_provider(
            _sp({"p": {"base_url": "https://x.com", "api_key": "k", "models": []}}), "p")
        self.assertIn("模型", result["error"])


class TestTestProviderRequest(unittest.TestCase):
    """Test the HTTP request path with mocked urllib."""
    _provider = {"base_url": "https://api.test.com", "api_key": "sk-test",
                 "auth_header": "x-api-key", "models": ["test-model"]}

    def _mock_resp(self, body_dict, status=200):
        """客户端 seam 下直接返回 body 字节。"""
        return json.dumps(body_dict).encode()

    @patch.object(AuthenticatedHttpClient, "open")
    def test_successful_request_returns_ok(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_resp({
            "model": "test-model",
            "content": [{"type": "text", "text": "Hello!"}],
        })
        result = balance_usage.test_provider(_sp({"p": self._provider}), "p")
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "test-model")
        self.assertEqual(result["reply"], "Hello!")

    @patch.object(AuthenticatedHttpClient, "open")
    def test_successful_request_empty_reply(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_resp({
            "model": "test-model", "content": [],
        })
        result = balance_usage.test_provider(_sp({"p": self._provider}), "p")
        self.assertTrue(result["ok"])
        self.assertEqual(result["reply"], "")

    @patch.object(AuthenticatedHttpClient, "open")
    def test_http_error_returns_message(self, mock_urlopen):
        err = urllib.error.HTTPError(
            "https://api.test.com/v1/messages", 400,
            "Bad Request", {},
            io.BytesIO(json.dumps({"error": {"message": "Model not exist."}}).encode()))
        mock_urlopen.side_effect = err
        result = balance_usage.test_provider(_sp({"p": self._provider}), "p")
        self.assertIn("Model not exist", result["error"])

    @patch.object(AuthenticatedHttpClient, "open")
    def test_network_error_returns_exception(self, mock_urlopen):
        mock_urlopen.side_effect = ConnectionError("timeout")
        result = balance_usage.test_provider(_sp({"p": self._provider}), "p")
        self.assertIn("ConnectionError", result["error"])

    @patch.object(AuthenticatedHttpClient, "open")
    def test_model_override(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_resp({
            "model": "custom-model",
            "content": [{"type": "text", "text": "hi"}],
        })
        result = balance_usage.test_provider(
            _sp({"p": self._provider}), "p", model="custom-model")
        self.assertTrue(result["ok"])
        # Verify the request body used the override model
        body = json.loads(mock_urlopen.call_args.kwargs["data"].decode())
        self.assertEqual(body["model"], "custom-model")

    def test_missing_api_key_returns_error(self):
        provider = {"base_url": "https://api.test.com", "enabled": True}
        result = balance_usage.test_provider(_sp({"p": provider}), "p")
        self.assertIn("API Key", result["error"])


class TestResolveProviderKey(unittest.TestCase):
    def test_none_key_falls_back_to_env(self):
        os.environ["TEST_BU_KEY"] = "sk-from-env"
        try:
            p = {"api_key": None, "api_key_env": "TEST_BU_KEY"}
            self.assertEqual(balance_usage.resolve_api_key(p), "sk-from-env")
        finally:
            del os.environ["TEST_BU_KEY"]

    def test_no_key_no_env_returns_none(self):
        self.assertIsNone(balance_usage.resolve_api_key({"api_key": None}))


class TestFmtReset(unittest.TestCase):
    def test_valid_iso_formatted(self):
        result = balance_usage._fmt_reset("2026-08-10T14:30:00Z")
        self.assertIn("8月10日", result)

    def test_invalid_iso_returns_prefix(self):
        result = balance_usage._fmt_reset("not-a-timestamp-value")
        self.assertEqual(result, "not-a-timestamp-")


if __name__ == "__main__":
    unittest.main()
