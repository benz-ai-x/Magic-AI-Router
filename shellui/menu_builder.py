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
    "network": "network",   # 配置 API 服务开关（ADR-009）
    "prefs": "slider.horizontal.3", "search": "doc.text.magnifyingglass",
    "about": "info.circle", "quit": "power",
    "updown": "arrow.up.arrow.down", "circle": "circle.fill",
}


def _symbol_image(name, point_size=None, color=None, description=None):
    """SF Symbol → NSImage；不可用（旧系统/符号缺失/异常）返回 None。

    description 进 VoiceOver（a11y）——圆点图标的颜色语义只有视觉通道，
    必须补文字描述（「已连接」等），否则屏幕阅读器拿不到状态。"""
    try:
        from AppKit import NSImage, NSImageSymbolConfiguration
        img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            name, description)
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
        if color is not None:
            # SF Symbol 默认 template=True——NSMenuItem 对 template 图像
            # 按菜单文字色单色渲染，tint 配置被无视（菜单里所有圆点一直
            # 显示黑色的根因，真机截图实锄）。带 tint 显式关掉 template
            # 让颜色生效（动态系统色自带明暗适配）；无 tint 的动作图标
            # 保持 template，随菜单文字色自动适配明暗。
            img.setTemplate_(False)
        return img
    except Exception:
        return None


def _apply_icon(item, key, point_size=None, color=None, description=None):
    """给 rumps.MenuItem 挂 SF Symbol 图标（菜单重建随建随挂）。

    description 缺省取 item.title（动态占位标题 __xxx__ 除外）——分区/
    动作图标的语义即标题，无需逐处手传。着色状态点请用
    _apply_status_dot（SF Symbol 的 tint 在 NSMenuItem 上两轮真机实测
    不生效）。"""
    if item is None:
        return
    if description is None:
        title = getattr(item, "title", "")
        if title and not title.startswith("__"):
            description = title
    img = _symbol_image(_ICON.get(key, key), point_size=point_size,
                        color=color, description=description)
    if img is not None:
        try:
            item._menuitem.setImage_(img)
        except Exception:
            pass  # 图标是增强，绝不阻断菜单构建


def _status_dot_image(kind, point_size):
    """手绘状态圆点（位图，不走 SF Symbol 渲染通道）。

    SF Symbol 图像在 NSMenuItem 上的 tint 两轮真机实测不生效
    （template 语义顽固，setTemplate_(False) 亦无效）——直接在画布上
    填色最可靠。idle 档画黑点并保持 template：随菜单文字色自动适配
    明暗（浅色黑/深色白）；彩色档（绿/黄/红）固定色 + 非模板——两种
    外观下都可读。状态语义经行标题文字到达 VoiceOver（符号图像的
    accessibilityDescription 通道随符号一并退役）。"""
    try:
        from AppKit import NSBezierPath, NSColor, NSImage, NSMakeRect
        size = float(point_size)
        img = NSImage.alloc().initWithSize_((size, size))
        img.lockFocus()
        fill = (NSColor.blackColor() if kind == "idle"
                else _status_color(kind))
        if fill is not None:
            fill.setFill()
            NSBezierPath.bezierPathWithOvalInRect_(
                NSMakeRect(0, 0, size, size)).fill()
        img.unlockFocus()
        img.setTemplate_(kind == "idle")
        return img
    except Exception:
        logger.exception("status dot draw failed")
        return None


