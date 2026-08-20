"""Final push: config_server routes + sys_proxy sync + proxy SSHMonitor + main middleware."""
import json
import os
import socket
import struct
import subprocess
import threading
import time
import unittest
from unittest.mock import patch, MagicMock, AsyncMock, PropertyMock

# ── config_server.py: missing handler routes + lifecycle ──────────
from http.server import HTTPServer
from services.config_server import ConfigServer, _Handler, CONFIG_PORT


class TestConfigServerLifecycle(unittest.TestCase):
    def test_start_and_stop(self):
        cs = ConfigServer(port=0)  # port 0 = OS picks free port
        # Can't easily bind port 0 with current API; test start on real port
        cs2 = ConfigServer(port=19876)
        self.assertTrue(cs2.start())
        self.assertTrue(cs2.running)
        cs2.stop()
        self.assertFalse(cs2.running)

    def test_start_when_already_running(self):
        cs = ConfigServer(port=19877)
        cs.start()
        self.assertTrue(cs.start())  # idempotent
        cs.stop()


class TestConfigServerRoutes(unittest.TestCase):
    """Test handler routes via real HTTP on a test port."""
    _server = None
    _port = 0

    @classmethod
    def setUpClass(cls):
        cls._port = 19878
        from services.config_server import ConfigServer
        cls._server = ConfigServer(port=cls._port)
        cls._server.start()
        time.sleep(0.3)

    @classmethod
    def tearDownClass(cls):
        if cls._server:
            cls._server.stop()

    def _get(self, path):
        import urllib.request
        url = f"http://127.0.0.1:{self._port}{path}?token={self._server.token}"
        try:
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_get_balance_route(self):
        with patch("services.config_server.fetch_balance", return_value=[]):
            status, body = self._get("/api/balance")
        self.assertEqual(status, 200)

    def test_get_usage_route(self):
        with patch("services.config_server.fetch_usage", return_value={"total": {}}):
            status, body = self._get("/api/usage")
        self.assertEqual(status, 200)

    def test_get_unknown_route(self):
        status, body = self._get("/api/nonexistent")
        self.assertEqual(status, 404)

    def test_post_test_provider(self):
        import urllib.request
        url = f"http://127.0.0.1:{self._port}/api/test-provider?token={self._server.token}"
        with patch("services.config_server.test_provider", return_value={"ok": True}):
            req = urllib.request.Request(url, data=json.dumps({"provider": "p"}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                body = json.loads(r.read())
        self.assertTrue(body["ok"])

    def test_put_state(self):
        import urllib.request
        url = f"http://127.0.0.1:{self._port}/api/state?token={self._server.token}"
        from mpconf.config_state import CommitPlan, SaveResult
        with patch("services.config_server.ConfigStateStore") as store_cls, \
             patch("mpconf.config_store.sp_save", return_value=(True, None)) as mock_ws:
            store_cls.return_value.prepare.return_value = CommitPlan(
                True, [], {"tunnels": []}, {"providers": {}})
            store_cls.return_value.commit.return_value = SaveResult(True, None, [])
            data = json.dumps({"mp": {"tunnels": []}, "sp": {"providers": {}}}).encode()
            req = urllib.request.Request(url, data=data, method="PUT",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                body = json.loads(r.read())
        self.assertTrue(body["ok"])

    def test_put_with_errors(self):
        import urllib.request
        url = f"http://127.0.0.1:{self._port}/api/state?token={self._server.token}"
        from mpconf.config_state import CommitPlan
        with patch("services.config_server.ConfigStateStore") as store_cls:
            store_cls.return_value.prepare.return_value = CommitPlan(
                False, ["error1"])
            data = json.dumps({"mp": {"tunnels": []}}).encode()
            req = urllib.request.Request(url, data=data, method="PUT",
                                         headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError as e:
                body = json.loads(e.read())
                self.assertFalse(body["ok"])


# ── proxy.py: SSHMonitor password auth + socks5 errors ────────────
from tunnel import proxy
class TestSSHMonitorPasswordAuth(unittest.TestCase):
    @patch("tunnel.subprocess_monitor.subprocess.Popen")
    @patch("tunnel.subprocess_monitor.subprocess.run")
    def test_start_password_auth(self, mock_run, mock_popen):
        mock_popen.return_value = MagicMock(pid=12345)
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        monitor = proxy.SSHMonitor(line_sink=lambda _: None)
        monitor.start(
            {"ssh_host": "srv", "ssh_user": "u", "ssh_port": 22,
             "auth_type": "password"},
            1080, "secret")
        self.assertEqual(monitor.status, "connecting")


class TestSocks5Errors(unittest.IsolatedAsyncioTestCase):
    async def test_handshake_rejected(self):
        reader = MagicMock()
        reader.read = AsyncMock(return_value=b"\x05\xff")  # method=0xff = rejected
        reader.readexactly = AsyncMock(side_effect=[
            b"\x05\x00",  # auth response
        ])
        writer = MagicMock()
        writer.write = MagicMock()
        writer.drain = AsyncMock()
        with patch("asyncio.open_connection", new=AsyncMock(return_value=(reader, writer))):
            # First readexactly returns auth method, then read returns handshake
            reader.readexactly = AsyncMock(return_value=b"\x05\xff")
            with self.assertRaises(RuntimeError):
                await proxy.socks5_connect("example.com", 443, "127.0.0.1:1080")


# ── suanpan/main.py: BodyLimitMiddleware chunked path ─────────────
class TestBodyLimitChunked(unittest.TestCase):
    def test_rejects_chunked_oversize(self):
        from starlette.testclient import TestClient
        from suanpan.config import AppConfig, ProviderConfig, RouterConfig
        from suanpan.main import create_app
        config = AppConfig(
            body_limit_mb=1,
            providers={"p": ProviderConfig(
                base_url="https://x.com", api_key="k",
                auth_header="x-api-key", enabled=True, models=["m"])},
            router=RouterConfig(default="p/m"),
        )
        app = create_app(config)
        with TestClient(app) as client:
            # Send without Content-Length (forces chunked path)
            big = "x" * (2 * 1024 * 1024)
            r = client.post("/v1/messages",
                            content=big,
                            headers={"Content-Type": "application/json",
                                     "Transfer-Encoding": "chunked"})
            self.assertEqual(r.status_code, 413)


# ── sys_proxy_controller.py: sync() branches ──────────────────────
from sysctl.sys_proxy_controller import SystemProxyController


class TestSysProxySyncBranches(unittest.TestCase):
    @patch("sysctl.sys_proxy_controller.system_proxy")
    def test_sync_applies_when_desired(self, mock_sp):
        mock_sp.recover_stale_transaction.return_value = (False, "")
        mock_sp.snapshot.return_value = [{"webproxy": {"Enabled": "no"}}]
        mock_sp.apply_transaction.return_value = (True, "", {"applied": True})
        ctrl = SystemProxyController(
            ssh_monitor=MagicMock(status="connected"),
            capture_state=lambda: (False, ""),
            config_fn=lambda: {"http_listen_port": 8888, "capture_port": 8080,
                                "system_proxy_default": True},
            paused_fn=lambda: False,
            initial_on=True,
        )
        ctrl.sync()
        mock_sp.apply_transaction.assert_called_once()

    @patch("sysctl.sys_proxy_controller.system_proxy")
    def test_sync_releases_when_not_desired(self, mock_sp):
        mock_sp.recover_stale_transaction.return_value = (False, "")
        mock_sp.snapshot.return_value = [{"webproxy": {"Enabled": "yes"}}]
        mock_sp.release_transaction.return_value = (True, "")
        ctrl = SystemProxyController(
            ssh_monitor=MagicMock(status="stopped"),
            capture_state=lambda: (False, ""),
            config_fn=lambda: {"http_listen_port": 8888},
            paused_fn=lambda: False,
            initial_on=True,
        )
        ctrl._on = True
        ctrl._snapshot = {"webproxy": {"Enabled": "yes"}}
        ctrl._desired = {"webproxy": {"Enabled": "yes"}}
        ctrl.sync()
        mock_sp.release_transaction.assert_called_once()

    @patch("sysctl.sys_proxy_controller.system_proxy")
    def test_sync_apply_failure_sets_error(self, mock_sp):
        mock_sp.recover_stale_transaction.return_value = (False, "")
        mock_sp.snapshot.return_value = [{"webproxy": {"Enabled": "no"}}]
        mock_sp.apply_transaction.return_value = (False, "networksetup failed", None)
        ctrl = SystemProxyController(
            ssh_monitor=MagicMock(status="connected"),
            capture_state=lambda: (False, ""),
            config_fn=lambda: {"http_listen_port": 8888, "capture_port": 8080,
                                "system_proxy_default": True},
            paused_fn=lambda: False,
            initial_on=True,
        )
        ctrl.sync()
        self.assertTrue(ctrl.error)
