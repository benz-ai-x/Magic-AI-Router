"""Tests for balance_usage.py — 余额/配额归一与 fetch_balance（R5 三刀切
后的单一职责半边；探测用例在 test_provider_probe.py、本地用量聚合在
test_usage_stats.py）。

Migrated from test_config_server.TestNormalizeBalance (the functions moved to
balance_usage.py), plus tests that exercise fetch_balance directly — now
possible because it takes a config dict instead of reading ~/.suanpan.yaml
internally.
"""
import json
import os
import unittest
import urllib.error
from unittest.mock import patch

from services import balance_usage
from services.authenticated_http import AuthenticatedHttpClient


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


class TestNormalizeBalanceNamedParser(unittest.TestCase):
    """C3：注册表 API 卡按名路由响应语法——形状嗅探只作兜底。"""

    def test_named_parser_equivalent_to_sniff(self):
        raw = {"balance_infos": [{"total_balance": "39.98",
                                  "topped_up_balance": "40.00",
                                  "currency": "CNY"}]}
        self.assertEqual(
            balance_usage.normalize_balance(raw, "余额", "deepseek_balance"),
            balance_usage.normalize_balance(raw, "余额"))

    def test_named_parser_kimi(self):
        raw = {"usage": {"used": 5, "limit": 10, "resetTime": None},
                "limits": [], "user": {"membership": {"level": ""}}}
        result = balance_usage.normalize_balance(raw, "Coding Plan",
                                                 "kimi_usage")
        self.assertEqual(result["primary"], "套餐")
        self.assertEqual(result["pct"], 50)

    def test_mismatched_shape_falls_back_to_sniff(self):
        # 卡上声明的语法与实际形状不符（接口变更期）→ 嗅探兜底，
        # 行为不比无卡名差
        raw = {"balance_infos": [{"total_balance": "1.00",
                                  "topped_up_balance": "1.00",
                                  "currency": "CNY"}]}
        result = balance_usage.normalize_balance(raw, "x", "glm_account")
        self.assertEqual(
            result, balance_usage.normalize_balance(raw, "x"))

    def test_unknown_parser_name_sniffs(self):
        raw = {"data": {"balance": 3.5, "totalSpendAmount": 1.0}}
        self.assertEqual(
            balance_usage.normalize_balance(raw, "x", "no_such_parser"),
            balance_usage.normalize_balance(raw, "x"))

    def test_registry_cards_carry_parser_names(self):
        # 注册表承诺的机器检查：每张 balance_apis 卡（及 model_usage_url）
        # 都带语法名——无裸 3 元组漏挂
        from shared.provider_auth import PROVIDER_REGISTRY
        for entry in PROVIDER_REGISTRY.values():
            for card in entry["balance_apis"]:
                self.assertEqual(len(card), 4, card)
                self.assertIn(card[3], balance_usage._BALANCE_PARSERS, card)
            mu = entry.get("model_usage_url")
            if mu:
                self.assertEqual(len(mu), 2, mu)
                self.assertIn(mu[1], balance_usage._BALANCE_PARSERS, mu)


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


class TestBalanceErrorShaping(unittest.TestCase):
    """#53：余额 API 失败不再只剩异常类型名——按 reason 分类出可行动
    中文（防泄漏纪律不变：不含凭证的 errno/reason 才进消息）。"""

    def test_urlerror_reason_categorized(self):
        import urllib.error
        from services import balance_usage as bu
        def fake_open_json(url, headers=None, **kw):
            raise urllib.error.URLError(
                __import__("socket").timeout("timed out"))

        with patch.object(bu._BALANCE_CLIENT, "open_json",
                                        side_effect=fake_open_json):
            results = bu.fetch_balance({"providers": {
                "p": {"base_url": "https://api.deepseek.com",
                      "api_key": "k"}}})
        err = results[0]["apis"][0]["error"]
        self.assertIn("超时", err)

    def test_conn_refused_categorized(self):
        import urllib.error
        from services import balance_usage as bu

        def fake_open_json(url, headers=None, **kw):
            raise urllib.error.URLError(
                ConnectionRefusedError(61, "Connection refused"))

        with patch.object(bu._BALANCE_CLIENT, "open_json",
                                        side_effect=fake_open_json):
            results = bu.fetch_balance({"providers": {
                "p": {"base_url": "https://api.deepseek.com",
                      "api_key": "k"}}})
        err = results[0]["apis"][0]["error"]
        self.assertIn("拒绝", err)


