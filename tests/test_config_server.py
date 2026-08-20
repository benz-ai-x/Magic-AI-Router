"""Tests for config_server.py — HTTP config API, auth, masking, balance/usage."""
import json
import os
import tempfile
import unittest
from http.client import HTTPConnection
from unittest.mock import MagicMock, patch

from services import config_server
def _start_server():
    """Start a config server on a random port, return (server, port)."""
    import threading
    s = config_server.ConfigServer()
    s._server = config_server._ThreadingHTTPServer(
        ("127.0.0.1", 0), config_server._Handler, expected_token=s._token)
    port = s._server.server_address[1]
    s._thread = threading.Thread(target=s._server.serve_forever, daemon=True)
    s._thread.start()
    return s, port


def _request(port, method, path, body=None, token=None, host="127.0.0.1"):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {}
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
        status, _ = _request(self.port, "GET", f"/api/state?token={self.token}")
        self.assertNotEqual(status, 403)

    def test_invalid_host_rejected(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", f"/api/state?token={self.token}", headers={"Host": "evil.com"})
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
        self.assertEqual(status, 403)

    def test_wrong_token_rejected(self):
        status, _ = _request(self.port, "GET", "/api/state?token=wrong")
        self.assertEqual(status, 403)

    def test_correct_token_allowed(self):
        status, _ = _request(self.port, "GET", f"/api/state?token={self.token}")
        self.assertEqual(status, 200)

    def test_bearer_header_accepted(self):
        status, _ = _request(self.port, "GET", "/api/state", token=self.token)
        self.assertEqual(status, 200)

    def test_token_comparison_is_constant_time(self):
        """_valid_token must delegate to secrets.compare_digest (not ==)."""
        from types import SimpleNamespace
        import secrets as _secrets
        h = SimpleNamespace(
            path=f"/api/state?token={self.token}",
            headers={},
            server=SimpleNamespace(expected_token=self.token),
        )
        with patch("services.config_server.secrets.compare_digest",
                   wraps=_secrets.compare_digest) as spy:
            result = config_server._Handler._valid_token(h)
            self.assertTrue(result)
            spy.assert_called_once_with(self.token, self.token)


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
                f"/api/fetch-models?token={self.token}",
                body='{"a": 1}',  # 7 bytes > 2
            )
        self.assertEqual(status, 413)

    def test_put_oversized_body_returns_413(self):
        with patch("services.config_server.MAX_BODY_BYTES", 2):
            status, _ = _request(
                self.port, "PUT",
                f"/api/state?token={self.token}",
                body='{"a": 1}',
            )
        self.assertEqual(status, 413)

    def test_post_normal_body_still_works(self):
        with patch("services.config_server.MAX_BODY_BYTES", 10_000_000):
            with patch("mpconf.config_store.sp_load_raw", return_value={"providers": {}}):
                status, _ = _request(
                    self.port, "POST",
                    f"/api/fetch-models?token={self.token}",
                    body='{"provider": "x"}',
                )
        self.assertEqual(status, 200)

    def test_post_invalid_content_length_returns_400(self):
        """Non-numeric Content-Length must return 400, not crash the handler."""
        import socket
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        sock.sendall(
            f"POST /api/fetch-models?token={self.token} HTTP/1.1\r\n"
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
            f"POST /api/fetch-models?token={self.token} HTTP/1.1\r\n"
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

    def _setup_configs(self, mp_cfg=None, sp_cfg=None):
        d = tempfile.mkdtemp()
        self._tmpdirs.append(tempfile.TemporaryDirectory())
        mp_path = os.path.join(d, "mp.json")
        sp_path = os.path.join(d, "sp.yaml")
        if mp_cfg:
            with open(mp_path, "w") as f:
                json.dump(mp_cfg, f)
        if sp_cfg:
            import yaml
            with open(sp_path, "w") as f:
                yaml.dump(sp_cfg, f)
        return mp_path, sp_path

    def test_get_state_returns_json(self):
        status, data = _request(self.port, "GET", f"/api/state?token={self.token}")
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
        status, _ = _request(self.port, "GET", f"/api/nonexistent?token={self.token}")
        self.assertEqual(status, 404)

    def test_html_served_at_root(self):
        status, data = _request(self.port, "GET", f"/?token={self.token}")
        self.assertEqual(status, 200)
        self.assertIn("<html", data.lower())


class TestPutState(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_put_invalid_json_returns_400(self):
        status, _ = _request(self.port, "PUT", f"/api/state?token={self.token}",
                             body="not json")
        self.assertEqual(status, 400)

    def test_put_empty_body_returns_400(self):
        status, _ = _request(self.port, "PUT", f"/api/state?token={self.token}")
        self.assertEqual(status, 400)

    def test_put_calls_write_mp_and_sp(self):
        with patch("services.config_server._write_mp", return_value=[]) as wmp, \
             patch("mpconf.config_store.sp_save", return_value=(True, None)) as wsp:
            body = json.dumps({"mp": {"tunnels": []}, "sp": {"providers": {}}})
            status, data = _request(self.port, "PUT", f"/api/state?token={self.token}",
                                    body=body)
        self.assertEqual(status, 200)
        wmp.assert_called_once()
        wsp.assert_called_once()

    def test_put_with_errors_returns_422(self):
        with patch("services.config_server._write_mp", return_value=["write failed"]):
            body = json.dumps({"mp": {}})
            status, data = _request(self.port, "PUT", f"/api/state?token={self.token}",
                                    body=body)
        self.assertEqual(status, 422)
        parsed = json.loads(data)
        self.assertFalse(parsed["ok"])
        self.assertIn("write failed", parsed["errors"])


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
        t = f"?token={self.token}" if token else ""
        return _request(self.port, "POST", f"/api/fetch-models{t}", body=body)

    def test_post_requires_token(self):
        status, _ = self._post('{"provider": "deepseek"}', token=False)
        self.assertEqual(status, 403)

    def test_post_unknown_route_404(self):
        status, _ = _request(self.port, "POST", f"/api/nope?token={self.token}",
                             body="{}")
        self.assertEqual(status, 404)

    def test_post_invalid_json_400(self):
        status, _ = self._post("not json")
        self.assertEqual(status, 400)

    def test_fetch_models_success_through_handler(self):
        body = json.dumps({"data": [{"id": "deepseek-v4-flash"}]}).encode()
        with patch("mpconf.config_store.sp_load_raw", return_value=self.SP), \
             patch("services.authenticated_http.AuthenticatedHttpClient.open",
                   return_value=body):
            status, data = self._post('{"provider": "deepseek"}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), {"models": ["deepseek-v4-flash"]})

    def test_fetch_models_unknown_provider_returns_error(self):
        with patch("mpconf.config_store.sp_load_raw", return_value=self.SP):
            status, data = self._post('{"provider": "ghost"}')
        self.assertEqual(status, 200)
        self.assertIn("error", json.loads(data))


class TestRestoreKey(unittest.TestCase):
    def test_untouched_key_restored(self):
        # api_key_set=True + no new value → keep the existing key.
        from suanpan.config import _restore_key
        result = _restore_key(None, "original-key", keep=True)
        self.assertEqual(result, "original-key")

    def test_new_key_kept(self):
        from suanpan.config import _restore_key
        result = _restore_key("sk-newkey123", "old-key", keep=True)
        self.assertEqual(result, "sk-newkey123")

    def test_cleared_when_not_flagged(self):
        # api_key_set=False + no new value → the key is cleared.
        from suanpan.config import _restore_key
        result = _restore_key(None, "old-key", keep=False)
        self.assertIsNone(result)


class TestReadMpEmpty(unittest.TestCase):
    def test_empty_config_returns_empty_dict(self):
        with patch.object(config_server, "load_config", return_value=None), \
             patch.object(config_server, "merge_config", return_value=None):
            self.assertEqual(config_server._read_mp(), {})


class TestWriteMpKeychainGuard(unittest.TestCase):
    """#40: only an explicit auth_type switch may delete keychain passwords.

    A partial tunnel payload (no auth_type key) must not silently wipe the
    saved password; neither must an untouched auth_type=password tunnel.
    """

    _tunnel_base = {"name": "t", "ssh_host": "h", "ssh_user": "u", "ssh_port": 22}

    def _write(self, tunnels):
        cfg = {"tunnels": tunnels}
        with patch("services.config_server.save_config", return_value=True), \
             patch("services.config_server.keychain.delete_password") as delete, \
             patch("services.config_server.keychain.set_password", return_value=True):
            errors = config_server._write_mp(cfg)
        return errors, delete

    def test_partial_payload_without_auth_type_keeps_password(self):
        errors, delete = self._write([dict(self._tunnel_base)])
        self.assertEqual(errors, [])
        delete.assert_not_called()

    def test_explicit_switch_to_key_deletes_password(self):
        t = dict(self._tunnel_base, auth_type="key")
        errors, delete = self._write([t])
        self.assertEqual(errors, [])
        delete.assert_called_once_with(t)

    def test_explicit_password_keeps_password(self):
        errors, delete = self._write([dict(self._tunnel_base, auth_type="password")])
        self.assertEqual(errors, [])
        delete.assert_not_called()


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
                                    f"/api/balance?token={self.token}")
        self.assertEqual(status, 200)
        self.assertIsInstance(json.loads(data), list)

    def test_usage_endpoint_returns_json(self):
        cfg = {"usage_log": {"path": "/nonexistent/usage.jsonl"}}
        with patch.object(config_server.config_store, "sp_load_raw", return_value=cfg):
            status, data = _request(self.port, "GET",
                                    f"/api/usage?token={self.token}")
        self.assertEqual(status, 200)
        payload = json.loads(data)
        self.assertIsInstance(payload, dict)
        self.assertIn("daily", payload)
        self.assertIn("scenarios", payload)

    def test_usage_endpoint_accepts_supported_ranges(self):
        cfg = {"usage_log": {"path": "/nonexistent/usage.jsonl"}}
        with patch.object(config_server.config_store, "sp_load_raw", return_value=cfg):
            for usage_range in ("today", "7d", "all"):
                with self.subTest(usage_range=usage_range):
                    status, _ = _request(
                        self.port, "GET",
                        f"/api/usage?token={self.token}&range={usage_range}",
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
                    config_server.config_store, "sp_load_raw",
                    return_value={"usage_log": {"path": path}}):
                default_status, default_data = _request(
                    self.port, "GET", f"/api/usage?token={self.token}")
                all_status, all_data = _request(
                    self.port, "GET",
                    f"/api/usage?token={self.token}&range=all")
        finally:
            os.unlink(path)
        self.assertEqual(default_status, 200)
        self.assertEqual(all_status, 200)
        self.assertEqual(json.loads(default_data), json.loads(all_data))
        self.assertEqual(json.loads(default_data)["total"]["calls"], 1)

    def test_usage_endpoint_rejects_invalid_range(self):
        status, data = _request(
            self.port, "GET",
            f"/api/usage?token={self.token}&range=month",
        )
        self.assertEqual(status, 400)
        self.assertIn("range", json.loads(data)["error"])


class TestPutStateWrongPath(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def test_put_wrong_path_returns_404(self):
        status, data = _request(self.port, "PUT",
                                f"/api/wrong?token={self.token}",
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
    """/api/state carries a read-only capture_active flag from the injected getter."""

    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def _get_state(self):
        status, data = _request(self.port, "GET", f"/api/state?token={self.token}")
        self.assertEqual(status, 200)
        return json.loads(data)

    def test_default_is_false_without_getter(self):
        self.server._server.capture_state_fn = None
        self.assertIs(self._get_state()["mp"]["capture_active"], False)

    def test_stub_true_flows_through(self):
        self.server._server.capture_state_fn = lambda: True
        self.assertIs(self._get_state()["mp"]["capture_active"], True)

    def test_broken_getter_degrades_to_false(self):
        def boom():
            raise RuntimeError("no capture ctrl")
        self.server._server.capture_state_fn = boom
        with self.assertLogs("magic-proxy.config_server", level="ERROR"):
            parsed = self._get_state()
        self.assertIs(parsed["mp"]["capture_active"], False)

    def test_capture_active_never_round_trips_into_file(self):
        # The flag is server-injected; _write_mp must strip it before saving.
        with patch("services.config_server.save_config", return_value=True) as save, \
             patch("services.config_server.merge_config", side_effect=lambda c: c):
            config_server._write_mp({"capture_active": True, "tunnels": []})
        saved_cfg = save.call_args[0][0]
        self.assertNotIn("capture_active", saved_cfg)


class TestTestTunnelEndpoint(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def _post(self, body, token=True):
        t = f"?token={self.token}" if token else ""
        return _request(self.port, "POST", f"/api/test-tunnel{t}", body=body)

    def test_requires_token(self):
        status, _ = self._post('{"index": 0}', token=False)
        self.assertEqual(status, 403)

    def test_invalid_index_type_returns_400(self):
        for bad in ('{"index": "x"}', '{"index": true}', '{"index": null}', '{}', '"str"'):
            with self.subTest(body=bad):
                status, data = self._post(bad)
                self.assertEqual(status, 400)
                self.assertFalse(json.loads(data)["ok"])

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
    """Unit tests for config_server.test_tunnel — all subprocesses mocked."""

    _KEY_TUNNEL = {
        "ssh_host": "example.com", "ssh_user": "u", "ssh_port": 2222,
        "auth_type": "key", "ssh_key": "~/.ssh/id_ed25519",
    }
    _PW_TUNNEL = {
        "ssh_host": "example.com", "ssh_user": "u", "ssh_port": 22,
        "auth_type": "password",
    }

    @staticmethod
    def _proc(returncode=0, stderr=""):
        from types import SimpleNamespace
        return SimpleNamespace(returncode=returncode,
                               stderr=stderr.encode("utf-8"))

    def test_missing_host_is_rejected_without_subprocess(self):
        with patch.object(config_server.subprocess, "run") as run:
            result = config_server.test_tunnel({"ssh_host": "  ", "ssh_port": 22})
        run.assert_not_called()
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
             patch.object(config_server.subprocess, "run") as run:
            result = config_server.test_tunnel(self._PW_TUNNEL)
        run.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("密码", result["error"])

    def test_key_auth_success_uses_batchmode_and_known_hosts(self):
        with patch.object(config_server.subprocess, "run",
                          return_value=self._proc(0)) as run:
            result = config_server.test_tunnel(self._KEY_TUNNEL)
        self.assertEqual(result, {"ok": True})
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "ssh")
        self.assertIn("BatchMode=yes", cmd)
        self.assertIn("ConnectTimeout=5", cmd)
        self.assertIn("StrictHostKeyChecking=yes", cmd)
        self.assertIn(
            f"UserKnownHostsFile={config_server.host_key.KNOWN_HOSTS_PATH}", cmd)
        self.assertIn("~/.ssh/id_ed25519", cmd)
        self.assertEqual(cmd[-2:], ["u@example.com", "true"])
        # Key auth must NOT go through sshpass.
        self.assertNotIn("sshpass", cmd)

    def test_password_auth_success_uses_sshpass_fd(self):
        with patch.object(config_server.keychain, "get_password",
                          return_value="sekrit"), \
             patch.object(config_server.subprocess, "run",
                          return_value=self._proc(0)) as run:
            result = config_server.test_tunnel(self._PW_TUNNEL)
        self.assertEqual(result, {"ok": True})
        kwargs = run.call_args[1]
        self.assertTrue(kwargs.get("pass_fds"), "password fd must be passed")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "sshpass")
        self.assertEqual(cmd[1], "-d")
        # The password itself must never appear in argv.
        self.assertNotIn("sekrit", cmd)
        self.assertIn("NumberOfPasswordPrompts=1", cmd)
        self.assertNotIn("BatchMode=yes", cmd)

    def test_timeout_returns_chinese_phrase(self):
        with patch.object(config_server.subprocess, "run",
                          side_effect=config_server.subprocess.TimeoutExpired(
                              cmd="ssh", timeout=15)):
            result = config_server.test_tunnel(self._KEY_TUNNEL)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "连接超时")

    def test_missing_binary_returns_oserror_phrase(self):
        with patch.object(config_server.subprocess, "run",
                          side_effect=FileNotFoundError("sshpass")):
            result = config_server.test_tunnel(self._KEY_TUNNEL)
        self.assertFalse(result["ok"])
        self.assertIn("无法启动", result["error"])

    def test_missing_sshpass_mentions_password_hint(self):
        with patch.object(config_server.keychain, "get_password",
                          return_value="sekrit"), \
             patch.object(config_server.subprocess, "run",
                          side_effect=FileNotFoundError("sshpass")):
            result = config_server.test_tunnel(self._PW_TUNNEL)
        self.assertFalse(result["ok"])
        self.assertIn("sshpass", result["error"])

    def test_host_key_changed_maps_before_verification_failed(self):
        stderr = ("@@@@ REMOTE HOST IDENTIFICATION HAS CHANGED! @@@@\n"
                  "Host key verification failed.")
        with patch.object(config_server.subprocess, "run",
                          return_value=self._proc(255, stderr)):
            result = config_server.test_tunnel(self._KEY_TUNNEL)
        self.assertFalse(result["ok"])
        self.assertIn("主机密钥已变更", result["error"])

    def test_host_key_untrusted_mapping(self):
        with patch.object(config_server.subprocess, "run",
                          return_value=self._proc(255, "Host key verification failed.")):
            result = config_server.test_tunnel(self._KEY_TUNNEL)
        self.assertFalse(result["ok"])
        self.assertIn("主机密钥未信任", result["error"])

    def test_permission_denied_mapping(self):
        with patch.object(config_server.subprocess, "run",
                          return_value=self._proc(255, "u@example.com: Permission denied (publickey).")):
            result = config_server.test_tunnel(self._KEY_TUNNEL)
        self.assertFalse(result["ok"])
        self.assertIn("认证失败", result["error"])

    def test_unknown_failure_includes_first_stderr_line(self):
        with patch.object(config_server.subprocess, "run",
                          return_value=self._proc(255, "some exotic failure\nsecond line")):
            result = config_server.test_tunnel(self._KEY_TUNNEL)
        self.assertFalse(result["ok"])
        self.assertIn("some exotic failure", result["error"])

    def test_none_stderr_degrades_gracefully(self):
        from types import SimpleNamespace
        with patch.object(config_server.subprocess, "run",
                          return_value=SimpleNamespace(returncode=255, stderr=None)):
            result = config_server.test_tunnel(self._KEY_TUNNEL)
        self.assertFalse(result["ok"])
        self.assertIn("未知错误", result["error"])

    def test_non_utf8_stderr_does_not_crash(self):
        with patch.object(config_server.subprocess, "run",
                          return_value=self._proc(255)) as run:
            from types import SimpleNamespace
            run.return_value = SimpleNamespace(returncode=255,
                                               stderr=b"\xff\xfe broken bytes")
            result = config_server.test_tunnel(self._KEY_TUNNEL)
        self.assertFalse(result["ok"])


class TestCaptureCleanEndpoint(unittest.TestCase):
    def setUp(self):
        self.server, self.port = _start_server()
        self.token = self.server._token

    def tearDown(self):
        self.server.stop()

    def _post(self):
        return _request(self.port, "POST",
                        f"/api/capture-clean?token={self.token}", body="{}")

    def test_requires_token(self):
        status, _ = _request(self.port, "POST", "/api/capture-clean",
                             body="{}")
        self.assertEqual(status, 403)

    def test_empty_body_returns_400(self):
        # Client contract (mirrors the other POST endpoints): a JSON body is
        # mandatory — config_ui's cleanCapture() sends '{}'.
        status, _ = _request(self.port, "POST",
                             f"/api/capture-clean?token={self.token}")
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
