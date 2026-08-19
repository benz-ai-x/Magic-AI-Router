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
import os
import secrets
import subprocess
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlparse

import keychain
import config_store
import host_key
import claude_code_setup
import capture_store
from config import load_config, save_config, merge_config
from balance_usage import (
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


def _read_mp():
    cfg = merge_config(load_config())
    if not cfg:
        return {}
    for t in cfg.get("tunnels", []):
        t["has_password"] = bool(
            t.get("auth_type") == "password" and keychain.get_password(t))
    return cfg


def _write_mp(cfg):
    """Write Magic AI Router config. Returns list of error strings (empty = ok).

    The file is saved FIRST; keychain writes happen only after the config is
    durable — a failed save must not orphan passwords for unsaved tunnels.
    """
    pending_set, pending_del = [], []
    # Server-injected read-only fields must never round-trip into the file.
    cfg.pop("capture_active", None)
    for i, t in enumerate(cfg.get("tunnels", [])):
        pw = t.pop("password", None)
        t.pop("has_password", None)
        if pw:
            pending_set.append((i, t, pw))
        # #40: only an EXPLICIT auth_type switch away from password may
        # delete the keychain entry — a partial payload that omits
        # auth_type must not silently wipe a saved password.
        if "auth_type" in t and t.get("auth_type") != "password":
            pending_del.append(t)
    if not save_config(merge_config(cfg)):
        return ["配置文件写入失败"]
    errors = []
    for i, t, pw in pending_set:
        if not keychain.set_password(t, pw):
            errors.append(f"隧道 {i + 1}: 密码保存到钥匙串失败")
    for t in pending_del:
        keychain.delete_password(t)
    return errors


# Hard ceiling for one tunnel connectivity probe: ssh's own ConnectTimeout
# covers TCP, this covers everything else (sshpass prompt waits, key
# exchange stalls) so the HTTP request can never hang indefinitely.
_TUNNEL_TEST_TIMEOUT = 15

_SSH_FAILURE_PHRASES = (
    # Order matters: key-changed stderr also contains "Host key verification
    # failed", so the more specific pattern must come first (same string
    # SSHMonitor.is_host_key_changed classifies on).
    ("REMOTE HOST IDENTIFICATION HAS CHANGED", "主机密钥已变更，请先从菜单栏处理告警"),
    ("Host key verification failed", "主机密钥未信任，请先从菜单栏连接一次完成信任"),
    ("Permission denied", "认证失败：密钥或密码被拒绝"),
    ("Connection refused", "连接被服务器拒绝"),
    ("Could not resolve hostname", "无法解析服务器地址"),
    ("Connection timed out", "连接超时"),
    ("No route to host", "无法路由到服务器"),
    ("Network is unreachable", "网络不可达"),
)


def _describe_ssh_failure(stderr):
    """Map raw ssh stderr to a short Chinese phrase for the config UI."""
    text = (stderr or "").strip()
    for needle, phrase in _SSH_FAILURE_PHRASES:
        if needle in text:
            return phrase
    first_line = text.splitlines()[0] if text else "未知错误"
    return f"连接失败：{first_line[:120]}"


def test_tunnel(tunnel):
    """One-shot SSH reachability probe for one saved tunnel config.

    Mirrors the real tunnel exactly where it matters for the result:
    - Host-key policy is the app's own (StrictHostKeyChecking=yes over
      host_key.KNOWN_HOSTS_PATH) — a green result means the tunnel itself
      would connect; an untrusted host fails fast, never auto-trusts.
    - Key auth runs BatchMode so a passphrase prompt can never hang; the
      identity file (possibly "") is passed like SSHMonitor.start does.
    - Password auth reuses SSHMonitor's sshpass-via-fd pattern so the
      password never appears in argv.

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

    ssh_args = [
        "-o", "ConnectTimeout=5",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={host_key.KNOWN_HOSTS_PATH}",
        "-o", "GlobalKnownHostsFile=/dev/null",
        "-p", str(port), destination, "true",
    ]
    password = ""
    if tunnel.get("auth_type") == "password":
        password = keychain.get_password(tunnel)
        if not password:
            return {"ok": False, "error": "钥匙串中没有该隧道的密码，请先保存"}

    r_fd = None
    try:
        if password:
            # sshpass via fd avoids leaking the password into `ps` (SSHMonitor).
            r_fd, w_fd = os.pipe()
            try:
                os.write(w_fd, (password + "\n").encode())
            finally:
                os.close(w_fd)
            cmd = ["sshpass", "-d", str(r_fd), "ssh",
                   "-o", "NumberOfPasswordPrompts=1"] + ssh_args
            proc = subprocess.run(
                cmd, capture_output=True,
                timeout=_TUNNEL_TEST_TIMEOUT, pass_fds=(r_fd,))
        else:
            cmd = ["ssh", "-o", "BatchMode=yes",
                   "-i", str(tunnel.get("ssh_key") or "")] + ssh_args
            proc = subprocess.run(
                cmd, capture_output=True,
                timeout=_TUNNEL_TEST_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "连接超时"}
    except OSError:
        hint = "（密码认证需要 sshpass）" if password else ""
        return {"ok": False, "error": f"无法启动 ssh{hint}"}
    finally:
        if r_fd is not None:
            try:
                os.close(r_fd)
            except OSError:
                pass
    if proc.returncode == 0:
        return {"ok": True}
    # bytes + replace decode (not text=True): SSH stderr can carry raw bytes
    # and a strict-locale decode must not blow up the endpoint.
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    return {"ok": False, "error": _describe_ssh_failure(stderr)}


class _ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    expected_token = None  # set by ConfigServer.start()
    on_sp_saved = None     # set by ConfigServer.start()
    capture_state_fn = None  # set by ConfigServer.start(); → bool | None

    def _valid_token(self):
        """Validate bearer token from query string or Authorization header."""
        qs = parse_qs(urlparse(self.path).query)
        token = qs.get("token", [""])[0]
        if not token:
            auth = self.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                token = auth[7:]
        # Constant-time comparison to prevent timing side-channels.
        if not token or not _Handler.expected_token:
            return False
        return secrets.compare_digest(token, _Handler.expected_token)

    def _valid_host(self):
        """Reject non-loopback Host headers (DNS-rebinding guard)."""
        host = self.headers.get("Host", "").rsplit(":", 1)[0].strip("[]")
        return host in _ALLOWED_HOSTS

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
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
            self._json(403, {"error": "forbidden"})
            return
        if path in ("/", "/index.html"):
            try:
                html = open(_resource_path("config_ui.html"), encoding="utf-8").read()
                self._send(200, html, "text/html; charset=utf-8")
            except OSError:
                self._json(404, {"error": "config_ui.html not found"})
        elif path == "/api/state":
            mp = _read_mp()
            sp = config_store.sp_load_masked()
            # Read-only runtime status injected for the config UI; _write_mp
            # strips it again so it can never round-trip into the file.
            try:
                mp["capture_active"] = bool(
                    _Handler.capture_state_fn
                    and _Handler.capture_state_fn())
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
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._valid_host() or not self._valid_token():
            self._json(403, {"error": "forbidden"})
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
            self._json(403, {"error": "forbidden"})
            return
        if urlparse(self.path).path != "/api/state":
            self._json(404, {"error": "not found"})
            return
        data = self._read_json_body()
        if data is None:
            return
        errors = []
        mp_in = data.get("mp")
        sp_in = data.get("sp")
        if isinstance(mp_in, dict):
            errors.extend(_write_mp(mp_in))
        if isinstance(sp_in, dict):
            ok, err = config_store.sp_save(sp_in)
            if not ok:
                errors.append(err)
            elif _Handler.on_sp_saved:
                try:
                    _Handler.on_sp_saved()
                except Exception:
                    logger.exception("on_sp_saved callback failed")
        if errors:
            self._json(422, {"ok": False, "errors": errors})
        else:
            self._json(200, {"ok": True})


class ConfigServer:
    """Background HTTP server for the config UI."""

    def __init__(self, on_sp_saved=None, port=CONFIG_PORT, capture_state=None):
        self._port = port
        self._server = None
        self._thread = None
        self._token = secrets.token_hex(16)
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

    @property
    def auth_url(self):
        return f"{self.url}?token={self._token}"

    def start(self):
        """Start the server. Returns True on success, False if port unavailable."""
        if self.running:
            return True
        try:
            _Handler.expected_token = self._token
            _Handler.on_sp_saved = self._on_sp_saved
            _Handler.capture_state_fn = self._capture_state
            self._server = _ThreadingHTTPServer(("127.0.0.1", self._port), _Handler)
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