def _apply_status_dot(item, kind, point_size):
    """给行挂手绘状态圆点（着色唯一可靠通道；失败兜底回符号路径）。"""
    if item is None:
        return
    img = _status_dot_image(kind, point_size)
    if img is None:
        _apply_icon(item, "circle", point_size=point_size,
                    color=_status_color(kind))
        return
    try:
        item._menuitem.setImage_(img)
    except Exception:
        pass


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
    """状态点着色（动态系统色，明暗模式自适应）。idle=未启动用
    labelColor（浅色模式黑/深色模式白）——用户拍板的二元语义：运行绿、
    未启动黑；黄只留给进行中（connecting/mounting），红只留给异常。"""
    try:
        from AppKit import NSColor
        return {"ok": NSColor.systemGreenColor(),
                "warn": NSColor.systemYellowColor(),
                "err": NSColor.systemRedColor(),
                "idle": NSColor.labelColor()}[kind]
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
    # ADR-009：配置 API 常驻开关标题（选 项 组）——缺省「开启」保持旧
    # 构造兼容（未配置即待开启）
    config_api_title: str = "开启配置 API 服务"


# ── builder ──────────────────────────────────────────────────────

# 转发会话行尾状态（随各自 monitor）
_FW_TAIL = {"connected": " — 转发中", "connecting": " — 转发启动中",
            "error": " — 转发异常"}

