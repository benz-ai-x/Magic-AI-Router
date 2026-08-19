"""Coverage gap tests for balance_usage.py, login_item.py, util.py.

Targets the specific uncovered lines reported by ``pytest --cov`` so the three
modules reach 100 % line coverage. Uses only public APIs (or module-level
helpers) and never writes real user config (conftest sandboxes paths).
"""
import io
import json
import os
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import balance_usage
import login_item
import util


# ---------------------------------------------------------------------------
# helpers — doubles for urllib.request.urlopen
# ---------------------------------------------------------------------------
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


def _http_error(code, body=b""):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body))


# ===========================================================================
# balance_usage._window_label — branches 53, 57-62
# ===========================================================================
class TestWindowLabelBranches(unittest.TestCase):
    def test_non_int_duration_returns_none(self):
        # Line 53: ``not isinstance(dur, int)`` — string duration
        self.assertIsNone(
            balance_usage._window_label({"duration": "300", "timeUnit": "TIME_UNIT_MINUTE"}))

    def test_none_duration_returns_none(self):
        # Line 53: missing duration → None (not an int)
        self.assertIsNone(balance_usage._window_label({"timeUnit": "TIME_UNIT_MINUTE"}))

    def test_minutes_not_divisible_by_60(self):
        # Line 57: 90 minutes → "90分钟" (not whole hours)
        self.assertEqual(
            balance_usage._window_label({"duration": 90, "timeUnit": "TIME_UNIT_MINUTE"}),
            "90分钟")

    def test_time_unit_hour(self):
        # Lines 58-59
        self.assertEqual(
            balance_usage._window_label({"duration": 5, "timeUnit": "TIME_UNIT_HOUR"}),
            "5小时")

    def test_time_unit_day(self):
        # Lines 60-61
        self.assertEqual(
            balance_usage._window_label({"duration": 7, "timeUnit": "TIME_UNIT_DAY"}),
            "7天")

    def test_unknown_time_unit_returns_none(self):
        # Line 62: known int duration but unrecognized timeUnit → fallthrough
        self.assertIsNone(
            balance_usage._window_label({"duration": 5, "timeUnit": "TIME_UNIT_WEEK"}))


# ===========================================================================
# balance_usage._window_duration_hours — branches 72, 75-79
# ===========================================================================
class TestWindowDurationHours(unittest.TestCase):
    def test_non_int_duration_returns_zero(self):
        self.assertEqual(
            balance_usage._window_duration_hours({"duration": "x", "timeUnit": "TIME_UNIT_MINUTE"}), 0)

    def test_missing_duration_returns_zero(self):
        self.assertEqual(balance_usage._window_duration_hours({"timeUnit": "TIME_UNIT_MINUTE"}), 0)

    def test_minutes_converted_to_hours(self):
        self.assertEqual(
            balance_usage._window_duration_hours({"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"}), 5)

    def test_hours_returned_directly(self):
        self.assertEqual(
            balance_usage._window_duration_hours({"duration": 12, "timeUnit": "TIME_UNIT_HOUR"}), 12)

    def test_days_converted_to_hours(self):
        self.assertEqual(
            balance_usage._window_duration_hours({"duration": 3, "timeUnit": "TIME_UNIT_DAY"}), 72)

    def test_unknown_time_unit_returns_zero(self):
        self.assertEqual(
            balance_usage._window_duration_hours({"duration": 5, "timeUnit": "TIME_UNIT_WEEK"}), 0)


# ===========================================================================
# balance_usage.fetch_models — line 139 (empty base_url)
# ===========================================================================
class TestFetchModelsEmptyBaseURL(unittest.TestCase):
    def test_empty_base_url_returns_error(self):
        sp = {"providers": {"p": {"base_url": "", "api_key": "sk-x"}}}
        r = balance_usage.fetch_models(sp, "p")
        self.assertEqual(r, {"error": "未配置 base_url"})


