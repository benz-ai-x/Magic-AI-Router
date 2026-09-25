"""Tests for provider_probe.py — 供应商验证与端点探测（自 balance_usage
三刀切迁出，R5；用例语义不变，仅换导入归宿）。

fetch_models / test_provider / probe_provider take raw config dicts (or a
provider-shaped dict) and return business failures as data — HTTP seams are
mocked at AuthenticatedHttpClient.open.
"""
import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from services import provider_probe
from services.authenticated_http import AuthenticatedHttpClient


def _sp(providers):
    return {"providers": providers}


def _models_payload(*ids):
    return json.dumps({"data": [{"id": i} for i in ids]}).encode()


def _http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b""))


class TestFetchModels(unittest.TestCase):
    """fetch_models(sp_raw, name) → {"models": [...]} | {"error": ...}."""

    PROVIDERS = {
        "deepseek": {
            "base_url": "https://api.deepseek.com/anthropic",
            "api_key": "sk-test",
        },
    }

    def _fetch(self, sp=None, name="deepseek"):
        return provider_probe.fetch_models(sp if sp is not None else _sp(self.PROVIDERS), name)

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


class TestTestProviderUnknown(unittest.TestCase):
    def test_unknown_provider_returns_error(self):
        result = provider_probe.test_provider(_sp({}), "nonexistent")
        self.assertNotIn("ok", result)
        self.assertIn("不存在", result["error"])


