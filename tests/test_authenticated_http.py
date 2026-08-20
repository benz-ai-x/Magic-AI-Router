"""AuthenticatedHttpClient（issue #4）：认证请求的统一出站策略.

Seam S1 —— 认证请求经此 adapter 出站：跨 origin（scheme/host/effective
port 任一变化）重定向一律拒绝、HTTPS→HTTP 降级必拒、同 origin 显式放行、
响应上限与超时集中定义。端口占用只是发现线索（那是 #3）；这里管的是
**凭证绝不出原始 origin**。
"""
import http.server
import json
import threading
import unittest

from services.authenticated_http import (
    AuthRedirectError,
    AuthenticatedHttpClient,
)


class _RecordingServer:
    """真实本地 HTTP server：记录请求头，可配置一条重定向。"""

    def __init__(self, redirect_to=None, status=200, body=b"{}"):
        seen = []
        handler = _make_handler(redirect_to, status, body, seen)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.seen = seen
        self.port = self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def url(self, path="/x"):
        return f"http://127.0.0.1:{self.port}{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def _make_handler(redirect_to, status, body, seen):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append({"path": self.path,
                         "authorization": self.headers.get("Authorization"),
                         "x_api_key": self.headers.get("x-api-key")})
            if redirect_to is not None and self.path == "/x":
                host = self.headers.get("Host", "")
                target = (f"http://{host}{redirect_to}"
                          if redirect_to.startswith("/") else redirect_to)
                self.send_response(307)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            payload = body
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *a):
            pass
    return H


class TestRedirectPolicy(unittest.TestCase):
    def setUp(self):
        self.client = AuthenticatedHttpClient()

    def test_cross_origin_redirect_rejected_and_secret_never_reaches_second_server(self):
        b = _RecordingServer()
        a = _RecordingServer(redirect_to=b.url("/steal"))
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        with self.assertRaises(AuthRedirectError):
            self.client.open_json(
                a.url(), headers={"Authorization": "Bearer SECRET"})
        self.assertEqual(len(b.seen), 0,
                         "第二个 server 必须一个请求都收不到")
        self.assertEqual(a.seen[0]["authorization"], "Bearer SECRET")

    def test_same_origin_redirect_followed_with_credentials(self):
        # 同一 server 自跳 /x → /final（同 scheme/host/port 即同 origin）
        server = _RecordingServer(redirect_to="/final")
        self.addCleanup(server.close)
        data = self.client.open_json(
            server.url(), headers={"Authorization": "Bearer SECRET"})
        self.assertEqual(data, {})
        final = [s for s in server.seen if s["path"] == "/final"]
        self.assertEqual(len(final), 1)
        self.assertEqual(final[0]["authorization"], "Bearer SECRET",
                         "同 origin 重定向保住凭证")

    def test_https_to_http_downgrade_always_rejected(self):
        # handler 直测：无法本地起真 https，降级判定在 handler 单元层钉死
        from services.authenticated_http import _AuthRedirectHandler
        import urllib.request
        h = _AuthRedirectHandler("https://s.test")
        req = urllib.request.Request("https://s.test/x",
                                     headers={"Authorization": "Bearer k"})
        with self.assertRaises(AuthRedirectError):
            h.redirect_request(req, None, 302, "Found",
                               {}, "http://s.test/x")

    def test_x_api_key_same_policy_as_authorization(self):
        b = _RecordingServer()
        a = _RecordingServer(redirect_to=b.url("/steal"))
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        with self.assertRaises(AuthRedirectError):
            self.client.open_json(a.url(), headers={"x-api-key": "SECRET"})
        self.assertEqual(len(b.seen), 0)


if __name__ == "__main__":
    unittest.main()
