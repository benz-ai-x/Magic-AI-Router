"""Lightweight config server for the web-based settings UI.

Serves a single-page HTML config panel + JSON API for reading/writing both
Magic AI Router (~/.magic-proxy.json) and Suanpan (~/.suanpan.yaml) configs.
Uses stdlib http.server — no FastAPI/uvicorn dependency. Runs on 127.0.0.1:9528
in a daemon thread, always available while the app is running.

Security: validates Host header (DNS-rebinding guard), masks API keys in GET
responses (restores on write), validates Suanpan config before persisting.
"""
import json
import logging
import secrets
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

from sysctl import keychain
from mpconf import config_store
from mpconf.config_state import ConfigStateStore
from tunnel import ssh_launch
from services import claude_code_setup
from capture import capture_store
from mpconf.config import load_config, merge_config
from services.balance_usage import (
    USAGE_RANGES,
    fetch_balance,
    fetch_models,
    fetch_usage,
    test_provider,
)
from util import resource_path as _resource_path

logger = logging.getLogger("magic-proxy.config_server")

CONFIG_PORT = 9528
MAX_BODY_BYTES = 10 * 1024 * 1024  # 10 MB — reject oversized POST/PUT bodies
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}

# GET / 无 token 时的登录页（自包含 HTML，无外部资源）。填 token → JS 用
# Bearer 调 / 种 HttpOnly cookie → 成功则 reload 进配置页。仅 GET / 返回此页；
# /api/* 的 401 保持纯 JSON（curl/脚本客户端不期待 HTML）。macOS 桥接首导航
# 带 Bearer → 200 不经过此页，行为不变。
_LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Magic AI Router — 登录</title>
<style>
body{font-family:-apple-system,system-ui,sans-serif;background:#f5f5f7;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{background:#fff;border-radius:12px;padding:32px;box-shadow:0 4px 24px rgba(0,0,0,.08);width:320px}
h1{font-size:18px;margin:0 0 8px}p{font-size:13px;color:#666;margin:0 0 20px}
input{width:100%;padding:10px;border:1px solid #d2d2d7;border-radius:8px;font-size:14px;box-sizing:border-box;margin-bottom:12px}
button{width:100%;padding:10px;background:#007aff;color:#fff;border:0;border-radius:8px;font-size:14px;cursor:pointer}
button:disabled{background:#ccc;cursor:default}.err{color:#ff3b30;font-size:13px;margin-top:8px;display:none}
</style></head><body>
<div class="card">
<h1>Magic AI Router</h1>
<p>输入配置页面的访问 token（Docker 版用 <code>suanpan.sh config-ui</code> 查看）</p>
<input id="tok" type="password" placeholder="token" autocomplete="off" autofocus>
<button id="go">进入</button>
<div class="err" id="err">token 无效，请重试</div>
</div>
<script>
const tok=document.getElementById('tok'),go=document.getElementById('go'),err=document.getElementById('err');
async function login(){
  const t=tok.value.trim();if(!t)return;
  go.disabled=true;err.style.display='none';
  try{
    const r=await fetch('/',{headers:{Authorization:'Bearer '+t}});
    if(r.ok){location.reload();return;}
  }catch(e){}
  err.style.display='block';go.disabled=false;tok.select();
}
go.onclick=login;
tok.onkeydown=e=>{if(e.key==='Enter')login();};
</script></body></html>"""


def _read_mp():
    try:
        cfg = merge_config(load_config())
    except Exception:
        # 迁移可行动错误等：降级为带 _load_error 的空态供 UI 提示，
        # /api/state 不 500（UI 保存被 validateConfig/prepare 双带阻断）
        import logging as _lg
        _lg.getLogger("magic-proxy.config_server").exception("_read_mp degraded")
        return {"_load_error": "Magic Proxy 配置装载失败，已阻止保存以防覆盖"}
    if not cfg:
        return {}
    for t in cfg.get("tunnels", []):
        t["has_password"] = bool(
            t.get("auth_type") == "password" and keychain.get_password(t))
    return cfg


def test_tunnel(tunnel):
    """One-shot SSH reachability probe for one saved tunnel config.

    本函数只持有端点职责：输入校验（地址/端口/选项注入守卫）与 Keychain
    取密码；SSH 调用策略与真实隧道完全同源（tunnel/ssh_launch.probe，
    含超时上限）——绿结果意味着隧道本身会连上，未信任主机快速失败，
    绝不自动信任。

    Returns {"ok": True} or {"ok": False, "error": "<中文短语>"} — never raises.
    """
    host = str(tunnel.get("ssh_host") or "").strip()
    user = str(tunnel.get("ssh_user") or "").strip()
    try:
        port = int(tunnel.get("ssh_port", 22))
    except (TypeError, ValueError):
        port = 0
    destination = f"{user}@{host}" if user else host
    if not host or not 1 <= port <= 65535 or destination.startswith("-"):
        return {"ok": False, "error": "隧道地址或端口无效"}

    password = ""
    if tunnel.get("auth_type") == "password":
        password = keychain.get_password(tunnel)
        if not password:
            return {"ok": False, "error": "钥匙串中没有该隧道的密码，请先保存"}

    # 探针消费校验归一后的值（strip/int），与历史行为一致——手改配置的
    # 空白 host 或 "022" 端口不进 ssh argv。
    normalized = dict(tunnel, ssh_host=host, ssh_user=user, ssh_port=port)
    return ssh_launch.probe(normalized, password=password)


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    """Carries the per-server callback refs on the INSTANCE (not class
    attributes): parallel ConfigServers in tests can never cross-talk, and
    tests construct one directly without replicating start() internals."""
    daemon_threads = True

    def __init__(self, address, handler, *, expected_token=None,
                 on_sp_saved=None, capture_state_fn=None):
        self.expected_token = expected_token
        self.on_sp_saved = on_sp_saved
        self.capture_state_fn = capture_state_fn
        super().__init__(address, handler)


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _valid_token(self):
        """Validate bearer token from Authorization header or session cookie.

        issue #10：URL 永不带凭证——query-string 认证路径已删除。首屏
        导航由桥接构造带 Authorization 头的请求；其响应种下 HttpOnly
        的 cfgsess 会话 cookie，此后 JS 同源 fetch 自动携带（token
        不进 URL/JS/日志）。常量时间比较保留。
        """
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        if not token:
            cookie_header = self.headers.get("Cookie", "")
            for part in cookie_header.split(";"):
                name, _, value = part.strip().partition("=")
                if name == "cfgsess":
                    token = value
                    break
        expected = self.server.expected_token
        if not token or not expected:
            return False
        return secrets.compare_digest(token, expected)

    def _valid_host(self):
        """Reject non-loopback Host headers (DNS-rebinding guard)."""
        host = self.headers.get("Host", "").rsplit(":", 1)[0].strip("[]")
        return host in _ALLOWED_HOSTS

    def _send(self, code, body, ctype="application/json; charset=utf-8",
              extra_headers=()):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for name, value in extra_headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def _read_json_body(self):
        """Read and parse JSON body with a size cap (MAX_BODY_BYTES).

        Returns parsed data on success, or None if an error response was
        already sent (400 invalid JSON / 413 too large).
        """
        try:
            length = int(self.headers.get("Content-Length", 0))
        except (ValueError, TypeError):
            self._json(400, {"error": "invalid Content-Length"})
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            self.close_connection = True
            self._json(413, {"error": "body too large"})
            return None
        try:
            return json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, ValueError):
            self._json(400, {"error": "invalid JSON"})
            return None

    def do_GET(self):
        if not self._valid_host():
            self._json(403, {"error": "forbidden"})
            return
        parsed_url = urlparse(self.path)
        path = parsed_url.path
        # Browsers auto-request /favicon.ico without the token; answer 204
        # instead of 403 so the console stays clean. WKWebView never asks.
        if path == "/favicon.ico":
            self._send(204, b"")
            return
        # agent.md is public (loopback-only, no token) so AI agents can read
        # product context without the user's bearer token.
        if path == "/agent.md":
            try:
                txt = open(_resource_path("agent.md"), encoding="utf-8").read()
                self._send(200, txt, "text/markdown; charset=utf-8")
            except OSError:
                self._json(404, {"error": "agent.md not found"})
            return
        if not self._valid_token():
            # GET / 的 401 返回登录页（浏览器直接打开可用）；API 路径仍 JSON
            if path in ("/", "/index.html"):
                self._send(401, _LOGIN_HTML, "text/html; charset=utf-8")
            else:
                self._json(401, {"error": "unauthorized"})
            return
        if path in ("/", "/index.html"):
            try:
                html = open(_resource_path("config_ui.html"), encoding="utf-8").read()
                extra = []
                auth = self.headers.get("Authorization", "")
                if auth.startswith("Bearer "):
                    # 桥接构造的首导航（header 呈现）→ 种 HttpOnly 会话
                    # cookie，刷新与后续 fetch 不再依赖 header
                    extra.append((
                        "Set-Cookie",
                        f"cfgsess={self.server.expected_token}; Path=/; "
                        "HttpOnly; SameSite=Strict"))
                self._send(200, html, "text/html; charset=utf-8", extra_headers=extra)
            except OSError:
                self._json(404, {"error": "config_ui.html not found"})
        elif path == "/api/state":
            mp = _read_mp()
            sp = config_store.sp_load_masked()
            # Read-only runtime status injected for the config UI; ConfigStateStore
            # strips it again so it can never round-trip into the file.
            try:
                fn = self.server.capture_state_fn
                mp["capture_active"] = bool(fn and fn())
            except Exception:
                logger.exception("capture_state_fn failed")
                mp["capture_active"] = False
            self._json(200, {"mp": mp, "sp": sp})
        elif path == "/api/balance":
            self._json(200, fetch_balance(config_store.sp_load_raw()))
        elif path == "/api/usage":
            usage_range = parse_qs(
                parsed_url.query, keep_blank_values=True
            ).get("range", ["all"])[0]
            if usage_range not in USAGE_RANGES:
                self._json(400, {"error": "invalid range"})
                return
            self._json(200, fetch_usage(
                config_store.sp_load_raw(), usage_range))
        elif path == "/api/cc-default-roles":
            self._json(200, claude_code_setup.default_roles())
        elif path == "/api/provider-templates":
            # #51：UI 供应商模板单一真源 = PROVIDER_REGISTRY（Python 侧）
            from mpconf.provider_auth import PROVIDER_REGISTRY
            templates = [
                {"id": name, "label": entry["label"],
                 "base_url": entry["base_url"],
                 "anthropic_native": entry["anthropic_native"]}
                for name, entry in PROVIDER_REGISTRY.items()]
            templates.append({"id": "custom", "label": "自定义"})
            self._json(200, templates)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._valid_host() or not self._valid_token():
            self._json(401, {"error": "unauthorized"})
            return
        path = urlparse(self.path).path
        if path not in ("/api/fetch-models", "/api/test-provider", "/api/setup-claude-code",
                        "/api/cc-sync-preview", "/api/test-tunnel", "/api/capture-clean"):
            self._json(404, {"error": "not found"})
            return
        data = self._read_json_body()
        if data is None:
            return
        if path == "/api/fetch-models":
            self._json(200, fetch_models(config_store.sp_load_raw(), str(data.get("provider", ""))))
        elif path == "/api/cc-sync-preview":
            roles = data.get("roles")  # {key: {model, ctx_1m}} or None
            self._json(200, claude_code_setup.preview(roles=roles))
        elif path == "/api/setup-claude-code":
            roles = data.get("roles")  # {key: {model, ctx_1m}} or None
            self._json(200, claude_code_setup.setup(roles=roles))
        elif path == "/api/test-tunnel":
            code, payload = self._test_tunnel(data)
            self._json(code, payload)
        elif path == "/api/capture-clean":
            self._json(200, self._capture_clean())
        else:
            self._json(200, test_provider(
                config_store.sp_load_raw(), str(data.get("provider", "")),
                data.get("model")))

    def _test_tunnel(self, data):
        """POST /api/test-tunnel {index} → probe saved tunnels[index].

        Returns (http_code, payload): 400 for bad index / no tunnels, 200
        with {"ok": bool, "error"?: str} once the probe actually runs.
        """
        if not isinstance(data, dict):
            return 400, {"ok": False, "error": "无效的请求体"}
        idx = data.get("index")
        if isinstance(idx, bool) or not isinstance(idx, int):
            return 400, {"ok": False, "error": "无效的隧道索引"}
        cfg = _read_mp()
        tunnels = cfg.get("tunnels", []) if isinstance(cfg, dict) else []
        if not tunnels:
            return 400, {"ok": False, "error": "尚未配置隧道"}
        if not 0 <= idx < len(tunnels):
            return 400, {"ok": False, "error": "隧道索引越界"}
        return 200, test_tunnel(tunnels[idx])

    def _capture_clean(self):
        """POST /api/capture-clean → empty the capture dir (keep the dir)."""
        cfg = _read_mp()
        capture_dir = cfg.get("capture_dir") if isinstance(cfg, dict) else None
        try:
            removed = capture_store.clean(capture_dir)
        except OSError as exc:
            logger.warning("capture clean failed: %s", exc)
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "removed": removed}

    def do_PUT(self):
        if not self._valid_host() or not self._valid_token():
            self._json(401, {"error": "unauthorized"})
            return
        if urlparse(self.path).path != "/api/state":
            self._json(404, {"error": "not found"})
            return
        data = self._read_json_body()
        if data is None:
            return
        mp_in = data.get("mp")
        sp_in = data.get("sp")
        if not isinstance(mp_in, dict):
            mp_in = None
        if not isinstance(sp_in, dict):
            sp_in = None
        if mp_in is None and sp_in is None:
            self._json(200, {"ok": True})
            return
        store = ConfigStateStore(keychain=keychain)
        plan = store.prepare(mp=mp_in, sp=sp_in)
        if not plan.ok:
            self._json(422, {"ok": False, "errors": plan.errors})
            return
        # on_sp_saved 只在完整提交后（含 MP 段成功）触发
        result = store.commit(
            plan,
            on_committed=(self.server.on_sp_saved
                          if (sp_in is not None
                              and self.server.on_sp_saved) else None))
        if not result.ok:
            self._json(422, {"ok": False, "errors": result.errors})
        else:
            self._json(200, {"ok": True})


class ConfigServer:
    """Background HTTP server for the config UI.

    bind_host / token 是构造参数（Docker 适配的 seam）：默认绑
    127.0.0.1 + 自造随机 token（macOS 行为）；容器形态传
    bind_host="0.0.0.0" + 配置卷里的固定 token。
    """

    def __init__(self, on_sp_saved=None, port=CONFIG_PORT, capture_state=None,
                 bind_host="127.0.0.1", token=None):
        self._port = port
        self._bind_host = bind_host
        self._server = None
        self._thread = None
        self._token = token if token is not None else secrets.token_hex(16)
        self._on_sp_saved = on_sp_saved
        # Optional getter → bool ("capture mode actually running now");
        # injected by app.py, stubbed in tests. None ⇒ /api/state reports False.
        self._capture_state = capture_state

    @property
    def token(self):
        return self._token

    @property
    def running(self):
        return bool(self._thread and self._thread.is_alive())

    @property
    def url(self):
        return f"http://127.0.0.1:{self._port}/"

    def start(self):
        """Start the server. Returns True on success, False if port unavailable."""
        if self.running:
            return True
        try:
            self._server = _ThreadingHTTPServer(
                (self._bind_host, self._port), _Handler,
                expected_token=self._token,
                on_sp_saved=self._on_sp_saved,
                capture_state_fn=self._capture_state)
        except OSError:
            logger.warning("Config server: port %d unavailable", self._port)
            return False
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="ConfigServer", daemon=True)
        self._thread.start()
        return True

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
