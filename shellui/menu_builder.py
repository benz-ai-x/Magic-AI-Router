"""Menu bar UI builder for Magic AI Router.

Constructs rumps menu trees from a frozen state snapshot + a callback
namespace.  Owns menu refs, status icon cache, and struct-key tracking.
Extracted from MagicProxyApp to isolate ~330 lines of view-layer code.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable
import logging

import rumps
from capture import chromium_proxy
from util import resource_path as _resource_path, truncate as _truncate

logger = logging.getLogger("magic-proxy.menu")

STATUS_ICON_RESOURCE = "MenubarIcon.png"
STATUS_ICON_GRAY_RESOURCE = "MenubarIcon-gray.png"
STATUS_ICON_YELLOW_RESOURCE = "MenubarIcon-yellow.png"
STATUS_STATE_STYLE = {
    "green":  ("systemBlueColor", "🔵"),
    "yellow": ("systemYellowColor", "🟡"),
    "gray":   ("systemGrayColor", "⚪"),
}
_ICON_RESOURCE_FOR_KEY = {
    "green":  STATUS_ICON_RESOURCE,
    "yellow": STATUS_ICON_YELLOW_RESOURCE,
    "gray":   STATUS_ICON_GRAY_RESOURCE,
}


def _status_color_for_connection(status, paused=False):
    if paused:
        return "yellow"
    if status == "connected":
        return "green"
    if status == "connecting":
        return "yellow"
    return "gray"


def _human(n, suffix="B"):
    if n < 1024:
        return f"{int(n)} {suffix}"
    for unit in ("K", "M", "G"):
        n /= 1024
        if n < 1024:
            decimals = 2 if unit == "G" else 1
            return f"{n:.{decimals}f} {unit}{suffix}"
    return f"{n:.2f} T{suffix}"


# ── interface types ──────────────────────────────────────────────

@dataclass(frozen=True)
class MenuState:
    """Frozen snapshot of all render data the MenuBuilder reads."""

    ssh_status: str
    ssh_cmd_str: str
    ssh_log: str
    ssh_error_msg: str
    paused: bool
    stats_snapshot: dict
    config: dict
    sys_proxy_on: bool
    sys_proxy_error: str
    capture_menu_title: str
    capture_error_hint: str | None
    suanpan_running: bool
    suanpan_error: str
    suanpan_listen_address: str
    current_tunnel: dict | None
    prevent_sleep_title: str
    launch_login_title: str


# ── builder ──────────────────────────────────────────────────────

class MenuBuilder:
    """Builds and refreshes the menu bar UI from a state snapshot.

    The `app` is the MagicProxyApp itself — callback names are its
    method names (cancel_connection, reconnect, toggle_pause, …).
    """

    def __init__(self, app, get_state: Callable[[], MenuState]):
        self._app = app          # MagicProxyApp: menu + callbacks + _nsapp
        self._get_state = get_state
        self.refs = {}
        self.last_struct_key = None
        self._icon_cache = {}
        self._icon_ok = True

    # ── struct key ────────────────────────────────────────

    def struct_key(self):
        st = self._get_state()
        s = st.ssh_status
        tunnels = st.config.get("tunnels", [])
        # Note: active_connections is deliberately NOT here (#40) — it
        # fluctuates every tick while traffic flows, but only affects the
        # traffic *title* (refresh_titles), never the menu structure.
        return (
            s, st.paused,
            st.config.get("current_tunnel", 0),
            len(tunnels),
            s == "error" and bool(st.ssh_error_msg),
            st.ssh_log if s == "connecting" else "",
            st.sys_proxy_on,
            bool(st.sys_proxy_error),
            st.capture_menu_title,
            st.capture_error_hint,
            st.suanpan_running,
            st.suanpan_error[:50] if st.suanpan_error else "",
        )

    # ── full build ────────────────────────────────────────

    def build(self):
        app = self._app
        app.menu.clear()
        self.refs = {}

        self._build_header()
        app.menu.add(None)
        app.menu.add(self._build_tunnel_submenu())
        app.menu.add(self._build_suanpan_submenu())
        app.menu.add(self._build_capture_submenu())
        app.menu.add(None)
        self._build_footer()
        self.refresh_titles()

    def _build_header(self):
        app = self._app
        st = self._get_state()
        s = st.ssh_status
        refs = self.refs

        # Proxy status line
        refs["proxy_status"] = rumps.MenuItem("__proxy_status__", callback=None)
        app.menu.add(refs["proxy_status"])

        # Router status line
        refs["router_status"] = rumps.MenuItem("__router_status__", callback=None)
        app.menu.add(refs["router_status"])

        # Connecting log lines
        if s == "connecting" and st.ssh_cmd_str:
            app.menu.add(rumps.MenuItem(f"  {_truncate(st.ssh_cmd_str, 60)}", callback=None))
            if st.ssh_log:
                app.menu.add(None)
                for line in st.ssh_log.split("\n")[-3:]:
                    app.menu.add(rumps.MenuItem(f"  {_truncate(line, 60)}", callback=None))

        if s == "error" and st.ssh_error_msg:
            app.menu.add(rumps.MenuItem(f"  {_truncate(st.ssh_error_msg, 80)}", callback=None))

        # Traffic line (connected only)
        if s == "connected" and not st.paused:
            refs["traffic"] = rumps.MenuItem("__traffic__", callback=None)
            app.menu.add(refs["traffic"])

    def _build_tunnel_submenu(self):
        st = self._get_state()
        a = self._app
        parent = rumps.MenuItem("代理隧道", callback=None)

        # Connect control
        s = st.ssh_status
        if s == "connecting":
            parent.add(rumps.MenuItem("取消连接", callback=a.cancel_connection, key="r"))
        else:
            if st.paused:
                parent.add(rumps.MenuItem("恢复代理", callback=a.toggle_pause, key="p"))
            elif s == "connected":
                parent.add(rumps.MenuItem("暂停代理", callback=a.toggle_pause, key="p"))
            parent.add(rumps.MenuItem(
                "重新连接" if s in ("connected", "error") else "连接代理",
                callback=a.reconnect, key="r"))

        # System proxy toggle
        parent.add(None)
        if st.sys_proxy_error:
            sysp_title = "系统代理：异常"
        elif st.sys_proxy_on:
            sysp_title = "系统代理：开"
        else:
            sysp_title = "系统代理：关"
        parent.add(rumps.MenuItem(sysp_title, callback=a.toggle_system_proxy, key="g"))

        # Tunnel selection
        tunnels = st.config.get("tunnels", [])
        if tunnels:
            parent.add(None)
            current_idx = st.config.get("current_tunnel", 0)
            for i, t in enumerate(tunnels):
                marker = "✓   " if i == current_idx else "    "
                name = t.get("name") or f"{t.get('ssh_user', '')}@{t.get('ssh_host', '')}"
                parent.add(rumps.MenuItem(f"{marker}{name}", callback=a.make_switch_tunnel(i)))

        # Proxied app launches
        apps_list = chromium_proxy.installed_apps()
        if apps_list:
            parent.add(None)
            for entry in apps_list:
                parent.add(rumps.MenuItem(
                    f"经代理启动 {entry['name']}", callback=a.make_launch_proxied(entry)))

        return parent

    def _build_capture_submenu(self):
        a = self._app
        st = self._get_state()
        parent = rumps.MenuItem("抓包", callback=None)
        parent.add(rumps.MenuItem(
            st.capture_menu_title, callback=a.toggle_capture, key="m"))
        if st.capture_error_hint:
            parent.add(rumps.MenuItem(st.capture_error_hint, callback=None))
        parent.add(None)
        parent.add(rumps.MenuItem("打开抓包目录", callback=a.open_capture_dir))
        parent.add(rumps.MenuItem("今日 JSONL", callback=a.open_today_jsonl))
        return parent

    def _build_suanpan_submenu(self):
        a = self._app
        st = self._get_state()
        parent = rumps.MenuItem("AI 路由", callback=None)
        parent.add(rumps.MenuItem(
            "停止路由" if st.suanpan_running else "启动路由",
            callback=a.toggle_suanpan))
        if st.suanpan_running:
            parent.add(rumps.MenuItem("重启路由", callback=a.restart_suanpan))
            parent.add(rumps.MenuItem("重新加载配置", callback=a.reload_suanpan))
        parent.add(None)
        parent.add(rumps.MenuItem("复制连接地址", callback=a.copy_suanpan_url))
        parent.add(rumps.MenuItem("复制配置样例", callback=a.copy_suanpan_example))
        return parent

    def _build_footer(self):
        app = self._app
        a = self._app
        st = self._get_state()
        app.menu.add(rumps.MenuItem("偏好设置…", callback=a.show_preferences, key=","))
        app.menu.add(rumps.MenuItem("查看日志", callback=a.show_log_window, key="l"))
        app.menu.add(None)
        app.menu.add(rumps.MenuItem(
            st.prevent_sleep_title, callback=a.toggle_prevent_sleep, key="n"))
        app.menu.add(rumps.MenuItem(
            st.launch_login_title, callback=a.toggle_launch_at_login, key="k"))
        app.menu.add(None)
        app.menu.add(rumps.MenuItem("关于 Magic AI Router", callback=a.about))
        app.menu.add(rumps.MenuItem("退出", callback=a.quit_app, key="q"))

    # ── dynamic title refresh ─────────────────────────────

    def refresh_titles(self):
        st = self._get_state()
        s = st.ssh_status
        tunnel = st.current_tunnel
        tunnel_name = tunnel.get("name") if tunnel else None
        tunnel_name = tunnel_name or (
            f"{tunnel.get('ssh_user', '')}@{tunnel.get('ssh_host', '')}" if tunnel else "未配置")

        # Proxy status line
        if st.paused:
            proxy_text = f"🟡  AI Proxy · {tunnel_name} · 已暂停"
        elif s == "connected":
            proxy_text = f"🟢  AI Proxy · {tunnel_name}"
        elif s == "connecting":
            proxy_text = f"🟡  AI Proxy · {tunnel_name} · 连接中…"
        elif s == "error":
            proxy_text = f"🔴  AI Proxy · {tunnel_name} · 连接失败"
        else:
            proxy_text = "⚫  AI Proxy"
        self._set_title("proxy_status", proxy_text)

        # Router status line
        if st.suanpan_running:
            router_text = f"🟢  AI Router · {st.suanpan_listen_address}"
        elif st.suanpan_error:
            router_text = f"🔴  AI Router · {st.suanpan_error[:40]}"
        else:
            router_text = "⚫  AI Router"
        self._set_title("router_status", router_text)

        # Traffic line
        if "traffic" in self.refs:
            snap = st.stats_snapshot
            traffic_text = (
                f"▼ {_human(snap['rate_down'], 'B/s')}"
                f"  ·  ▲ {_human(snap['rate_up'], 'B/s')}"
                f"  ·  {snap['active_connections']} 连接"
            )
            self._set_title("traffic", traffic_text)

    def _set_title(self, key, text):
        item = self.refs.get(key)
        if item is not None and item.title != text:
            item.title = text

    # ── status bar icon ───────────────────────────────────

    def set_status_icon(self, color_key):
        _, emoji = STATUS_STATE_STYLE[color_key]
        item = getattr(getattr(self._app, "_nsapp", None), "nsstatusitem", None)
        if item is None or not self._icon_ok:
            self._app.title = emoji
            return
        try:
            img = self._status_image(color_key)
        except Exception:
            logger.exception("Custom status icon failed; using emoji")
            self._icon_ok = False
            self._app.title = emoji
            return
        item.setImage_(img)
        item.setTitle_("")

    def _status_image(self, color_key):
        cached = self._icon_cache.get(color_key)
        if cached is not None:
            return cached
        from AppKit import NSImage
        from Foundation import NSMakeSize
        resource = _ICON_RESOURCE_FOR_KEY.get(color_key, STATUS_ICON_GRAY_RESOURCE)
        base = NSImage.alloc().initWithContentsOfFile_(
            _resource_path(resource))
        if base is None:
            raise RuntimeError("Status icon unavailable: " + resource)
        img = base.copy()
        img.setSize_(NSMakeSize(22, 22))
        img.setTemplate_(False)
        self._icon_cache[color_key] = img
        return img