# ===========================================================================
# balance_usage.test_provider — lines 220-221 (HTTPError body not JSON)
# ===========================================================================
class TestTestProviderHTTPErrorNonJSON(unittest.TestCase):
    """When the HTTPError body fails JSON parsing, fall back to ``HTTP {code}``."""
    _provider = {"base_url": "https://api.test.com", "api_key": "sk-test",
                 "auth_header": "x-api-key", "models": ["m"]}

    def test_non_json_error_body_falls_back_to_http_code(self):
        err = urllib.error.HTTPError(
            "https://api.test.com/v1/messages", 502,
            "Bad Gateway", {},
            io.BytesIO(b"<html>Bad Gateway</html>"))
        with patch("urllib.request.urlopen", side_effect=err):
            r = balance_usage.test_provider(
                {"providers": {"p": self._provider}}, "p")
        self.assertEqual(r, {"error": "HTTP 502"})

    def test_json_body_read_failure_falls_back_to_http_code(self):
        # HTTPError whose .read() itself raises (rare transport hiccup)
        err = urllib.error.HTTPError(
            "https://api.test.com/v1/messages", 500,
            "Internal Server Error", {}, io.BytesIO(b""))
        # Replace read() to blow up during json.loads → except Exception path
        err.read = lambda: (_ for _ in ()).throw(ValueError("socket closed"))
        with patch("urllib.request.urlopen", side_effect=err):
            r = balance_usage.test_provider(
                {"providers": {"p": self._provider}}, "p")
        self.assertEqual(r, {"error": "HTTP 500"})


# ===========================================================================
# balance_usage.fetch_balance — lines 243-254 (matched-provider API loop)
# ===========================================================================
class TestFetchBalanceMatchedProvider(unittest.TestCase):
    """Exercise the loop body that runs when a supported provider has a key."""
    PROVIDERS = {
        "deepseek": {
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-test",
        },
    }

    def test_success_path_normalizes_response(self):
        body = json.dumps(
            {"balance_infos": [{"total_balance": "9.9",
                                "topped_up_balance": "10", "currency": "CNY"}]}
        ).encode()
        with patch("urllib.request.urlopen", return_value=_FakeResp(body)):
            r = balance_usage.fetch_balance({"providers": self.PROVIDERS})
        self.assertEqual(len(r), 1)
        entry = r[0]
        self.assertTrue(entry["supported"])
        self.assertEqual(len(entry["apis"]), 1)
        self.assertIn("¥9.9", entry["apis"][0]["primary"])

    def test_bearer_auth_used_for_bearer_style(self):
        body = json.dumps(
            {"balance_infos": [{"total_balance": "1", "currency": "CNY"}]}
        ).encode()
        with patch("urllib.request.urlopen", return_value=_FakeResp(body)) as m:
            balance_usage.fetch_balance({"providers": self.PROVIDERS})
        req = m.call_args[0][0]
        self.assertEqual(req.headers.get("Authorization"), "Bearer sk-test")

    def test_raw_auth_used_for_raw_style(self):
        # GLM (bigmodel.cn) uses "raw" auth style → bare key in Authorization
        sp = {"providers": {"glm": {
            "base_url": "https://open.bigmodel.cn",
            "api_key": "sk-glm",
        }}}
        # Two APIs: quota/limit + account report → patch urlopen twice
        bodies = [
            json.dumps({"data": {"limits": [{"unit": 5, "percentage": 50}]}}).encode(),
            json.dumps({"data": {"balance": 1.5, "totalSpendAmount": 0.25}}).encode(),
        ]
        with patch("urllib.request.urlopen",
                   side_effect=[_FakeResp(b) for b in bodies]) as m:
            r = balance_usage.fetch_balance(sp)
        entry = r[0]
        self.assertTrue(entry["supported"])
        self.assertEqual(len(entry["apis"]), 2)
        # Both calls used the bare key (raw style)
        for call in m.call_args_list:
            self.assertEqual(call[0][0].headers.get("Authorization"), "sk-glm")

    def test_exception_path_records_error_label(self):
        with patch("urllib.request.urlopen",
                   side_effect=urllib.error.URLError("timeout")):
            r = balance_usage.fetch_balance({"providers": self.PROVIDERS})
        entry = r[0]
        self.assertTrue(entry["supported"])
        self.assertEqual(len(entry["apis"]), 1)
        self.assertEqual(entry["apis"][0]["error"], "URLError")

    def test_http_error_records_error_type(self):
        with patch("urllib.request.urlopen",
                   side_effect=_http_error(403, b"forbidden")):
            r = balance_usage.fetch_balance({"providers": self.PROVIDERS})
        entry = r[0]
        self.assertEqual(entry["apis"][0]["error"], "HTTPError")