class TestTestProviderValidation(unittest.TestCase):
    def test_empty_base_url_returns_error(self):
        result = provider_probe.test_provider(
            _sp({"p": {"base_url": "", "api_key": "k", "models": ["m"]}}), "p")
        self.assertIn("base_url", result["error"])

    def test_no_api_key_returns_error(self):
        result = provider_probe.test_provider(
            _sp({"p": {"base_url": "https://x.com", "api_key": None, "models": ["m"]}}), "p")
        self.assertIn("API Key", result["error"])

    def test_no_models_returns_error(self):
        result = provider_probe.test_provider(
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
        result = provider_probe.test_provider(_sp({"p": self._provider}), "p")
        self.assertTrue(result["ok"])
        self.assertEqual(result["model"], "test-model")
        self.assertEqual(result["reply"], "Hello!")

    @patch.object(AuthenticatedHttpClient, "open")
    def test_successful_request_empty_reply(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_resp({
            "model": "test-model", "content": [],
        })
        result = provider_probe.test_provider(_sp({"p": self._provider}), "p")
        self.assertTrue(result["ok"])
        self.assertEqual(result["reply"], "")

    @patch.object(AuthenticatedHttpClient, "open")
    def test_http_error_returns_message(self, mock_urlopen):
        err = urllib.error.HTTPError(
            "https://api.test.com/v1/messages", 400,
            "Bad Request", {},
            io.BytesIO(json.dumps({"error": {"message": "Model not exist."}}).encode()))
        mock_urlopen.side_effect = err
        result = provider_probe.test_provider(_sp({"p": self._provider}), "p")
        self.assertIn("Model not exist", result["error"])

    @patch.object(AuthenticatedHttpClient, "open")
    def test_network_error_returns_exception(self, mock_urlopen):
        mock_urlopen.side_effect = ConnectionError("timeout")
        result = provider_probe.test_provider(_sp({"p": self._provider}), "p")
        self.assertIn("ConnectionError", result["error"])

    @patch.object(AuthenticatedHttpClient, "open")
    def test_model_override(self, mock_urlopen):
        mock_urlopen.return_value = self._mock_resp({
            "model": "custom-model",
            "content": [{"type": "text", "text": "hi"}],
        })
        result = provider_probe.test_provider(
            _sp({"p": self._provider}), "p", model="custom-model")
        self.assertTrue(result["ok"])
        # Verify the request body used the override model
        body = json.loads(mock_urlopen.call_args.kwargs["data"].decode())
        self.assertEqual(body["model"], "custom-model")

    def test_missing_api_key_returns_error(self):
        provider = {"base_url": "https://api.test.com", "enabled": True}
        result = provider_probe.test_provider(_sp({"p": provider}), "p")
        self.assertIn("API Key", result["error"])


# ── ADR-010：端点三级探测 + test_provider 协议分叉 ──────────────────

class TestProbeProvider(unittest.TestCase):
    """probe_provider(provider) → 每协议
    {reachable, auth_ok, latency_ms, models, error}。"""

    def _probe(self, side_effect, provider=None):
        provider = provider or {"base_url": "https://api.test.com",
                                "api_key": "sk-x"}
        with patch.object(AuthenticatedHttpClient, "open",
                          side_effect=side_effect):
            return provider_probe.probe_provider(provider)

    def test_missing_base_url(self):
        self.assertIn("error", provider_probe.probe_provider({}))

    def test_existence_and_models_matrix(self):
        def side_effect(url, headers=None, data=None, method=None,
                        timeout=None):
            if url.endswith("/v1/messages"):
                raise _http_error(404)      # anthropic 端点不存在
            if url.endswith("/chat/completions"):
                raise _http_error(405)      # 路由在（Method Not Allowed）
            if url.endswith("/responses"):
                raise _http_error(401)      # 在但 Key 被拒
            if url.endswith("/v1/models"):
                return _models_payload("m1", "m2")
            raise AssertionError(url)

        out = self._probe(side_effect)
        self.assertFalse(out["anthropic"]["reachable"])
        self.assertTrue(out["anthropic"]["auth_ok"])  # models 认证通过
        self.assertEqual(out["anthropic"]["models"], ["m1", "m2"])
        self.assertTrue(out["openai"]["reachable"])
        self.assertTrue(out["openai"]["auth_ok"])
        self.assertTrue(out["responses"]["reachable"])
        self.assertFalse(out["responses"]["auth_ok"])
        self.assertIn("Key", out["responses"]["error"])
        for proto in ("anthropic", "openai", "responses"):
            self.assertIsInstance(out[proto]["latency_ms"], int)

    def test_models_does_not_flip_reachable(self):
        """models 接口在 ≠ POST 端点在（OpenAI 官方反例）。"""
        def side_effect(url, headers=None, data=None, method=None,
                        timeout=None):
            if url.endswith(("/v1/messages", "/chat/completions",
                             "/responses")):
                raise _http_error(404)
            if url.endswith("/v1/models"):
                return _models_payload("m")
            raise AssertionError(url)

        out = self._probe(side_effect)
        for proto in ("anthropic", "openai", "responses"):
            self.assertFalse(out[proto]["reachable"],
                             f"{proto} 不应因 models 在而转正")

    def test_network_error_classified(self):
        import socket
        def side_effect(url, headers=None, data=None, method=None,
                        timeout=None):
            raise urllib.error.URLError(socket.gaierror())
        out = self._probe(side_effect)
        for proto in ("anthropic", "openai", "responses"):
            self.assertFalse(out[proto]["reachable"])
            self.assertIn("域名解析失败", out[proto]["error"])


class TestTestProviderProtocolFork(unittest.TestCase):
    """ADR-010：test_provider 按 provider.protocol 分叉端点与消息形态。"""

    def test_openai_protocol_posts_chat_completions(self):
        sp = _sp({"oai": {"base_url": "https://api.openai.com/v1",
                          "protocol": "openai", "api_key": "sk",
                          "models": ["gpt-4o-mini"]}})
        resp = json.dumps({"model": "gpt-4o-mini",
                           "choices": [{"message": {"content": "hi"}}]}
                          ).encode()
        with patch.object(AuthenticatedHttpClient, "open",
                          return_value=resp) as m:
            r = provider_probe.test_provider(sp, "oai")
        self.assertTrue(r["ok"])
        self.assertEqual(r["reply"], "hi")
        self.assertEqual(m.call_args[0][0],
                         "https://api.openai.com/v1/chat/completions")
        body = json.loads(m.call_args[1]["data"]
                          if "data" in m.call_args[1]
                          else m.call_args.kwargs["data"])
        self.assertEqual(body["model"], "gpt-4o-mini")
        self.assertIn("max_tokens", body)

    def test_openai_protocol_gpt5_family_uses_completion_tokens(self):
        sp = _sp({"oai": {"base_url": "https://api.openai.com/v1",
                          "protocol": "openai", "api_key": "sk",
                          "models": ["gpt-5.2"]}})
        resp = json.dumps({"choices": [{"message": {"content": "x"}}]}).encode()
        with patch.object(AuthenticatedHttpClient, "open",
                          return_value=resp) as m:
            provider_probe.test_provider(sp, "oai")
        data = (m.call_args[1]["data"] if "data" in m.call_args[1]
                else m.call_args.kwargs["data"])
        body = json.loads(data)
        self.assertNotIn("max_tokens", body)
        self.assertEqual(body["max_completion_tokens"], 32)

    def test_anthropic_default_unchanged(self):
        sp = _sp({"p": {"base_url": "https://api.anthropic.com",
                        "api_key": "sk", "models": ["claude-x"]}})
        resp = json.dumps({"content": [{"type": "text", "text": "yo"}]}).encode()
        with patch.object(AuthenticatedHttpClient, "open",
                          return_value=resp) as m:
            r = provider_probe.test_provider(sp, "p")
        self.assertTrue(r["ok"])
        self.assertEqual(r["reply"], "yo")
        self.assertEqual(m.call_args[0][0],
                         "https://api.anthropic.com/v1/messages")


if __name__ == "__main__":
    unittest.main()
