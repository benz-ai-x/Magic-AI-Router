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


# ── SF Symbols 图标体系（macOS 11+；旧系统/未知符号静默降级纯文本）──
# 符号全部取 SF 1/2（macOS 11 基线）；_apply_icon 任何失败都不抛。
_ICON = {
    # 分区父项
    "proxy_menu": "bolt.fill",       # 代 理（-D 会话）
    "forward_menu": "arrowshape.turn.up.right",  # 端口映射（-L 转发）
    "mount_menu": "externaldrive",   # 远程挂载（NFS over SSH，ADR-007）
    "router": "cpu", "capture": "eye", "system": "gearshape",
    # 代理区
    "connect": "play.fill", "cancel": "stop.fill", "pause": "pause.fill",
    "refresh": "arrow.clockwise", "sysproxy": "globe",
    "tunnel_row": "server.rack", "launch": "arrow.up.right.square",
    # 端口映射区 / 挂载区
    "fw_start": "play.circle", "fw_stop": "stop.circle",
    "mount_row": "externaldrive",
    # AI 路由 / 抓包
    "cycle": "arrow.triangle.2.circlepath",
    "doc": "doc.on.doc", "clipboard": "doc.on.clipboard",
    "folder": "folder", "jsonl": "doc.text",
    # 系统区 / 页脚 / 状态区
    "sleep": "moon.zzz", "login": "arrow.up.circle",
    "prefs": "slider.horizontal.3", "search": "doc.text.magnifyingglass",
    "about": "info.circle", "quit": "power",
    "updown": "arrow.up.arrow.down", "circle": "circle.fill",
}


def _symbol_image(name, point_size=None, color=None):
    """SF Symbol → NSImage；不可用（旧系统/符号缺失/异常）返回 None。"""
    try:
        from AppKit import NSImage, NSImageSymbolConfiguration
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            name, None)
        if img is None:
            return None
        cfgs = []
        if point_size is not None:
            from AppKit import NSFontWeightRegular
            cfgs.append(NSImageSymbolConfiguration
                        .configurationWithPointSize_weight_(
                            point_size, NSFontWeightRegular))
        if color is not None:
            cfgs.append(NSImageSymbolConfiguration
                        .configurationWithTintColor_(color))
        for i in range(1, len(cfgs)):
            cfgs[i] = cfgs[i - 1].configByApplyingConfiguration_(cfgs[i])
        if cfgs:
            img = img.imageWithSymbolConfiguration_(cfgs[-1])
        return img
    except Exception:
        return None


def _apply_icon(item, key, point_size=None, color=None):
    """给 rumps.MenuItem 挂 SF Symbol 图标（菜单重建随建随挂）。"""
    if item is None:
        return
    img = _symbol_image(_ICON.get(key, key), point_size=point_size, color=color)
    if img is not None:
        try:
            item._menuitem.setImage_(img)
        except Exception:
            pass  # 图标是增强，绝不阻断菜单构建


def _proxy_tunnel_index(config):
    """代理角色 → 隧道下标：id（current_tunnel_id）是唯一真相，旧下标
    是兼容回退（无 id 旧档/手编配置），末路回首条。与 mpconf.config
    merge 的解析序同一语义——菜单只消费不重定义。"""
    if not isinstance(config, dict):
        return 0
    tunnels = config.get("tunnels", [])
    cid = config.get("current_tunnel_id") or ""
    if cid:
        for i, t in enumerate(tunnels):
            if isinstance(t, dict) and t.get("id") == cid:
                return i
    try:
        idx = int(config.get("current_tunnel", 0))
    except (TypeError, ValueError):
        idx = 0
    return idx if 0 <= idx < len(tunnels) else 0


def _status_color(kind):
    """状态点着色（动态系统色，明暗模式自适应）。"""
    try:
        from AppKit import NSColor
        return {"ok": NSColor.systemGreenColor(),
                "warn": NSColor.systemYellowColor(),
                "err": NSColor.systemRedColor(),
                "idle": NSColor.systemGrayColor()}[kind]
    except Exception:
        return None