class TestAllApiQuotaDisplay(unittest.TestCase):
    """#调研落地：「全读 API，没有不显示」——GLM 月度换 model-usage 官方
    统计（本月窗口）；Kimi Advanced（totalQuota={}）月度行消失；本地
    聚合回退整体删除。"""

    GLM_QUOTA_URL = "https://open.bigmodel.cn/api/monitor/usage/quota/limit"
    GLM_ACCOUNT_URL = ("https://www.bigmodel.cn/api/biz/account/"
                       "query-customer-account-report")
    GLM_MODEL_USAGE = ("https://open.bigmodel.cn/api/monitor/usage/model-usage")
    KIMI_URL = "https://api.kimi.com/coding/v1/usages"

    def _run(self, by_url, sp):
        def side_effect(url, headers=None, data=None, method=None,
                        timeout=None):
            for key, payload in by_url.items():
                if url.startswith(key):
                    return json.dumps(payload).encode()
            raise AssertionError(f"unmocked URL: {url}")
        with patch.object(AuthenticatedHttpClient, "open",
                          side_effect=side_effect):
            return balance_usage.fetch_balance(sp)

    GLM_PRO_QUOTA = {"data": {"level": "pro", "limits": [
        {"type": "CREDIT_LIMIT", "unit": 3, "percentage": 1,
         "usage": 12000, "currentValue": 12},
        {"type": "CREDIT_LIMIT", "unit": 6, "percentage": 5,
         "usage": 60000, "currentValue": 3322}]}}
    GLM_MODEL_USAGE_RESP = {"data": {"totalUsage": {
        "totalModelCallCount": 1920, "totalTokensUsage": 420500000}}}

    def test_glm_monthly_from_model_usage_api(self):
        """GLM 月度 = model-usage 本月窗口官方统计（非本地聚合）。"""
        sp = {"providers": {"glm": {
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "k"}}}
        result = self._run({
            self.GLM_QUOTA_URL: self.GLM_PRO_QUOTA,
            self.GLM_ACCOUNT_URL: {"data": {"balance": 1.0,
                                            "totalSpendAmount": 9.0}},
            self.GLM_MODEL_USAGE: self.GLM_MODEL_USAGE_RESP,
        }, sp)
        apis = result[0]["apis"]
        all_q = [q for a in apis for q in a.get("quotas", [])]
        self.assertIn("每月", [q["period"] for q in all_q])
        m = next(q for q in all_q if q["period"] == "每月")
        self.assertEqual(m["used"], 420500000)   # 官方 totalTokensUsage
        self.assertEqual(m["calls"], 1920)        # totalModelCallCount
        self.assertEqual(m.get("source"), "api")
        self.assertNotIn("_sort", m)

    def test_glm_monthly_no_local_fallback(self):
        """GLM 月度不再落本地 usage.jsonl 聚合行（source=local 整体删除）。"""
        sp = {"usage_log": {"path": "/tmp/nonexistent-usage.jsonl"},
              "providers": {"glm": {
                  "base_url": "https://open.bigmodel.cn/api/paas/v4",
                  "api_key": "k"}}}
        result = self._run({
            self.GLM_QUOTA_URL: self.GLM_PRO_QUOTA,
            self.GLM_ACCOUNT_URL: {"data": {"balance": 1.0,
                                            "totalSpendAmount": 9.0}},
            self.GLM_MODEL_USAGE: self.GLM_MODEL_USAGE_RESP,
        }, sp)
        for api in result[0]["apis"]:
            for q in api.get("quotas", []):
                self.assertNotEqual(q.get("source"), "local")


    def test_glm_model_usage_failure_shows_error_keeps_quota(self):
        """model-usage 失败时本月用量块报可行动错误，配额行不受影响。"""
        sp = {"providers": {"glm": {
            "base_url": "https://open.bigmodel.cn/api/paas/v4",
            "api_key": "k"}}}

        def side_effect(url, headers=None, **kw):
            if "model-usage" in url:
                import urllib.error
                raise urllib.error.URLError(
                    ConnectionRefusedError(61, "refused"))
            for key, payload in {
                self.GLM_QUOTA_URL: self.GLM_PRO_QUOTA,
                self.GLM_ACCOUNT_URL: {"data": {"balance": 1.0,
                                                "totalSpendAmount": 9.0}},
            }.items():
                if url.startswith(key):
                    return json.dumps(payload).encode()
            raise AssertionError(f"unmocked URL: {url}")

        with patch.object(AuthenticatedHttpClient, "open",
                          side_effect=side_effect):
            result = balance_usage.fetch_balance(sp)
        apis = result[0]["apis"]
        monthly = next(a for a in apis if a.get("label") == "本月用量")
        self.assertIn("拒绝", monthly["error"])
        plan = apis[0]
        self.assertEqual([q["period"] for q in plan["quotas"]],
                         ["5小时", "每周"])

    def test_kimi_advanced_no_monthly_row(self):
        """Kimi Advanced（totalQuota={}）月度行不显示——API 没有就不实现。"""
        sp = {"providers": {"kimi": {
            "base_url": "https://api.kimi.com/anthropic", "api_key": "k"}}}
        payload = {"usage": {"limit": "100", "used": "42",
                             "resetTime": "2026-08-16T03:00:46Z"},
                   "limits": [{"window": {"duration": 300,
                                          "timeUnit": "TIME_UNIT_MINUTE"},
                               "detail": {"used": 0, "limit": 100,
                                          "resetTime": "2026-08-23T21:00:00Z"}}],
                   "user": {"membership": {"level": "LEVEL_ADVANCED"}},
                   "totalQuota": {}}
        result = self._run({self.KIMI_URL: payload}, sp)
        qs = result[0]["apis"][0]["quotas"]
        self.assertNotIn("每月", [q["period"] for q in qs])
        self.assertEqual([q["period"] for q in qs], ["5小时", "每周"])


if __name__ == "__main__":
    unittest.main()
