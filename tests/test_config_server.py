"""Tests for config_server.py — HTTP config API, auth, masking, balance/usage."""
import json
import os
import tempfile
import unittest
from http.client import HTTPConnection
from unittest.mock import MagicMock, patch

from services import config_server
def _start_server(on_sp_saved=None, on_mp_saved=None):
    """Start a config server on a random port, return (server, port)."""
    import threading
    s = config_server.ConfigServer(on_sp_saved=on_sp_saved,
                                   on_mp_saved=on_mp_saved)
    s._server = config_server._ThreadingHTTPServer(
        ("127.0.0.1", 0), config_server._Handler, expected_token=s._token,
        on_sp_saved=on_sp_saved, on_mp_saved=on_mp_saved)
    port = s._server.server_address[1]
    s._thread = threading.Thread(target=s._server.serve_forever, daemon=True)
    s._thread.start()
    return s, port


def _request(port, method, path, body=None, token=None, host="127.0.0.1",
             headers=None):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = dict(headers or {})
    if body is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request(method, path, body=body, headers=headers)
    resp = conn.getresponse()
    data = resp.read().decode()
    conn.close()
    return resp.status, data


class TestHostHeaderGuard(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_valid_host_allowed(self):
        status, _ = _request(self.port, "GET", "/api/state", token=self.token)
        self.assertNotEqual(status, 403)

    def test_invalid_host_rejected(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/state", headers={"Host": "evil.com", "Authorization": f"Bearer {self.token}"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 403)


class TestTokenAuth(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_no_token_rejected(self):
        status, _ = _request(self.port, "GET", "/api/state")
        self.assertEqual(status, 401)

    def test_wrong_token_rejected(self):
        status, _ = _request(self.port, "GET", "/api/state", token="wrong")
        self.assertEqual(status, 401)

    def test_correct_token_allowed(self):
        status, _ = _request(self.port, "GET", "/api/state", token=self.token)
        self.assertEqual(status, 200)

    def test_bearer_header_accepted(self):
        status, _ = _request(self.port, "GET", "/api/state", token=self.token)
        self.assertEqual(status, 200)

    def test_token_comparison_is_constant_time(self):
        """_valid_token must delegate to secrets.compare_digest (not ==)."""
        from types import SimpleNamespace
        import secrets as _secrets
        h = SimpleNamespace(
            path="/api/state",
            headers={"Authorization": f"Bearer {self.token}"},
            server=SimpleNamespace(expected_token=self.token),
        )
        with patch("services.config_server.secrets.compare_digest",
                   wraps=_secrets.compare_digest) as spy:
            result = config_server._Handler._valid_token(h)
            self.assertTrue(result)
            spy.assert_called_once_with(self.token, self.token)


class TestLoginPage(unittest.TestCase):
    """GET / 无 token 时返回登录页 HTML（浏览器直接打开可用）；API 401 仍 JSON。

    macOS 桥接首导航带 Bearer → 200（不经过此路径），零变化；Docker 版
    浏览器直接打开获得登录页。
    """

    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_get_root_no_token_returns_login_html(self):
        status, body = _request(self.port, "GET", "/")
        self.assertEqual(status, 401)
        # 登录页是自包含 HTML（非 JSON 错误）
        self.assertIn("<!doctype html>", body.lower())
        self.assertIn("token", body.lower())
        # 含表单与 JS：fetch 带 Authorization 头调 / 种 cookie 后跳转
        self.assertIn("Authorization", body)
        self.assertIn("Bearer", body)
        self.assertIn("fetch", body)

    def test_get_root_wrong_token_also_returns_login_html(self):
        status, body = _request(self.port, "GET", "/", token="wrong")
        self.assertEqual(status, 401)
        self.assertIn("<!doctype html>", body.lower())

    def test_api_401_remains_json(self):
        """API 路径的 401 保持纯 JSON——curl/脚本客户端不期待 HTML。"""
        status, body = _request(self.port, "GET", "/api/state")
        self.assertEqual(status, 401)
        data = json.loads(body)
        self.assertEqual(data, {"error": "unauthorized"})

    def test_post_401_remains_json(self):
        status, body = _request(self.port, "POST", "/api/test-provider",
                                body="{}")
        self.assertEqual(status, 401)
        data = json.loads(body)
        self.assertEqual(data, {"error": "unauthorized"})

    def test_bridge_bearer_still_200(self):
        """macOS 桥接路径不回归：带 Bearer 首导航仍直接进页面。"""
        status, body = _request(self.port, "GET", "/", token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("<!doctype html>", body.lower())
        self.assertNotIn("登录", body[:500])  # 不是登录页


class TestFavicon(unittest.TestCase):
    """Browsers auto-request /favicon.ico without a token — 204, not 403."""

    def setUp(self):
        self.server, self.port = _start_server()

    def tearDown(self):
        self.server.stop()

    def test_favicon_returns_204_without_token(self):
        status, body = _request(self.port, "GET", "/favicon.ico")
        self.assertEqual(status, 204)
        self.assertEqual(body, "")

    def test_favicon_still_guards_host(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/favicon.ico", headers={"Host": "evil.com"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 403)


class TestBodySizeLimit(unittest.TestCase):
    """POST/PUT bodies over MAX_BODY_BYTES must be rejected with 413."""

    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_post_oversized_body_returns_413(self):
        with patch("services.config_server.MAX_BODY_BYTES", 2):
            status, _ = _request(
                self.port, "POST",
                "/api/fetch-models", token=self.token,
                body='{"a": 1}',  # 7 bytes > 2
            )
        self.assertEqual(status, 413)

    def test_put_oversized_body_returns_413(self):
        with patch("services.config_server.MAX_BODY_BYTES", 2):
            status, _ = _request(
                self.port, "PUT",
                "/api/state", token=self.token,
                body='{"a": 1}',
            )
        self.assertEqual(status, 413)

    def test_post_normal_body_still_works(self):
        with patch("services.config_server.MAX_BODY_BYTES", 10_000_000):
            with patch("services.sp_config.sp_load_raw", return_value={"providers": {}}):
                status, _ = _request(
                    self.port, "POST",
                    "/api/fetch-models", token=self.token,
                    body='{"provider": "x"}',
                )
        self.assertEqual(status, 200)

    def test_post_invalid_content_length_returns_400(self):
        """Non-numeric Content-Length must return 400, not crash the handler."""
        import socket
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.sendall(
            f"POST /api/fetch-models HTTP/1.1\r\nAuthorization: Bearer {self.token}\r\n"
            f"Host: 127.0.0.1\r\n"
            f"Content-Length: not-a-number\r\n"
            f"\r\n".encode()
        )
        resp_line = sock.recv(4096).decode(errors="replace").split("\r\n")[0]
        sock.close()
        self.assertIn("400", resp_line)

    def test_negative_content_length_rejected(self):
        """Negative Content-Length must be rejected — rfile.read(-1) reads
        until EOF, defeating the body-size cap.  Python's BaseHTTPRequestHandler
        treats -1 as an oversized value (413) at the protocol level; our own
        guard returns 400. Either way, the request must not reach the handler."""
        import socket
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.sendall(
            f"POST /api/fetch-models HTTP/1.1\r\nAuthorization: Bearer {self.token}\r\n"
            f"Host: 127.0.0.1\r\n"
            f"Content-Length: -1\r\n"
            f"\r\n".encode()
        )
        resp_line = sock.recv(4096).decode(errors="replace").split("\r\n")[0]
        sock.close()
        self.assertTrue("400" in resp_line or "413" in resp_line,
                        f"expected rejection, got: {resp_line}")


class TestApiState(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token
        self._tmpdirs = []

    def tearDown(self):
        self.server.stop()
        for d in self._tmpdirs:
            d.cleanup()

    def test_get_state_returns_json(self):
        status, data = _request(self.port, "GET", "/api/state", token=self.token)
        self.assertEqual(status, 200)
        parsed = json.loads(data)
        self.assertIn("mp", parsed)
        self.assertIn("sp", parsed)

    def test_read_sp_masks_provider_keys(self):
        from suanpan.config import load_config_masked
        with patch("suanpan.config.load_config_raw") as mock_raw:
            mock_raw.return_value = {
                "providers": {"deepseek": {"api_key": "sk-abcd1234efgh5678"}},
                "api_key": "sk-secretkey123",
            }
            result = load_config_masked("/dev/null")
        prov = result["providers"]["deepseek"]
        self.assertIsNone(prov["api_key"])
        self.assertTrue(prov["api_key_set"])
        self.assertIsNone(result["api_key"])
        self.assertTrue(result["api_key_set"])

    def test_unknown_route_returns_404(self):
        status, _ = _request(self.port, "GET", "/api/nonexistent", token=self.token)
        self.assertEqual(status, 404)

    def test_html_served_at_root(self):
        status, data = _request(self.port, "GET", "/", token=self.token)
        self.assertEqual(status, 200)
        self.assertIn("<html", data.lower())


class TestPutState(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_put_invalid_json_returns_400(self):
        status, _ = _request(self.port, "PUT", "/api/state", token=self.token,
                             body="not json")
        self.assertEqual(status, 400)

    def test_put_empty_body_returns_400(self):
        status, _ = _request(self.port, "PUT", "/api/state", token=self.token)
        self.assertEqual(status, 400)

    def test_put_goes_through_config_state_store(self):
        """issue #6：PUT 经 ConfigStateStore 事务边界（prepare→commit）。"""
        from mpconf.config_state import CommitPlan, SaveResult
        plan = CommitPlan(True, [], {"tunnels": []}, {"providers": {}})
        with patch("services.config_server.ConfigStateStore") as store_cls:
            store_cls.return_value.prepare.return_value = plan
            store_cls.return_value.commit.return_value = SaveResult(True, None, [])
            body = json.dumps({"mp": {"tunnels": []}, "sp": {"providers": {}}})
            status, data = _request(self.port, "PUT",
                                    "/api/state", token=self.token,
                                    body=body)
        self.assertEqual(status, 200)
        store_cls.return_value.prepare.assert_called_once()
        store_cls.return_value.commit.assert_called_once()

    def test_put_with_validation_errors_returns_422(self):
        from mpconf.config_state import CommitPlan
        with patch("services.config_server.ConfigStateStore") as store_cls:
            store_cls.return_value.prepare.return_value = CommitPlan(
                False, ["端口无效"])
            body = json.dumps({"mp": {}})
            status, data = _request(self.port, "PUT",
                                    "/api/state", token=self.token,
                                    body=body)
        self.assertEqual(status, 422)
        parsed = json.loads(data)
        self.assertFalse(parsed["ok"])
        self.assertIn("端口无效", parsed["errors"])


class TestFetchModelsEndpoint(unittest.TestCase):
    SP = {
        "providers": {
            "deepseek": {
                "base_url": "https://api.deepseek.com/anthropic",
                "api_key": "sk-test",
            },
        },
    }

    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def _post(self, body, token=True):
        return _request(self.port, "POST", "/api/fetch-models", body=body,
                        token=self.token if token else None)

    def test_post_requires_token(self):
        status, _ = self._post('{"provider": "deepseek"}', token=False)
        self.assertEqual(status, 401)

    def test_post_unknown_route_404(self):
        status, _ = _request(self.port, "POST", "/api/nope", token=self.token,
                             body="{}")
        self.assertEqual(status, 404)

    def test_post_invalid_json_400(self):
        status, _ = self._post("not json")
        self.assertEqual(status, 400)

    def test_fetch_models_success_through_handler(self):
        body = json.dumps({"data": [{"id": "deepseek-v4-flash"}]}).encode()
        with patch("services.sp_config.sp_load_raw", return_value=self.SP), \
             patch("services.authenticated_http.AuthenticatedHttpClient.open",
                   return_value=body):
            status, data = self._post('{"provider": "deepseek"}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"models": ["deepseek-v4-flash"]})

    def test_fetch_models_unknown_provider_returns_error(self):
        with patch("services.sp_config.sp_load_raw", return_value=self.SP):
            status, data = self._post('{"provider": "ghost"}')
        self.assertEqual(status, 200)
        self.assertIn("error", json.loads(data))


class TestRestoreKey(unittest.TestCase):
    def test_untouched_key_restored(self):
        # api_key_set=True + no new value → keep the existing key.
        from shared.provider_auth import restore_masked_key as _restore_key
        result = _restore_key(None, "original-key", keep=True)
        self.assertEqual(result, "original-key")

    def test_new_key_kept(self):
        from shared.provider_auth import restore_masked_key as _restore_key
        result = _restore_key("sk-newkey123", "old-key", keep=True)
        self.assertEqual(result, "sk-newkey123")

    def test_cleared_when_not_flagged(self):
        # api_key_set=False + no new value → the key is cleared.
        from shared.provider_auth import restore_masked_key as _restore_key
        result = _restore_key(None, "old-key", keep=False)
        self.assertIsNone(result)


class TestReadMpEmpty(unittest.TestCase):
    def test_empty_config_returns_empty_dict(self):
        with patch.object(config_server, "load_config", return_value=None), \
             patch.object(config_server, "merge_config", return_value=None):
            self.assertEqual(config_server._read_mp(), {})


class TestBalanceUsageEndpoints(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_balance_endpoint_returns_json(self):
        # Patch fetch_balance so the handler path runs without real network calls.
        with patch.object(config_server, "fetch_balance", return_value=[{"provider": "p"}]):
            status, data = _request(self.port, "GET",
                                    "/api/balance", token=self.token)
        self.assertEqual(status, 200)
        self.assertIsInstance(json.loads(data), list)

    def test_usage_endpoint_returns_json(self):
        cfg = {"usage_log": {"path": "/nonexistent/usage.jsonl"}}
        with patch.object(config_server.sp_config, "sp_load_raw", return_value=cfg):
            status, data = _request(self.port, "GET",
                                    "/api/usage", token=self.token)
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertIsInstance(payload, dict)
        self.assertIn("daily", payload)
        self.assertIn("scenarios", payload)

    def test_usage_endpoint_accepts_supported_ranges(self):
        cfg = {"usage_log": {"path": "/nonexistent/usage.jsonl"}}
        with patch.object(config_server.sp_config, "sp_load_raw", return_value=cfg):
            for usage_range in ("today", "7d", "all"):
                with self.subTest(usage_range=usage_range):
                    status, _ = _request(
                        self.port, "GET",
                        f"/api/usage?range={usage_range}", token=self.token,
                    )
                    self.assertEqual(status, 200)

    def test_usage_endpoint_defaults_to_all(self):
        with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
            f.write(json.dumps({
                "ts": "2020-01-01T00:00:00+08:00", "provider": "p",
                "scenario": "default", "input_tokens": 1, "output_tokens": 1,
                "cache_read_tokens": 0, "cache_creation_tokens": 0,
                "status": 200, "latency_ms": 1,
            }) + "\n")
            path = f.name
        try:
            with patch.object(
                    config_server.sp_config, "sp_load_raw",
                    return_value={"usage_log": {"path": path}}):
                default_status, default_data = _request(
                    self.port, "GET", "/api/usage", token=self.token)
                all_status, all_data = _request(
                    self.port, "GET",
                    "/api/usage?range=all", token=self.token)
        finally:
            os.unlink(path)
        self.assertEqual(default_status, 200)
        self.assertEqual(all_status, 200)
        self.assertEqual(json.loads(default_data), json.loads(all_data))
        self.assertEqual(json.loads(default_data)["total"]["calls"], 1)

    def test_usage_endpoint_rejects_invalid_range(self):
        status, data = _request(
            self.port, "GET",
            "/api/usage?range=bogus", token=self.token,
        )
        self.assertEqual(status, 400)
        self.assertIn("range", json.loads(data)["error"])

    def test_usage_endpoint_accepts_month_range(self):
        status, data = _request(
            self.port, "GET",
            "/api/usage?range=month", token=self.token,
        )
        self.assertEqual(status, 200)
        self.assertIn("total", json.loads(data))


class TestPutStateWrongPath(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_put_wrong_path_returns_404(self):
        status, data = _request(self.port, "PUT",
                                "/api/wrong", token=self.token,
                                body=json.dumps({}))
        self.assertEqual(status, 404)


class TestConfigServerStart(unittest.TestCase):
    def test_start_on_unavailable_port_returns_false(self):
        # Bind a port first, then try to start the server on it.
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.listen(1)
        try:
            cs = config_server.ConfigServer(port=port)
            self.assertFalse(cs.start())
        finally:
            s.close()

    def test_start_when_already_running_returns_true(self):
        cs = config_server.ConfigServer(port=0)
        # Simulate an already-alive worker thread so running == True
        cs._thread = MagicMock()
        cs._thread.is_alive.return_value = True
        self.assertTrue(cs.start())


class TestCaptureStateField(unittest.TestCase):
    def test_capture_active_never_round_trips_into_plan(self):
        """服务端注入的只读字段在 prepare 阶段剥离，不可能回写文件。"""
        from mpconf.config_state import CommitPlan  # noqa: F401
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            store = config_server.ConfigStateStore(
                mp_path=str(Path(d) / "m.json"),
                sp_path=str(Path(d) / "s.yaml"))
            plan = store.prepare(mp={"tunnels": [], "capture_active": True})
            self.assertTrue(plan.ok)
            self.assertNotIn("capture_active", plan.mp_candidate)


class TestTestTunnelEndpoint(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def _post(self, body, token=True):
        return _request(self.port, "POST", "/api/test-tunnel", body=body,
                        token=self.token if token else None)

    def test_requires_token(self):
        status, _ = self._post('{"index": 0}', token=False)
        self.assertEqual(status, 401)

    def test_invalid_index_type_returns_400(self):
        for bad in ('{"index": "x"}', '{"index": true}', '{"index": null}', '{}', '"str"'):
            with self.subTest(body=bad):
                status, data = self._post(bad)
                self.assertEqual(status, 400)
                body = json.loads(data)
                # #69 S11d：非 dict body（如 "str"）被 _read_json_body 统一
                # 400 拒绝（error 键）；dict 但 index 非法走端点逻辑（ok 键）
                self.assertTrue(
                    ("ok" in body and not body["ok"]) or "error" in body,
                    body)

    def test_no_tunnels_returns_400(self):
        with patch.object(config_server, "_read_mp", return_value={"tunnels": []}):
            status, data = self._post('{"index": 0}')
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(data)["ok"])

    def test_index_out_of_range_returns_400(self):
        cfg = {"tunnels": [{"ssh_host": "h"}]}
        with patch.object(config_server, "_read_mp", return_value=cfg):
            status, data = self._post('{"index": 5}')
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(data)["ok"])

    def test_negative_index_returns_400(self):
        cfg = {"tunnels": [{"ssh_host": "h"}]}
        with patch.object(config_server, "_read_mp", return_value=cfg):
            status, _ = self._post('{"index": -1}')
        self.assertEqual(status, 400)

    def test_valid_index_delegates_to_test_tunnel(self):
        tunnel = {"ssh_host": "example.com", "ssh_user": "u", "ssh_port": 2222}
        with patch.object(config_server, "_read_mp",
                          return_value={"tunnels": [tunnel]}), \
             patch.object(config_server, "test_tunnel",
                          return_value={"ok": True}) as probe:
            status, data = self._post('{"index": 0}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"ok": True})
        probe.assert_called_once_with(tunnel)


class TestTunnelProbeLogic(unittest.TestCase):
    """config_server.test_tunnel 的端点职责：输入校验 + Keychain 取用 +
    委托 ssh_launch.probe（探针 argv / 失败分类的测试在 test_ssh_launch.py）。"""

    _KEY_TUNNEL = {
        "ssh_host": "example.com", "ssh_user": "u", "ssh_port": 2222,
        "auth_type": "key", "ssh_key": "~/.ssh/id_ed25519",
    }
    _PW_TUNNEL = {
        "ssh_host": "example.com", "ssh_user": "u", "ssh_port": 22,
        "auth_type": "password",
    }

    def test_missing_host_is_rejected_without_probe(self):
        with patch.object(config_server.ssh_launch, "probe") as probe:
            result = config_server.test_tunnel({"ssh_host": "  ", "ssh_port": 22})
        probe.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("地址", result["error"])

    def test_invalid_port_is_rejected(self):
        result = config_server.test_tunnel({"ssh_host": "h", "ssh_port": 99999})
        self.assertFalse(result["ok"])
        result = config_server.test_tunnel({"ssh_host": "h", "ssh_port": "x"})
        self.assertFalse(result["ok"])

    def test_option_like_destination_is_rejected(self):
        result = config_server.test_tunnel({"ssh_host": "-oProxyCommand=evil"})
        self.assertFalse(result["ok"])
        self.assertIn("无效", result["error"])

    def test_password_auth_without_saved_password(self):
        with patch.object(config_server.keychain, "get_password",
                          return_value=""), \
             patch.object(config_server.ssh_launch, "probe") as probe:
            result = config_server.test_tunnel(self._PW_TUNNEL)
        probe.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("密码", result["error"])

    def test_key_auth_delegates_to_ssh_launch(self):
        with patch.object(config_server.ssh_launch, "probe",
                          return_value={"ok": True}) as probe:
            result = config_server.test_tunnel(self._KEY_TUNNEL)
        self.assertEqual(result, {"ok": True})
        probe.assert_called_once_with(self._KEY_TUNNEL, password="")

    def test_password_auth_passes_saved_password_through(self):
        with patch.object(config_server.keychain, "get_password",
                          return_value="sekrit"), \
             patch.object(config_server.ssh_launch, "probe",
                          return_value={"ok": False, "error": "连接超时"}) as probe:
            result = config_server.test_tunnel(self._PW_TUNNEL)
        self.assertEqual(result, {"ok": False, "error": "连接超时"})
        probe.assert_called_once_with(self._PW_TUNNEL, password="sekrit")

    def test_probe_receives_normalized_tunnel_fields(self):
        """手改配置的空白/非规范端口经校验归一后才进探针。"""
        raw = {"ssh_host": "  example.com ", "ssh_user": " u ",
               "ssh_port": "2222", "auth_type": "key", "ssh_key": "k"}
        with patch.object(config_server.ssh_launch, "probe",
                          return_value={"ok": True}) as probe:
            result = config_server.test_tunnel(raw)
        self.assertEqual(result, {"ok": True})
        target = probe.call_args[0][0]
        self.assertEqual(target["ssh_host"], "example.com")
        self.assertEqual(target["ssh_user"], "u")
        self.assertEqual(target["ssh_port"], 2222)


class TestTestForwardEndpoint(unittest.TestCase):
    """POST /api/test-forward {index, forward}：行内测试按钮的后端面。"""

    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def _post(self, body, token=True):
        return _request(self.port, "POST", "/api/test-forward", body=body,
                        token=self.token if token else None)

    def test_requires_token(self):
        status, _ = self._post('{"index": 0, "forward": {}}', token=False)
        self.assertEqual(status, 401)

    def test_bad_shape_returns_400(self):
        for bad in ('{"index": "x", "forward": {}}',
                    '{"index": true, "forward": {}}',
                    '{}',
                    '{"index": 0}',
                    '{"forward": {}}',
                    '{"index": 0, "forward": "x"}',
                    '{"tunnel": "x", "forward": {}}',
                    '{"tunnel": 5, "forward": {}}'):
            with self.subTest(body=bad):
                status, data = self._post(bad)
                self.assertEqual(status, 400)
                body = json.loads(data)
                self.assertTrue(
                    ("ok" in body and not body["ok"]) or "error" in body,
                    body)

    def test_no_tunnels_returns_400(self):
        with patch.object(config_server, "_read_mp",
                          return_value={"tunnels": []}):
            status, data = self._post('{"index": 0, "forward": {}}')
        self.assertEqual(status, 400)
        self.assertFalse(json.loads(data)["ok"])

    def test_index_out_of_range_returns_400(self):
        cfg = {"tunnels": [{"ssh_host": "h"}]}
        with patch.object(config_server, "_read_mp", return_value=cfg):
            status, _ = self._post('{"index": 5, "forward": {}}')
        self.assertEqual(status, 400)

    def test_form_tunnel_body_delegates_without_index(self):
        """设置窗新载荷 {tunnel, forward}：隧道与转发都取表单当前值——
        未保存的新隧道同样可测，不落 _read_mp（无 index 可解析）。"""
        tunnel = {"ssh_host": "new.example.com", "ssh_user": "u",
                  "ssh_port": 2222, "auth_type": "key"}
        forward = {"local_port": 9000, "remote_host": "10.0.0.5",
                   "remote_port": 8000}
        with patch.object(config_server, "_read_mp") as rm, \
             patch.object(config_server, "test_forward",
                          return_value={"ok": True, "latency_ms": 42}) as tf:
            status, data = self._post(json.dumps({"tunnel": tunnel,
                                                  "forward": forward}))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"ok": True, "latency_ms": 42})
        rm.assert_not_called()
        tf.assert_called_once_with(tunnel, forward)

    def test_legacy_index_body_still_resolves_saved_tunnel(self):
        """旧载荷 {index, forward} 兼容：按已保存隧道解析（agent.md 契约）。"""
        tunnel = {"ssh_host": "example.com", "ssh_user": "u", "ssh_port": 22}
        forward = {"local_port": 9000, "remote_host": "10.0.0.5",
                   "remote_port": 8000}
        with patch.object(config_server, "_read_mp",
                          return_value={"tunnels": [tunnel]}), \
             patch.object(config_server, "test_forward",
                          return_value={"ok": True, "latency_ms": 42}) as tf:
            status, data = self._post(json.dumps({"index": 0,
                                                  "forward": forward}))
        self.assertEqual(status, 200)
        tf.assert_called_once_with(tunnel, forward)


class TestForwardProbeLogic(unittest.TestCase):
    """config_server.test_forward 的端点职责：输入守卫 + Keychain 取用 +
    委托 ssh_launch.probe_forward（-W argv / 分类的测试在 test_ssh_launch.py）。"""

    _KEY_TUNNEL = {
        "ssh_host": "example.com", "ssh_user": "u", "ssh_port": 2222,
        "auth_type": "key", "ssh_key": "~/.ssh/id_ed25519",
    }
    _PW_TUNNEL = {"ssh_host": "example.com", "ssh_user": "u",
                  "ssh_port": 22, "auth_type": "password"}

    def test_invalid_tunnel_guarded_without_probe(self):
        with patch.object(config_server.ssh_launch, "probe_forward") as pf:
            result = config_server.test_forward(
                {"ssh_host": "  ", "ssh_port": 22}, {"remote_port": 8000})
        pf.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("地址", result["error"])

    def test_option_like_destination_is_rejected(self):
        result = config_server.test_forward(
            {"ssh_host": "-oProxyCommand=evil"}, {"remote_port": 8000})
        self.assertFalse(result["ok"])
        self.assertIn("无效", result["error"])

    def test_password_auth_without_saved_password(self):
        with patch.object(config_server.keychain, "get_password",
                          return_value=""), \
             patch.object(config_server.ssh_launch, "probe_forward") as pf:
            result = config_server.test_forward(
                self._PW_TUNNEL, {"remote_port": 8000})
        pf.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("密码", result["error"])

    def test_delegates_with_form_values_and_password(self):
        with patch.object(config_server.keychain, "get_password",
                          return_value="sekrit"), \
             patch.object(config_server.ssh_launch, "probe_forward",
                          return_value={"ok": True,
                                        "latency_ms": 120}) as pf:
            result = config_server.test_forward(self._PW_TUNNEL, {
                "local_port": 9000, "remote_host": "10.0.0.5",
                "remote_port": 8000})
        self.assertEqual(result, {"ok": True, "latency_ms": 120})
        args, kwargs = pf.call_args
        self.assertEqual(kwargs.get("password"), "sekrit")
        # 隧道侧归一后传入；forward 的三个表单值原样透传（归一在 probe_forward）
        self.assertEqual(args[0]["ssh_host"], "example.com")
        self.assertEqual(args[1], "10.0.0.5")
        self.assertEqual(args[2], 8000)


class TestCaptureCleanEndpoint(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def _post(self):
        return _request(self.port, "POST",
                        "/api/capture-clean", token=self.token, body="{}")

    def test_requires_token(self):
        status, _ = _request(self.port, "POST", "/api/capture-clean",
                             body="{}")
        self.assertEqual(status, 401)

    def test_empty_body_returns_400(self):
        # Client contract (mirrors the other POST endpoints): a JSON body is
        # mandatory — config_ui's cleanCapture() sends '{}'.
        status, _ = _request(self.port, "POST",
                             "/api/capture-clean", token=self.token)
        self.assertEqual(status, 400)

    def test_clean_delegates_to_capture_store(self):
        with patch.object(config_server, "_read_mp",
                          return_value={"capture_dir": "~/captures"}), \
             patch.object(config_server.capture_store, "clean",
                          return_value=3) as clean:
            status, data = self._post()
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"ok": True, "removed": 3})
        clean.assert_called_once_with("~/captures")

    def test_clean_defaults_to_none_when_dir_missing(self):
        with patch.object(config_server, "_read_mp", return_value={}), \
             patch.object(config_server.capture_store, "clean",
                          return_value=0) as clean:
            status, data = self._post()
        self.assertEqual(status, 200)
        clean.assert_called_once_with(None)

    def test_clean_oserror_surfaces_message(self):
        with patch.object(config_server, "_read_mp", return_value={}), \
             patch.object(config_server.capture_store, "clean",
                          side_effect=OSError("拒绝修改非 Magic AI Router 创建的现有目录")):
            status, data = self._post()
        self.assertEqual(status, 200)
        parsed = json.loads(data)
        self.assertFalse(parsed["ok"])
        self.assertIn("Magic AI Router", parsed["error"])


if __name__ == "__main__":
    unittest.main()


class TestHeaderOnlyAuth(unittest.TestCase):
    """issue #10：token 只进 Authorization header 与 HttpOnly 会话 cookie。

    URL 永不带凭证；query-string 认证路径删除；无凭证的 / 与 /api/* 一律
    401。常量时间比较保留。
    """

    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def _get(self, path, headers=None):
        return _request(self.port, "GET", path, headers=headers or {})

    def test_root_without_credentials_is_401(self):
        status, _ = self._get("/")
        self.assertEqual(status, 401)

    def test_query_string_token_no_longer_accepted(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", f"/api/state?token={self.token}")  # 真 query 形态
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 401, "query 认证路径必须删除")

    def test_root_with_bearer_header_sets_httponly_session_cookie(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/", headers={
            "Authorization": f"Bearer {self.token}"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        set_cookie = resp.getheader("Set-Cookie", "")
        self.assertIn("cfgsess=", set_cookie)
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)

    def test_api_with_session_cookie_only_is_authorized(self):
        _, resp_headers = self._get("/", headers={
            "Authorization": f"Bearer {self.token}"})
        # 从原始响应提取 cookie（_request 返回文本，改用 http.client 直取）
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/state", headers={
            "Cookie": f"cfgsess={self.token}"})
        resp = conn.getresponse()
        body = resp.read().decode()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertIn('"mp"', body)

    def test_bogus_cookie_rejected_constant_time_paths_alive(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/state", headers={
            "Cookie": "cfgsess=wrong"})
        resp = conn.getresponse()
        resp.read()
        conn.close()
        self.assertEqual(resp.status, 401)


class TestConfigServerParameterization(unittest.TestCase):
    """ConfigServer 的 bind_host / token 构造参数（Docker 适配的 seam）。

    默认值保持 macOS 行为：绑 127.0.0.1 + 自造随机 token。
    """

    def tearDown(self):
        srv = getattr(self, "srv", None)
        if srv is not None:
            srv.stop()

    def _start(self, **kwargs):
        self.srv = config_server.ConfigServer(port=0, **kwargs)
        self.assertTrue(self.srv.start())
        return self.srv._server.server_address[1]

    def test_defaults_bind_loopback_and_generate_token(self):
        port = self._start()
        self.assertEqual(self.srv._server.server_address[0], "127.0.0.1")
        self.assertRegex(self.srv.token, r"^[0-9a-f]{32}$")
        status, _ = _request(port, "GET", "/api/state", token=self.srv.token)
        self.assertEqual(status, 200)

    def test_custom_bind_host_honored(self):
        # 0.0.0.0 即 Docker 实取值：绑定后 loopback 仍可达。
        port = self._start(bind_host="0.0.0.0")
        self.assertEqual(self.srv._server.server_address[0], "0.0.0.0")
        status, _ = _request(port, "GET", "/api/state", token=self.srv.token)
        self.assertEqual(status, 200)

    def test_custom_token_used_for_auth(self):
        port = self._start(token="fixed-token")
        self.assertEqual(self.srv.token, "fixed-token")
        status, _ = _request(port, "GET", "/api/state", token="fixed-token")
        self.assertEqual(status, 200)
        status, _ = _request(port, "GET", "/api/state")
        self.assertEqual(status, 401)


class TestMultiActiveDecorations(unittest.TestCase):
    """多活（v0.9）：/api/state 的 is_proxy / forward_running 装饰注入。"""

    def _state(self, tunnel_states_fn):
        import threading
        s = config_server.ConfigServer(tunnel_states_fn=tunnel_states_fn)
        s._server = config_server._ThreadingHTTPServer(
            ("127.0.0.1", 0), config_server._Handler,
            expected_token=s._token, tunnel_states_fn=tunnel_states_fn)
        port = s._server.server_address[1]
        threading.Thread(target=s._server.serve_forever, daemon=True).start()
        self.addCleanup(s.stop)
        status, body = _request(port, "GET", "/api/state", token=s._token)
        return json.loads(body)["mp"]

    def test_is_proxy_and_forward_running_injected(self):
        cfg = {"tunnels": [
            {"id": "t-1", "ssh_host": "a", "forwards": []},
            {"id": "t-2", "ssh_host": "b", "forwards": []}],
            "current_tunnel": 0}
        with patch.object(config_server, "_read_mp", return_value=cfg):
            mp = self._state(lambda: [("t-2", "b", "connected")])
        self.assertTrue(mp["tunnels"][0]["is_proxy"])
        self.assertFalse(mp["tunnels"][0]["forward_running"])
        self.assertFalse(mp["tunnels"][1]["is_proxy"])
        self.assertTrue(mp["tunnels"][1]["forward_running"])

    def test_states_fn_absent_decorates_all_false(self):
        cfg = {"tunnels": [{"id": "t-1", "ssh_host": "a", "forwards": []}],
               "current_tunnel": 0}
        with patch.object(config_server, "_read_mp", return_value=cfg):
            mp = self._state(None)
        self.assertTrue(mp["tunnels"][0]["is_proxy"])
        self.assertFalse(mp["tunnels"][0]["forward_running"])

    def test_decorations_never_round_trip_into_plan(self):
        """READONLY_DECORATED_FIELDS 扩名单——is_proxy/forward_running 永不落盘。"""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            store = config_server.ConfigStateStore(
                mp_path=str(Path(d) / "m.json"),
                sp_path=str(Path(d) / "s.yaml"))
            plan = store.prepare(mp={"tunnels": [
                {"id": "t-1", "ssh_host": "a", "forwards": [],
                 "is_proxy": True, "forward_running": True}]})
            self.assertTrue(plan.ok)
            t = plan.mp_candidate["tunnels"][0]
            self.assertNotIn("is_proxy", t)
            self.assertNotIn("forward_running", t)


class TestPutSectionCallbacks(unittest.TestCase):
    """PUT 提交成功后按「事务涉及的段」触发回调：MP 段 → on_mp_saved
    （app 刷新内存副本），SP 段 → on_sp_saved（网关 reload）。失败不触发。"""

    def setUp(self):
        self.mp_saved = MagicMock()
        self.sp_saved = MagicMock()
        self.server, self.port = _start_server(self.sp_saved, self.mp_saved)
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def _put(self, body_obj, commit_ok=True):
        from mpconf.config_state import CommitPlan, SaveResult
        plan = (CommitPlan(True, [], body_obj.get("mp"), body_obj.get("sp"))
                if commit_ok else CommitPlan(False, ["bad"]))
        result = (SaveResult(True, None, []) if commit_ok
                  else SaveResult(False, "mp", ["bad"]))

        def _commit(_plan, on_committed=None):
            # 真实 commit 的语义替身：成功才触发 on_committed
            if commit_ok and on_committed is not None:
                on_committed()
            return result

        with patch("services.config_server.ConfigStateStore") as store_cls:
            store_cls.return_value.prepare.return_value = plan
            store_cls.return_value.commit.side_effect = _commit
            return _request(self.port, "PUT", "/api/state",
                            token=self.token, body=json.dumps(body_obj))

    def test_mp_only_put_fires_on_mp_saved_only(self):
        status, _ = self._put({"mp": {"tunnels": []}})
        self.assertEqual(status, 200)
        self.mp_saved.assert_called_once()
        self.sp_saved.assert_not_called()

    def test_sp_only_put_fires_on_sp_saved_only(self):
        status, _ = self._put({"sp": {"providers": {}}})
        self.assertEqual(status, 200)
        self.sp_saved.assert_called_once()
        self.mp_saved.assert_not_called()

    def test_both_sections_fire_both_callbacks(self):
        status, _ = self._put({"mp": {"tunnels": []}, "sp": {"providers": {}}})
        self.assertEqual(status, 200)
        self.mp_saved.assert_called_once()
        self.sp_saved.assert_called_once()

    def test_failed_commit_fires_nothing(self):
        status, _ = self._put({"mp": {"tunnels": []}}, commit_ok=False)
        self.assertEqual(status, 422)
        self.mp_saved.assert_not_called()
        self.sp_saved.assert_not_called()