# 挂载行尾状态与着色档（ADR-007）；单一「·」分隔（与转发行同款），
# 异常详情折进行标题（结构恒定——状态细节不触发重建）。值兼作
# VoiceOver 描述
_MOUNT_TAIL = {"mounted": "已挂载", "mounting": "挂载中…",
               "unmounting": "卸载中…", "unmounted": "未挂载",
               "error": "异常"}


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
        #
        # 转发/挂载只进**身份**签名（哪些行存在——隧道/端口对/挂载名）：
        # 行内状态（连接态、挂载态、error 文本、enabled 翻转）由
        # _refresh_forward_rows/_refresh_mount_rows 就地刷新。后台状态
        # 翻转不再整树重建——此前任何一条会话 connecting→connected 都会
        # clear+rebuild 全部七组，用户正展开子菜单时整棵塌掉。
        fw_identity = tuple(
            (t.get("id") or f"#{i}",
             tuple((f.get("local_port"), f.get("remote_port"))
                   for f in (t.get("forwards") or [])
                   if isinstance(f, dict)))
            for i, t in enumerate(tunnels) if isinstance(t, dict))
        mount_identity = tuple((e.tunnel_id, e.name)
                               for e in (st.mount_states or ()))
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
            fw_identity,   # 行集合变化（配置增删）→ 重建
            mount_identity,
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
        _apply_status_dot(refs["proxy_status"],
                          _line_status_kind(s, st.paused), point_size=10)
        app.menu.add(refs["proxy_status"])

        # Router status line
        refs["router_status"] = rumps.MenuItem("__router_status__", callback=None)
        _apply_status_dot(
            refs["router_status"],
            "ok" if st.suanpan_running
            else ("err" if st.suanpan_error else "idle"),
            point_size=10)
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

        # System proxy toggle（动词式——开关范式统一：标题是动作，
        # 状态由菜单语境与通知承载）
        parent.add(None)
        if st.sys_proxy_error:
            sysp_title = "系统代理异常 · 点按重试"
        elif st.sys_proxy_on:
            sysp_title = "关闭系统代理"
        else:
            sysp_title = "开启系统代理"
        item = rumps.MenuItem(sysp_title, callback=a.toggle_system_proxy, key="g")
        _apply_icon(item, "sysproxy")
        parent.add(item)

        # 代理角色单选（哪条隧道当 SOCKS5 上游）
        tunnels = st.config.get("tunnels", [])
        if tunnels:
            parent.add(None)
            parent.add(rumps.MenuItem("代理隧道（本地代理的上游）",
                                      callback=None))
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
        「随代理运行」信息行；其余隧道各自启停/单会话重连。

        逐条启停（v0.11）：每条转发独立成行（点击即启停），圆点随会话
        状态着色、停用行灰点。UX 批次：①单隧道拍平（包装行只在多隧道
        时有意义——转发行一级直达）；②行结构**恒定**（会话启停动作恒
        在，标签由刷新段定「启动/停止」），状态/文案就地刷新——后台
        状态翻转不重建整树；③代理隧道的行点击会重启整个代理会话（全
        代理流量中断），连接中在隧道行标题明示。
        """
        st = self._get_state()
        a = self._app
        parent = rumps.MenuItem("端口映射", callback=None)
        _apply_icon(parent, "forward_menu")

        tunnels = st.config.get("tunnels", [])
        single = len(tunnels) == 1
        any_rules = False
        for i, t in enumerate(tunnels):
            tid = t.get("id") or f"#{i}"
            forwards = t.get("forwards") or []
            any_rules = any_rules or bool(forwards)
            is_proxy = i == _proxy_tunnel_index(st.config)
            if single:
                host = parent          # 拍平：行直接挂顶层（免一层嵌套）
            else:
                # 多隧道：包装行承载隧道名/尾标；代理隧道的包装行即
                # 「随代理运行」上下文行（转发行挂其下）。占位标题按
                # 隧道 id 唯一——rumps Menu 以标题为键，重名行互相覆盖
                host = rumps.MenuItem(f"__fw_tunnel_{tid}__", callback=None)
                if is_proxy:
                    _apply_icon(host, "circle", point_size=9)
                    self.refs[("fw_ctx", tid)] = host
                else:
                    _apply_icon(host, "tunnel_row")
                    self.refs[("fw_tunnel", tid)] = host
            if is_proxy and single:
                ctx = rumps.MenuItem(f"__fw_ctx_{tid}__", callback=None)
                _apply_icon(ctx, "circle", point_size=9)
                self.refs[("fw_ctx", tid)] = ctx
                host.add(ctx)
            self._add_forward_rows(host, a, tid, forwards)
            if not is_proxy:
                # 逐条转发行在前、会话动作在后（v0.12 既定行序）
                host.add(None)
                if forwards:
                    action = rumps.MenuItem(
                        f"__fw_action_{tid}__",
                        callback=a.toggle_forward_session(tid))
                    _apply_icon(action, "fw_start")
                    self.refs[("fw_action", tid)] = action
                    host.add(action)
                    item = rumps.MenuItem(
                        "重新连接", callback=a.make_reconnect_tunnel(tid))
                    _apply_icon(item, "refresh")
                    host.add(item)
                else:
                    item = rumps.MenuItem(
                        "添加转发规则…", callback=a.show_prefs_forwards)
                    _apply_icon(item, "forward_menu")
                    host.add(item)
            if not single:
                parent.add(host)
                parent.add(None)

        if tunnels and not any_rules:
            parent.add(rumps.MenuItem("添加转发规则…",
                                      callback=a.show_prefs_forwards))
        self._refresh_forward_rows(st)
        return parent

    def _add_forward_rows(self, parent, app, tunnel_id, forwards):
        """隧道行下挂逐条转发子行：点击即启停（唯一动作，免四级嵌套）。
        标题与圆点由 _refresh_forward_rows 就地刷新（结构恒定）。"""
        for fi, f in enumerate(forwards):
            if not isinstance(f, dict):
                continue
            row = rumps.MenuItem(
                f"__fw_row_{tunnel_id}_{fi}__",
                callback=app.make_toggle_forward(tunnel_id, fi))
            _apply_icon(row, "circle", point_size=8)
            self.refs[("fw_row", tunnel_id, fi)] = row
            parent.add(row)

    def _refresh_forward_rows(self, st):
        """端口映射区动态段：隧道行尾标、逐条转发行尾标与圆点、启停
        动作标签。每秒 tick 调用——只在标题变化时重挂图标（SF Symbol
        查找不便宜，不能每 tick 全量重设）。"""
        tunnels = st.config.get("tunnels", [])
        current_idx = _proxy_tunnel_index(st.config)
        fw_running = {f.tunnel_id: f.status
                      for f in (st.forward_states or ())}
        for i, t in enumerate(tunnels):
            if not isinstance(t, dict):
                continue
            tid = t.get("id") or f"#{i}"
            name = t.get("name") or \
                f"{t.get('ssh_user', '')}@{t.get('ssh_host', '')}"
            is_proxy = i == current_idx
            if is_proxy:
                row = self.refs.get(("fw_ctx", tid))
                if row is not None:
                    on = st.ssh_status == "connected"
                    # 点击代理隧道的转发行 = 重启代理会话（全流量中断），
                    # 副作用在行标题明示——先于点击可见
                    title = (f"{name} — 随代理运行 · 启停将重启代理"
                             if on else f"{name} — 未随代理运行")
                    if row.title != title:
                        row.title = title
                        _apply_status_dot(
                            row, "ok" if on else "idle", point_size=9)
            else:
                row = self.refs.get(("fw_tunnel", tid))
                if row is not None:
                    status = fw_running.get(tid)
                    tail = (_FW_TAIL.get(status, " — 转发重试中")
                            if status else " — 未启动")
                    self._set_title_ref(row, f"{name}{tail}")
                action = self.refs.get(("fw_action", tid))
                if action is not None:
                    running = tid in fw_running
                    new_title = "停止端口转发" if running else "启动端口转发"
                    if action.title != new_title:  # 图标随标题变化才重挂
                        action.title = new_title
                        _apply_icon(action,
                                    "fw_stop" if running else "fw_start")
            session_up = (st.ssh_status == "connected" if is_proxy
                          else fw_running.get(tid) == "connected")
            for fi, f in enumerate(t.get("forwards") or []):
                if not isinstance(f, dict):
                    continue
                row = self.refs.get(("fw_row", tid, fi))
                if row is None:
                    continue
                lp, rp = f.get("local_port"), f.get("remote_port")
                enabled = f.get("enabled") is not False
                # 二元着色（用户拍板）：已映射=绿；未连接/已停用都是
                # 「没启动」=黑（idle/labelColor）
                if enabled and session_up:
                    title, kind = f"{lp} → {rp} · 已映射", "ok"
                elif enabled:
                    title, kind = f"{lp} → {rp} · 未连接", "idle"
                else:
                    title, kind = f"{lp} → {rp} · 已停用", "idle"
                if row.title != title:
                    row.title = title
                    _apply_status_dot(row, kind, point_size=8)

    def _set_title_ref(self, row, text):
        if row is not None and row.title != text:
            row.title = text

    def _build_mount_submenu(self):
        """远程挂载 ▸ —— NFS over SSH 挂载项（ADR-007）：跨隧道列出所有
        配置了 NFS 挂载的项，每项启停 + 打开挂载目录。NFS 走独立专用
        会话，无端口映射里「随代理运行」的特殊行。

        UX 批次：行结构恒定（挂载/卸载动作恒在，标签刷新定字），状态
        尾标与异常详情就地刷新——后台挂载态翻转不重建整树；空态行可
        点击深链偏好设置。"""
        st = self._get_state()
        a = self._app
        parent = rumps.MenuItem("远程挂载", callback=None)
        _apply_icon(parent, "mount_menu")

        any_mounts = False
        for entry in (st.mount_states or ()):
            # MountState（NamedTuple 投影）：字段即契约，不再防御式猜形状。
            # 占位标题按 (tid, 挂载名) 唯一——rumps Menu 以标题为键去重
            tid, mname = entry.tunnel_id, entry.name
            any_mounts = True
            row = rumps.MenuItem(f"__mount_row_{tid}_{mname}__",
                                 callback=None)
            _apply_icon(row, "circle", point_size=9)
            self.refs[("mount_row", tid, mname)] = row
            action = rumps.MenuItem(
                f"__mount_action_{tid}_{mname}__",
                callback=a.make_toggle_mount(tid, mname))
            _apply_icon(action, "fw_start")
            self.refs[("mount_action", tid, mname)] = action
            row.add(action)
            item = rumps.MenuItem(
                "打开挂载目录", callback=a.make_open_mount_dir(tid, mname))
            _apply_icon(item, "folder")
            row.add(item)
            parent.add(row)

        if not any_mounts:
            item = rumps.MenuItem("配置挂载…", callback=a.show_prefs_mounts)
            _apply_icon(item, "folder")
            parent.add(item)
        self._refresh_mount_rows(st)
        return parent

    def _refresh_mount_rows(self, st):
        """挂载区动态段：行尾标/圆点/异常详情/挂载-卸载动作标签。"""
        for entry in (st.mount_states or ()):
            row = self.refs.get(("mount_row", entry.tunnel_id, entry.name))
            if row is None:
                continue
            status, error = entry.status, entry.error
            base = f"{entry.tunnel_name} · {entry.name}"
            kind = _mount_status_kind(status)
            if status == "error" and error:
                title = f"{base} · 异常：{_truncate(error, 40)}"
            else:
                tail = _MOUNT_TAIL.get(status)
                title = f"{base} · {tail}" if tail else base
            if row.title != title:
                row.title = title
                _apply_status_dot(row, kind, point_size=9)
            action = self.refs.get(
                ("mount_action", entry.tunnel_id, entry.name))
            if action is not None:
                active = status in ("mounted", "mounting", "unmounting")
                new_title = "卸载" if active else "挂载"
                if action.title != new_title:  # 图标随标题变化才重挂
                    action.title = new_title
                    _apply_icon(action, "fw_stop" if active else "fw_start")

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
        item = rumps.MenuItem("配置 Agent…", callback=a.show_agent_setup)
        _apply_icon(item, "wand.and.stars")
        parent.add(item)
        item = rumps.MenuItem("复制连接地址", callback=a.copy_suanpan_url)
        _apply_icon(item, "doc")
        parent.add(item)
        item = rumps.MenuItem("复制配置样例", callback=a.copy_suanpan_example)
        _apply_icon(item, "clipboard")
        parent.add(item)
        return parent

    def _build_system_submenu(self):
        """选 项 ▸ —— 防睡眠 / 登录启动 / 配置 API（v0.9.1 重组 + ADR-009）。

        UX 批次：组名从「系 统」改为「选 项」——与「系统代理」（代 理 组
        内的 macOS 网络概念）命名撞车，用户扫视时易混淆入口。"""
        a = self._app
        st = self._get_state()
        parent = rumps.MenuItem("选 项", callback=None)
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
        item = rumps.MenuItem(
            st.config_api_title, callback=a.toggle_config_api)
        _apply_icon(item, "network")
        self.refs["config_api"] = item
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
        elif tunnel:
            # 断开也保留隧道名——此刻恰恰更需要知道当前配的是谁
            proxy_text = f"AI Proxy · {tunnel_name} · 未连接"
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
            proxy_text += f" ｜ {mounts_up} 个挂载"
        # 故障可见性（UX 批次）：异常不数成功、顶部无感知的时代结束——
        # 转发/挂载的 error 态在状态行立即可见，不必逐层展开子菜单
        fw_bad = sum(1 for f in (st.forward_states or ())
                     if f.status == "error")
        if fw_bad:
            proxy_text += f" ｜ ⚠ {fw_bad} 转发异常"
        mounts_bad = sum(1 for entry in (st.mount_states or ())
                         if entry.status == "error")
        if mounts_bad:
            proxy_text += f" ｜ ⚠ {mounts_bad} 挂载异常"
        self._set_title("proxy_status", proxy_text)

        # Router status line —— 原始错误串不进菜单（截断读不完也无法
        # 复制）；短状态 + 详情走日志窗
        if st.suanpan_running:
            router_text = f"AI Router · {st.suanpan_listen_address}"
        elif st.suanpan_error:
            router_text = "AI Router · 启动失败"
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
        self._set_title("config_api", st.config_api_title)

        # 端口映射 / 挂载动态段（UX 批次）：状态翻转就地刷新，不重建
        self._refresh_forward_rows(st)
        self._refresh_mount_rows(st)

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