def _line_status_kind(status, paused=False):
    """状态行圆点的着色档：connected=ok / connecting·paused=warn /
    error=err / 其余=idle。"""
    if paused:
        return "warn"
    return {"connected": "ok", "connecting": "warn",
            "error": "err"}.get(status, "idle")


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
    # 多活（v0.9）：转发会话快照 [(tunnel_id, name, status)]——隧道子菜单
    # 与状态行的转发计数消费；默认 () 保持既有测试构造兼容
    forward_states: tuple = ()
    # NFS 挂载快照 [(tunnel_id, tunnel_name, mount_name, status, error)]
    # （ADR-007）——挂载子菜单与状态行挂载计数消费；默认 () 同上
    mount_states: tuple = ()


# ── builder ──────────────────────────────────────────────────────

# 转发会话行尾状态（随各自 monitor）
_FW_TAIL = {"connected": " — 转发中", "connecting": " — 转发启动中",
            "error": " — 转发异常"}

# 挂载行尾状态与着色档（ADR-007）
_MOUNT_TAIL = {"mounted": " — 已挂载", "mounting": " — 挂载中…",
               "unmounting": " — 卸载中…", "unmounted": " — 未挂载",
               "error": " — 异常"}


def _mount_status_kind(status):
    return {"mounted": "ok", "mounting": "warn", "unmounting": "warn",
            "error": "err"}.get(status, "idle")

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
            st.config.get("current_tunnel_id", ""),
            len(tunnels),
            s == "error" and bool(st.ssh_error_msg),
            st.ssh_log if s == "connecting" else "",
            st.sys_proxy_on,
            bool(st.sys_proxy_error),
            st.capture_menu_title,
            st.capture_error_hint,
            st.suanpan_running,
            st.suanpan_error[:50] if st.suanpan_error else "",
            tuple(st.forward_states),  # 转发会话状态变化 → 重建子菜单
            tuple(st.mount_states),    # 挂载状态变化 → 重建挂载子菜单
        )

    # ── full build ────────────────────────────────────────

    def build(self):
        app = self._app
        app.menu.clear()
        self.refs = {}

        self._build_header()
        app.menu.add(None)
        app.menu.add(self._build_proxy_submenu())
        app.menu.add(self._build_forward_submenu())
        app.menu.add(self._build_mount_submenu())
        app.menu.add(self._build_suanpan_submenu())
        app.menu.add(self._build_capture_submenu())
        app.menu.add(self._build_system_submenu())
        app.menu.add(None)
        self._build_footer()
        self.refresh_titles()

    def _build_header(self):
        app = self._app
        st = self._get_state()
        s = st.ssh_status
        refs = self.refs

        # Proxy status line —— 着色圆点承载状态色（状态字段在 struct_key
        # 内，变化即重建换色；emoji 已退役）
        refs["proxy_status"] = rumps.MenuItem("__proxy_status__", callback=None)
        _apply_icon(refs["proxy_status"], "circle", point_size=10,
                    color=_status_color(_line_status_kind(s, st.paused)))
        app.menu.add(refs["proxy_status"])

        # Router status line
        refs["router_status"] = rumps.MenuItem("__router_status__", callback=None)
        _apply_icon(refs["router_status"], "circle", point_size=10,
                    color=_status_color(
                        "ok" if st.suanpan_running
                        else ("err" if st.suanpan_error else "idle")))
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
            _apply_icon(refs["traffic"], "updown", point_size=10,
                        color=_status_color("idle"))
            app.menu.add(refs["traffic"])

    def _build_proxy_submenu(self):
        """代 理 ▸ —— 只管那条唯一的 -D 会话（SOCKS5 上游）：启停/暂停/
        重连、系统代理、代理角色单选、经代理启动。"""
        st = self._get_state()
        a = self._app
        parent = rumps.MenuItem("代 理", callback=None)
        _apply_icon(parent, "proxy_menu")

        # Connect control
        s = st.ssh_status
        if s == "connecting":
            item = rumps.MenuItem("取消连接", callback=a.cancel_connection, key="r")
            _apply_icon(item, "cancel")
            parent.add(item)
        else:
            if st.paused:
                item = rumps.MenuItem("恢复代理", callback=a.toggle_pause, key="p")
                _apply_icon(item, "connect")
                parent.add(item)
            elif s == "connected":
                item = rumps.MenuItem("暂停代理", callback=a.toggle_pause, key="p")
                _apply_icon(item, "pause")
                parent.add(item)
            item = rumps.MenuItem(
                "重新连接" if s in ("connected", "error") else "连接代理",
                callback=a.reconnect, key="r")
            _apply_icon(item, "refresh")
            parent.add(item)

        # System proxy toggle
        parent.add(None)
        if st.sys_proxy_error:
            sysp_title = "系统代理：异常"
        elif st.sys_proxy_on:
            sysp_title = "系统代理：开"
        else:
            sysp_title = "系统代理：关"
        item = rumps.MenuItem(sysp_title, callback=a.toggle_system_proxy, key="g")
        _apply_icon(item, "sysproxy")
        parent.add(item)

        # 代理角色单选（哪条隧道当 SOCKS5 上游）
        tunnels = st.config.get("tunnels", [])
        if tunnels:
            parent.add(None)
            parent.add(rumps.MenuItem("代理隧道（SOCKS5 上游）", callback=None))
            current_idx = _proxy_tunnel_index(st.config)
            for i, t in enumerate(tunnels):
                marker = "✓ " if i == current_idx else ""
                name = t.get("name") or f"{t.get('ssh_user', '')}@{t.get('ssh_host', '')}"
                item = rumps.MenuItem(f"{marker}{name}",
                                      callback=a.make_switch_tunnel(i))
                _apply_icon(item, "tunnel_row")
                parent.add(item)

        # Proxied app launches
        apps_list = chromium_proxy.installed_apps()
        if apps_list:
            parent.add(None)
            sub = rumps.MenuItem("经代理启动 App", callback=None)
            _apply_icon(sub, "launch")
            for entry in apps_list:
                item = rumps.MenuItem(
                    entry["name"], callback=a.make_launch_proxied(entry))
                _apply_icon(item, "launch")
                sub.add(item)
            parent.add(sub)

        return parent

    def _build_forward_submenu(self):
        """端口映射 ▸ —— 只管纯 -L 转发会话（多活）：代理隧道自身显示
        「随代理运行」信息行；其余隧道各自启停/单会话重连。"""
        st = self._get_state()
        a = self._app
        parent = rumps.MenuItem("端口映射", callback=None)
        _apply_icon(parent, "forward_menu")

        tunnels = st.config.get("tunnels", [])
        current_idx = _proxy_tunnel_index(st.config)
        fw_running = {f.tunnel_id: f.status
                      for f in (st.forward_states or ())}
        any_rules = False
        for i, t in enumerate(tunnels):
            tid = t.get("id") or f"#{i}"
            name = t.get("name") or f"{t.get('ssh_user', '')}@{t.get('ssh_host', '')}"
            forwards = t.get("forwards") or []
            any_rules = any_rules or bool(forwards)
            if i == current_idx:
                # 代理隧道自身：其 -L 随代理会话跑，此处仅呈现状态
                on = st.ssh_status == "connected"
                row = rumps.MenuItem(
                    f"{name} — {'随代理运行' if on else '未随代理运行'}",
                    callback=None)
                _apply_icon(row, "circle", point_size=9,
                            color=_status_color("ok" if on else "idle"))
                parent.add(row)
                parent.add(None)
                continue
            if tid in fw_running:
                tail = _FW_TAIL.get(fw_running[tid], " — 转发重试中")
            else:
                tail = " — 未启动"
            summary = " · ".join(
                f"{f.get('local_port')}→{f.get('remote_port')}"
                for f in forwards if isinstance(f, dict)) if forwards else ""
            title = f"{name}{tail}" + (f" · {summary}" if summary else "")
            row = rumps.MenuItem(title, callback=None)
            _apply_icon(row, "tunnel_row")
            if tid in fw_running:
                item = rumps.MenuItem("停止端口转发",
                                      callback=a.toggle_forward_session(tid))
                _apply_icon(item, "fw_stop")
                row.add(item)
                item = rumps.MenuItem("重新连接",
                                      callback=a.make_reconnect_tunnel(tid))
                _apply_icon(item, "refresh")
                row.add(item)
            elif forwards:
                item = rumps.MenuItem("启动端口转发",
                                      callback=a.toggle_forward_session(tid))
                _apply_icon(item, "fw_start")
                row.add(item)
            else:
                row.add(rumps.MenuItem("启动端口转发（需先配置转发规则）",
                                       callback=None))
            parent.add(row)

        if tunnels and not any_rules:
            parent.add(None)
            parent.add(rumps.MenuItem("在偏好设置 → 隧道里添加转发规则",
                                      callback=None))
        return parent

    def _build_mount_submenu(self):
        """远程挂载 ▸ —— NFS over SSH 挂载项（ADR-007）：跨隧道列出所有
        配置了 NFS 挂载的项，每项启停 + 打开挂载目录。NFS 走独立专用
        会话，无端口映射里「随代理运行」的特殊行。"""
        st = self._get_state()
        a = self._app
        parent = rumps.MenuItem("远程挂载", callback=None)
        _apply_icon(parent, "mount_menu")

        any_mounts = False
        for entry in (st.mount_states or ()):
            # MountState（NamedTuple 投影）：字段即契约，不再防御式猜形状
            tid, tname, mname, status, error = (
                entry.tunnel_id, entry.tunnel_name, entry.name,
                entry.status, entry.error)
            any_mounts = True
            tail = _MOUNT_TAIL.get(status, "")
            row = rumps.MenuItem(f"{tname} · {mname}{tail}", callback=None)
            _apply_icon(row, "circle", point_size=9,
                        color=_status_color(_mount_status_kind(status)))
            active = status in ("mounted", "mounting", "unmounting")
            item = rumps.MenuItem(
                "卸载" if active else "挂载",
                callback=a.make_toggle_mount(tid, mname))
            _apply_icon(item, "fw_stop" if active else "fw_start")
            row.add(item)
            item = rumps.MenuItem(
                "打开挂载目录", callback=a.make_open_mount_dir(tid, mname))
            _apply_icon(item, "folder")
            row.add(item)
            if status == "error" and error:
                row.add(rumps.MenuItem(f"  {_truncate(error, 60)}",
                                       callback=None))
            parent.add(row)

        if not any_mounts:
            parent.add(rumps.MenuItem("在偏好设置 → 远程挂载里配置",
                                      callback=None))
        return parent

    def _build_capture_submenu(self):
        a = self._app
        st = self._get_state()
        parent = rumps.MenuItem("抓 包", callback=None)
        _apply_icon(parent, "capture")
        item = rumps.MenuItem(st.capture_menu_title, callback=a.toggle_capture, key="m")
        _apply_icon(item, "capture")
        parent.add(item)
        if st.capture_error_hint:
            parent.add(rumps.MenuItem(st.capture_error_hint, callback=None))
        parent.add(None)
        item = rumps.MenuItem("打开抓包目录", callback=a.open_capture_dir)
        _apply_icon(item, "folder")
        parent.add(item)
        item = rumps.MenuItem("今日 JSONL", callback=a.open_today_jsonl)
        _apply_icon(item, "jsonl")
        parent.add(item)
        return parent

    def _build_suanpan_submenu(self):
        a = self._app
        st = self._get_state()
        parent = rumps.MenuItem("AI 路由", callback=None)
        _apply_icon(parent, "router")
        item = rumps.MenuItem(
            "停止路由" if st.suanpan_running else "启动路由",
            callback=a.toggle_suanpan)
        _apply_icon(item, "cancel" if st.suanpan_running else "connect")
        parent.add(item)
        if st.suanpan_running:
            item = rumps.MenuItem("重启路由", callback=a.restart_suanpan)
            _apply_icon(item, "cycle")
            parent.add(item)
            item = rumps.MenuItem("重新加载配置", callback=a.reload_suanpan)
            _apply_icon(item, "refresh")
            parent.add(item)
        parent.add(None)
        item = rumps.MenuItem("复制连接地址", callback=a.copy_suanpan_url)
        _apply_icon(item, "doc")
        parent.add(item)
        item = rumps.MenuItem("复制配置样例", callback=a.copy_suanpan_example)
        _apply_icon(item, "clipboard")
        parent.add(item)
        return parent

    def _build_system_submenu(self):
        """系 统 ▸ —— 防睡眠 / 登录启动从页脚收进来（v0.9.1 重组）。"""
        a = self._app
        st = self._get_state()
        parent = rumps.MenuItem("系 统", callback=None)
        _apply_icon(parent, "system")
        item = rumps.MenuItem(
            st.prevent_sleep_title, callback=a.toggle_prevent_sleep, key="n")
        _apply_icon(item, "sleep")
        self.refs["prevent_sleep"] = item
        parent.add(item)
        item = rumps.MenuItem(
            st.launch_login_title, callback=a.toggle_launch_at_login, key="k")
        _apply_icon(item, "login")
        self.refs["launch_login"] = item
        parent.add(item)
        return parent

    def _build_footer(self):
        app = self._app
        a = self._app
        item = rumps.MenuItem("偏好设置…", callback=a.show_preferences, key=",")
        _apply_icon(item, "prefs")
        app.menu.add(item)
        item = rumps.MenuItem("查看日志", callback=a.show_log_window, key="l")
        _apply_icon(item, "search")
        app.menu.add(item)
        item = rumps.MenuItem("复制 AI 助手指令",
                              callback=a.copy_agent_instructions)
        _apply_icon(item, "clipboard")
        app.menu.add(item)
        app.menu.add(None)
        item = rumps.MenuItem("关于 Magic AI Router", callback=a.about)
        _apply_icon(item, "about")
        app.menu.add(item)
        item = rumps.MenuItem("退出", callback=a.quit_app, key="q")
        _apply_icon(item, "quit")
        app.menu.add(item)

    # ── dynamic title refresh ─────────────────────────────

    def refresh_titles(self):
        st = self._get_state()
        s = st.ssh_status
        tunnel = st.current_tunnel
        tunnel_name = tunnel.get("name") if tunnel else None
        tunnel_name = tunnel_name or (
            f"{tunnel.get('ssh_user', '')}@{tunnel.get('ssh_host', '')}" if tunnel else "未配置")

        # Proxy status line —— 颜色由行首圆点图标承载（emoji 已退役）
        if st.paused:
            proxy_text = f"AI Proxy · {tunnel_name} · 已暂停"
        elif s == "connected":
            proxy_text = f"AI Proxy · {tunnel_name}"
        elif s == "connecting":
            proxy_text = f"AI Proxy · {tunnel_name} · 连接中…"
        elif s == "error":
            proxy_text = f"AI Proxy · {tunnel_name} · 连接失败"
        else:
            proxy_text = "AI Proxy"
        # 多活：转发会话在跑时状态行附转发计数（主图标语义不变——只反映
        # 代理会话，:8888 上游只依赖它）
        fw_up = sum(1 for f in (st.forward_states or ())
                    if f.status == "connected")
        if fw_up:
            proxy_text += f" ｜ {fw_up} 条转发"
        # 挂载计数同款模式（ADR-007）：只数已挂载
        mounts_up = sum(1 for entry in (st.mount_states or ())
                        if entry.status == "mounted")
        if mounts_up:
            proxy_text += f" ｜ {mounts_up} 挂载"
        self._set_title("proxy_status", proxy_text)

        # Router status line
        if st.suanpan_running:
            router_text = f"AI Router · {st.suanpan_listen_address}"
        elif st.suanpan_error:
            router_text = f"AI Router · {st.suanpan_error[:40]}"
        else:
            router_text = "AI Router"
        self._set_title("router_status", router_text)

        # Traffic line —— 方向箭头由行首图标承载
        if "traffic" in self.refs:
            snap = st.stats_snapshot
            traffic_text = (
                f"{_human(snap['rate_down'], 'B/s')}"
                f"  ·  {_human(snap['rate_up'], 'B/s')}"
                f"  ·  {snap['active_connections']} 连接"
            )
            self._set_title("traffic", traffic_text)

        # 系统区开关文案（refs 化，不依赖整菜单重建）
        self._set_title("prevent_sleep", st.prevent_sleep_title)
        self._set_title("launch_login", st.launch_login_title)

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