# ===========================================================================
# balance_usage.fetch_usage — lines 284-285 (OSError swallowed)
# ===========================================================================
class TestFetchUsageOSError(unittest.TestCase):
    def test_directory_path_raises_oserror_swallowed(self):
        """``os.path.exists(dir)`` is True but ``open(dir)`` raises
        ``IsADirectoryError`` (subclass of ``OSError``) → swallowed, zeros."""
        with tempfile.TemporaryDirectory() as d:
            r = balance_usage.fetch_usage({"usage_log": {"path": d}})
        self.assertEqual(r["total"]["calls"], 0)
        self.assertEqual(r["providers"], {})


# ===========================================================================
# login_item._plist_path — line 33 (direct call)
# ===========================================================================
class TestPlistPath(unittest.TestCase):
    def test_plist_path_shape(self):
        p = login_item._plist_path()
        self.assertTrue(p.endswith("com.benzai.magic-ai-router.login.plist"))
        self.assertIn("LaunchAgents", p)


# ===========================================================================
# login_item.set_launch_at_login — lines 70-73 (OSError handler)
# ===========================================================================
class TestSetLaunchAtLoginOSError(unittest.TestCase):
    def setUp(self):
        self._orig_frozen = login_item.FROZEN

    def tearDown(self):
        login_item.FROZEN = self._orig_frozen

    def test_enable_oserror_returns_failure(self):
        """makedirs raising OSError → ``(False, "写入登录项失败")``.

        #40: the enable path writes via config_store.atomic_write, which
        catches the OSError, logs the detail, and reports False — the
        user-facing message stays generic.
        """
        login_item.FROZEN = True
        with patch.object(login_item.sys, "executable", "/x"), \
             patch.object(login_item.os.path, "exists", return_value=True), \
             patch.object(login_item.os, "makedirs",
                          side_effect=OSError("disk full")):
            ok, err = login_item.set_launch_at_login(True)
        self.assertFalse(ok)
        self.assertIn("写入登录项失败", err)

    def test_disable_oserror_returns_failure(self):
        """unlink raising OSError → ``(False, "写入登录项失败：...")``."""
        login_item.FROZEN = True
        with patch.object(login_item.sys, "executable", "/x"), \
             patch.object(login_item.os.path, "exists", return_value=True), \
             patch.object(login_item.subprocess, "run"), \
             patch.object(login_item.os, "unlink",
                          side_effect=OSError("permission denied")):
            ok, err = login_item.set_launch_at_login(False)
        self.assertFalse(ok)
        self.assertIn("写入登录项失败", err)
        self.assertIn("permission denied", err)


# ===========================================================================
# util._stamp_from_sources — lines 35-36 (OSError on getmtime swallowed)
# ===========================================================================
class TestStampFromSourcesOSError:
    def test_getmtime_oserror_swallowed(self, tmp_path, monkeypatch):
        """If os.path.getmtime raises OSError (file vanished between glob and
        stat), the loop swallows it and returns None."""
        (tmp_path / "a.py").write_text("")

        def raise_oserror(_path):
            raise OSError("file gone")

        monkeypatch.setattr("os.path.getmtime", raise_oserror)
        assert util._stamp_from_sources(str(tmp_path)) is None


if __name__ == "__main__":
    unittest.main()
