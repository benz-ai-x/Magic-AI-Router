#!/usr/bin/env python3
"""Magic Stack — macOS menu bar app for HTTP→SOCKS5 over SSH tunnel."""
import logging
import logging.handlers
import os
import subprocess
import sys
import time

from AppKit import NSApplication, NSMenu, NSMenuItem, NSApplicationWillTerminateNotification
from Foundation import NSObject, NSNotificationCenter
import rumps

from capture import ca_trust
from capture import chromium_proxy
from shared import keychain
from sysctl import login_item
from shared import netloc
from shared.identity import IdentityMigrationError
from sysctl import port_check
from shellui.bridge_protocol import (ACTION_COPY_AGENT_INSTRUCTIONS,
    ACTION_FORWARD_SESSION, ACTION_NFS_MOUNT_TOGGLE, ACTION_OPEN_PATH,
    ACTION_RECONNECT_PROXY)
from shared.defaults import DEFAULT_CAPTURE_DIR, DEFAULT_CAPTURE_PORT
from mpconf.config import (  # noqa: F401 — DEFAULT_CONFIG 是模块导出符号
    DEFAULT_CONFIG, load_config, merge_config, resolve_mount_dir)
from mount.coordinator import MountCoordinator
from shared.runtime_state import RuntimeProjection
from shellui.log_window import LogBuffer, show_log_window
from shellui.webview_window import show_config_window
from shellui.menu_builder import MenuBuilder, MenuState, _status_color_for_connection
from mpconf.config_state import ConfigStateStore
from shared.stats import Stats
from tunnel.connection_coordinator import ConnectionCoordinator
from tunnel.reconnect_trigger import ReconnectTrigger, WakeEventSource
from services.intents import UserIntents
from services.lifecycle_runtime import LifecycleRuntime, config_server_wanted
from util import build_stamp, version_display, resource_path

LOG_DIR = os.path.expanduser("~/Library/Logs")
LOG_PATH = os.path.join(LOG_DIR, "MagicProxy.log")
VERSION = "0.13.0"
VERSION_DISPLAY = version_display(VERSION, build_stamp())

log_buffer = LogBuffer()


def _setup_logging():
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
    except OSError:
        return
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if log_buffer not in root.handlers:
        root.addHandler(log_buffer)
    if any(isinstance(h, logging.handlers.RotatingFileHandler) for h in root.handlers):
        return
    handler = logging.handlers.RotatingFileHandler(
        LOG_PATH, maxBytes=512 * 1024, backupCount=2,
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    ))
    root.addHandler(handler)


_setup_logging()
logger = logging.getLogger("magic-proxy.app")
actions_log = logging.getLogger("magic-proxy.actions")
ssh_log = logging.getLogger("magic-proxy.ssh")




class _TerminateObserver(NSObject):
    """NSApplicationWillTerminate → app._shutdown()。

    菜单退出（quit_app）与 AppleEvent 退出（osascript quit / 注销 /
    重启）都必经 NSApp.terminate_——本观察者是两条路径的公共咽喉。
    曾有的缺口：清理只挂在菜单回调上，AppleEvent 退出直接跳过——NFS
    会话的 ssh 泄漏成孤儿（PPID=1）占住本地端口，下个实例的 NFS 会话
    在 ExitOnForwardFailure 下永久失败（实测：12049 被孤儿占用致挂载
    死循环）。"""

    def onTerminate_(self, _note):
        app = self._app_ref()
        if app is not None:
            app._shutdown()


class MagicProxyApp(rumps.App):
    def __init__(self):
        try:
            cfg = load_config()
        except IdentityMigrationError as exc:
            # 迁移可行动错误（显式重复 id）：绝不带病运行——弹窗给出
            # 处置指引后退出，原配置文件未被动过
            rumps.alert(
                "Magic Stack",
                f"配置包含重复的隧道 id，无法安全启动。\n\n{exc}\n\n"
                "请打开配置文件修正重复 id 后重启应用。")
            raise SystemExit(1)
        self._config = merge_config(cfg)
        self._stats = Stats()
        # 菜单开关的唯一写径持有者（#46）：与 UI 保存同一事务管线
        self._config_store = ConfigStateStore(keychain=keychain)
        self.VERSION = VERSION
        self.VERSION_DISPLAY = VERSION_DISPLAY
        self._log_path = LOG_PATH
        self._log_buffer = log_buffer

        # Connection lifecycle
        self._conn = ConnectionCoordinator(
            stats=self._stats,
            ssh_log_sink=lambda line: ssh_log.info("ssh| %s", line),
            get_config=lambda: self._config,
            get_tunnel_password=self._tunnel_password,
        )
        # NFS 挂载协调（ADR-007）：专用 NFS 会话 + 挂载生命周期，与端口
        # 转发会话并行互不干扰；resolve_mount_dir 注入——mount 域不横向
        # import mpconf
        self._mounts = MountCoordinator(
            get_config=lambda: self._config,
            get_tunnel_password=self._tunnel_password,
            ssh_log_sink=lambda line: ssh_log.info("nfs| %s", line),
            resolve_mount_dir=resolve_mount_dir,
        )
        # #86：唤醒事件 → 立即重连（跳过退避）。事件源装不上则静默
        # 降级——网络中断场景由 #85 的无限退避兜底。多活：NFS 会话同拍
        # 僵尸重建（唤醒断了所有隧道的 TCP）。
        self._reconnect_trigger = ReconnectTrigger(self._on_wake_event)
        WakeEventSource(self._reconnect_trigger.notify).start()

        # Non-blocking quit→relaunch state machine for proxied app launches
        self._relaunch_waiter = None

        # Services (AI router + capture + system proxy + sleep + config
        # server): LifecycleRuntime 持有全部构造/启动/退出顺序（架构候选
        # 2+3）——app 只经合法属性面取子模块引用，不再两阶段构造、不再
        # 私有属性掏取，「抓包正在运行」在 lifecycle 内单一投影。
        # 运行态投影（架构评审 R3）：app 一处组装，capture/forwards/mounts
        # 三参穿层塌缩为一个 RuntimeProjection seam（懒求值——构造期
        # _capture_ctrl 尚未由 lifecycle 创建，请求时才调用）
        self._lifecycle = LifecycleRuntime(
            config_fn=lambda: self._config,
            ssh_monitor=self._conn.ssh,
            paused_fn=lambda: self._conn.paused,
            on_menu_dirty=lambda: setattr(self._menu_builder, "last_struct_key", None),
            initial_sys_proxy_on=self._config.get("system_proxy_default", False),
            runtime_state_fn=lambda: RuntimeProjection(
                capture_active=self._capture_ctrl.actively_running,
                forwards=tuple(self._conn.forward_sessions()),
                mounts=tuple(self._mounts.mount_states())),
            on_mp_saved=self._on_mp_saved,
        )
        self._suanpan = self._lifecycle.suanpan
        self._capture_ctrl = self._lifecycle.capture_ctrl
        self._sys_proxy = self._lifecycle.sys_proxy
        self._capture = self._lifecycle.capture
        self._config_server = self._lifecycle.config_server
        # 用户意图单一归宿（架构评审 R5 候选 1）：菜单回调与设置窗桥接
        # 两套 adapter 共用——guard 分派/线程纪律/通知/dirty 在 intents
        # 独占，app 只做翻译（菜单从状态推导、桥接从 action 字符串映射）
        self._intents = UserIntents(
            conn=self._conn,
            mounts=self._mounts,
            notify=self._notify,
            mark_dirty=self._dirty,
            update_mp=self._update_mp_config,
            reload_config=self._reload_config_or_alert,
            capture_ctrl=self._capture_ctrl,
            get_capture_dir=lambda: self._config.get(
                "capture_dir", DEFAULT_CAPTURE_DIR),
            alert=lambda message: rumps.alert(
                title="Magic Stack", message=message),
            hold_copy_latch=lambda: self._set_config_holders(
                copy_latch=True),
            get_agent_instructions=self._config_server.agent_instructions,
        )
        # ADR-009 配置服务持有者：设置窗开着 / 复制指令会话闩锁。
        # config_api_enabled 是第三持有者（磁盘真相，经 self._config 读）。
        self._config_window_open = False
        self._copy_api_latch = False
        if not self._lifecycle.start_all():
            # 单实例守卫失败（issue #3）：用户可见的清晰错误，绝不以
            # 僵尸实例形态继续起菜单。
            rumps.alert("Magic Stack",
                        "已有 Magic Stack 实例在运行。\n\n"
                        "本次启动已退出——请通过菜单栏使用现有实例，"
                        "或先退出它再重新启动。")
            raise SystemExit(0)

        # Menu
        self._menu_builder = MenuBuilder(
            self, self._make_menu_state)

        super().__init__(
            name="Magic Stack",
            title="⚫",
            quit_button=None,
        )
        self._install_edit_menu()
        self._menu_builder.build()

        if cfg is None or not self._config.get("servers"):
            self.show_preferences(None)
        else:
            self.check_both_ports()
            self._conn.start()
            # 多活：forward_autostart 的转发会话随应用启动恢复
            self._conn.apply_autostarts()
        # NFS：auto_mount 的挂载项随应用启动恢复（tick 负责补会话）
        self._mounts.apply_autostarts()

        # 退出咽喉：AppleEvent 退出不走菜单回调——统一经
        # NSApplicationWillTerminate 进 _shutdown（见 _TerminateObserver）
        self._shutdown_done = False
        import weakref
        self._terminate_observer = _TerminateObserver.alloc().init()
        self._terminate_observer._app_ref = weakref.ref(self)
        NSNotificationCenter.defaultCenter(
        ).addObserver_selector_name_object_(
            self._terminate_observer, "onTerminate:",
            NSApplicationWillTerminateNotification, None)

        rumps.Timer(self._on_tick, 1).start()

    # ── helpers ──────────────────────────────────────────

    @staticmethod
    def _install_edit_menu():
        """Install standard Edit submenu for text field responder chain."""
        app = NSApplication.sharedApplication()
        main = app.mainMenu()
        if main is None:
            main = NSMenu.alloc().init()
            app.setMainMenu_(main)
        if main.itemWithTitle_("Edit") is not None:
            return
        edit_menu = NSMenu.alloc().initWithTitle_("Edit")
        for title, action, key in (
            ("Copy", "copy:", "c"), ("Paste", "paste:", "v"),
            ("Cut", "cut:", "x"), ("Select All", "selectAll:", "a"),
        ):
            edit_menu.addItem_(
                NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, action, key))
        top = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Edit", None, "")
        top.setSubmenu_(edit_menu)
        main.addItem_(top)

    def _tunnel_password(self, tunnel):
        _auth = (tunnel.get("ssh") or {}).get("auth_type") if tunnel else None
        if _auth == "password":
            return keychain.get_password(tunnel)
        return ""

    # ── menu state ───────────────────────────────────────

    def _make_menu_state(self):
        """Build a frozen snapshot of all data MenuBuilder reads."""
        s = self._conn.ssh
        sp = self._suanpan
        cap = self._capture_ctrl
        sysp = self._sys_proxy
        return MenuState(
            ssh_status=s.status,
            ssh_cmd_str=s.cmd_str,
            ssh_log=s.log if s.status == "connecting" else "",
            ssh_error_msg=s.error_msg,
            paused=self._conn.paused,
            stats_snapshot=self._stats.snapshot(),
            config=self._config,
            sys_proxy_on=sysp.on,
            sys_proxy_error=sysp.error,
            capture_enabled=cap.enabled,
            capture_state=cap.menu_state(),
            capture_hint=cap.hint(),
            suanpan_running=sp.running,
            suanpan_error=sp.error,
            suanpan_listen_address=sp.listen_address() if sp.running else "",
            current_server=self._conn.current_server,
            forward_states=tuple(self._conn.forward_sessions()),
            mount_states=tuple(self._mounts.mount_states()),
        )

    # ── tick ─────────────────────────────────────────────

    def _on_wake_event(self):
        """#86 唤醒 → 代理/转发会话重连 + NFS 会话僵尸重建（同拍）。"""
        self._conn.handle_reconnect_trigger()
        self._mounts.reconnect_now()

    def _on_tick(self, _):
        self._stats.tick()
        self._conn.handle_retry()

        # Set icon from pre-check status (matches original ordering)——
        # 主图标永远反映代理会话（:8888 上游只依赖它）；转发会话的健康
        # 在隧道子菜单逐条呈现
        s = self._conn.ssh.status
        self._menu_builder.set_status_icon(
            _status_color_for_connection(s, self._conn.paused))

        key = self._menu_builder.struct_key()
        if key != self._menu_builder.last_struct_key:
            self._menu_builder.build()
            self._menu_builder.last_struct_key = key
        else:
            self._menu_builder.refresh_titles()

        # SSH check AFTER icon (matches original)
        self._conn.check_ssh()
        self._conn.check_forwards()
        # NFS 挂载收敛（会话健康 + 挂载/卸载 reconcile，不阻塞主线程）
        self._mounts.tick()

        # Services —— 防睡眠按聚合状态：任一会话在跑就不睡（暂停是代理
        # 会话语义，转发会话仍在服务时不因代理暂停而允许睡眠）；挂载在
        # 途/已挂载同理（hard 挂载睡着 = Finder 卡死）
        self._lifecycle.tick(self._config.get("capture_port", DEFAULT_CAPTURE_PORT))
        mounts_active = (self._mounts.any_mounted()
                         or self._mounts.any_session_connected())
        sleep_status = ("connected"
                        if (self._conn.any_connected or mounts_active)
                        else s)
        sleep_paused = (self._conn.paused
                        and not self._conn.any_forward_session_connected
                        and not mounts_active)
        self._lifecycle.sync_sleep(sleep_status, sleep_paused,
                             self._config.get("prevent_sleep", False))

        # Pending proxied-app relaunch (quit → wait → launch)
        self._tick_relaunch()

    def _tick_relaunch(self):
        """Advance the quit→relaunch state machine (never blocks the menu)."""
        w = self._relaunch_waiter
        if w is None:
            return
        action, payload = w.step()
        if action is None:
            return
        self._relaunch_waiter = None
        if action == "timeout":
            rumps.alert(title="Magic Stack",
                        message=f"{w.name} 未能及时退出，请手动退出后重试。")
            return
        ok, err = chromium_proxy.launch(w.path, payload)
        if not ok:
            rumps.alert(title="Magic Stack",
                        message=f"启动失败：\n\n{err}")
            return
        rumps.alert(
            title="Magic Stack",
            message=(f"已经代理启动 {w.name}（→ {payload}）。\n\n"
                     "• 仅本次启动的实例走代理；从 Dock 直接开的不算\n"
                     f"• Magic-Proxy 未运行时 {w.name} 将联网失败、不会直连"),
        )

    # ── menu callbacks ───────────────────────────────────

    def _dirty(self):
        self._menu_builder.last_struct_key = None

    def _update_mp_config(self, mutate):
        """菜单开关唯一写径（#46）：写前读新 + 事务写，成功后刷新内存副本。

        旧径 save_config(self._config) 用启动时的内存副本整文件覆写——
        UI 保存后不重连就点开关，磁盘上 UI 的改动被静默抹掉。现全部经
        ConfigStateStore.update_mp（与 UI 保存同一校验 + journal + 0600
        原子写管线），成功后重读磁盘刷新副本。
        """
        try:
            result = self._config_store.update_mp(mutate)
        except IdentityMigrationError as exc:
            rumps.alert(
                "Magic Stack",
                f"配置包含重复的隧道 id，无法保存本次更改。\n\n{exc}\n\n"
                "请打开配置文件修正重复 id 后重试。")
            return False
        if not result.ok:
            logger.warning("menu config update rejected: %s", result.errors)
            self._notify("配置保存失败", "; ".join(result.errors)[:160])
            return False
        cfg = load_config()
        if cfg:
            self._config = merge_config(cfg)
        self._dirty()
        return True

    def _notify(self, subtitle, message=""):
        rumps.notification("Magic Stack", subtitle, message)

    def _on_mp_saved(self):
        """UI 保存 MP 段后的内存副本收敛（配置服务线程调用）。

        旧缺口：PUT 只落盘 + reload 网关，app 内存副本直到下一次重连才
        重读——防睡眠/抓包设置/代理角色在窗口期全按旧值行动。此处重读
        替换引用后，tick 与各使用点自然收敛（与 reconnect 的 reload_cfg
        同款跨线程纪律，#68）。
        """
        try:
            cfg = load_config()
        except IdentityMigrationError:
            return  # prepare 已拦病态写入，此为防御；旧副本继续服务
        if not cfg:
            return
        new_config = merge_config(cfg)
        old_login = bool(self._config.get("launch_at_login", False))
        new_login = bool(new_config.get("launch_at_login", False))
        self._config = new_config
        self._dirty()
        # 登录启动是唯一的配置外副作用：UI 保存路径此前只写文件不注册
        # LaunchAgent（只有菜单路径注册）——两条写径在此对齐
        if old_login != new_login:
            ok, err = login_item.set_launch_at_login(new_login)
            if not ok:
                logger.warning("UI 保存后同步登录启动失败：%s", err)
        # NFS：新配置的 auto_mount 挂载项收敛补挂（tick 负责补会话）
        self._mounts.apply_autostarts()
        # ADR-009：UI 系统页保存可能翻转 config_api_enabled——按新
        # 持有态收敛 :9528（设置窗此刻开着，服务不会被误停）
        self._sync_config_server()

    # ── connection ───────────────────────────────────────

    def cancel_connection(self, _):
        self._conn.cancel()

    def _reload_config_or_alert(self):
        """重读磁盘配置刷新内存副本（重连 / 单会话重建共用）。

        迁移可行动错误（重复 id）不得在回调里裸抛——保持现有连接并给
        出指引；调用方可能在 daemon 线程（#68），NSAlert 经
        AppHelper.callAfter 回主线程（host_key_flow 同款）。曾有两份
        reload_cfg 闭包一处弹窗一处静默的分叉，处置统一到本方法。
        """
        try:
            cfg = load_config()
        except IdentityMigrationError as exc:
            from PyObjCTools import AppHelper
            AppHelper.callAfter(
                rumps.alert,
                "Magic Stack",
                f"配置包含重复的隧道 id，已保持现有连接。\n\n{exc}\n\n"
                "请打开配置文件修正重复 id 后重试。")
            return
        if cfg:
            self._config = merge_config(cfg)

    def reconnect(self, _):
        self._intents.reconnect_proxy_or_forward()

    def toggle_pause(self, _):
        self._conn.toggle_pause()
        self._sys_proxy.sync()
        self._lifecycle.sync_sleep(self._conn.ssh.status, self._conn.paused,
                             self._config.get("prevent_sleep", False))

    def toggle_system_proxy(self, _):
        self._sys_proxy.toggle()

    def make_switch_server(self, sid):
        """切换代理服务器（v2：proxy_server_id 单一真相）。"""
        def switch(_):
            target = next((t for t in self._config.get("servers", [])
                           if isinstance(t, dict) and t.get("id") == sid), None)
            if target is None:
                return
            if target is self._conn.current_server \
                    and self._conn.ssh.status == "connected":
                return
            if not self._update_mp_config(
                    lambda c: {**c, "proxy_server_id": sid}):
                return
            self.reconnect(None)
        return switch

    # ── 多活转发会话（v0.9） ──────────────────────────────

    def toggle_forward_session(self, tunnel_id):
        """菜单「启动/停止端口转发」：无会话则启，有则停。"""
        def act(_):
            self._intents.toggle_forward_session(tunnel_id)
        return act

    def make_reconnect_tunnel(self, tunnel_id):
        """重连指定隧道：代理隧道走整体 restart（含降级逻辑），转发会话
        单会话重建（显式意图——会话存在即重建，Spec-A 语义）。"""
        def act(_):
            self._intents.reconnect_proxy_or_forward(tunnel_id)
        return act

    def make_toggle_forward(self, tunnel_id, index):
        """菜单「端口映射逐条启停」：意图体在 UserIntents.toggle_forward_row
        （写径 + 守卫重建 + 如实文案——R5 候选 1 收敛）。"""
        def act(_):
            self._intents.toggle_forward_row(tunnel_id, index)
        return act

    # ── NFS 挂载（ADR-007）───────────────────────────────

    def make_toggle_mount(self, tunnel_id, name):
        """菜单「挂载/卸载」：在挂（mounted/mounting/unmounting）则卸，
        其余（unmounted/error）则挂。"""
        def act(_):
            self._intents.toggle_mount(tunnel_id, name)
        return act

    def make_open_mount_dir(self, tunnel_id, name):
        """菜单「打开挂载目录」：Finder 中打开（不存在则先建目录）。"""
        def act(_):
            for t in self._config.get("servers", []):
                if not (isinstance(t, dict) and t.get("id") == tunnel_id):
                    continue
                for row in ((((t.get("services") or {}).get("nfs") or {})
                             .get("mounts")) or []):
                    if isinstance(row, dict) and row.get("name") == name:
                        d = resolve_mount_dir(row)
                        try:
                            os.makedirs(d, exist_ok=True)
                            subprocess.Popen(["open", d])
                        except OSError:
                            actions_log.exception(
                                "Failed to open mount dir %s", d)
                        return
        return act

    # ── suanpan ──────────────────────────────────────────
    # SuanpanRuntime 的公开方法组装直接写在 App 的菜单回调里，不再多一层
    # 间接（LifecycleRuntime 升格落地）。

    def toggle_suanpan(self, _):
        sp = self._suanpan
        if sp.running:
            sp.stop()
            self._notify("AI 路由已停止")
        elif sp.start():
            self._notify("AI 路由已启动", f"http://{sp.listen_address()}")
        else:
            self._notify("AI 路由启动失败", sp.error[:120])
        self._menu_builder.build()

    def copy_suanpan_url(self, _):
        url = f"http://{self._suanpan.listen_address()}"
        proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        proc.communicate(url.encode())
        self._notify("已复制连接地址", url)

    def copy_suanpan_example(self, _):
        path = resource_path("suanpan.example.yaml")
        if not os.path.exists(path):
            self._notify("配置样例", "文件未找到")
            return
        with open(path) as f:
            content = f.read()
        proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        proc.communicate(content.encode())
        self._notify("已复制配置样例", f"{len(content)} 字节")

    def reload_suanpan(self, _):
        sp = self._suanpan
        if sp.reload():
            self._notify("AI 路由配置已重载")
        else:
            self._notify("AI 路由重载失败", sp.error[:120])
        self._menu_builder.build()

    def restart_suanpan(self, _):
        sp = self._suanpan
        if sp.running:
            sp.stop()
        if sp.start():
            self._notify("AI 路由已重启", f"http://{sp.listen_address()}")
        else:
            self._notify("AI 路由重启失败", sp.error[:120])
        self._menu_builder.build()

    # ── sleep / login ────────────────────────────────────

    def toggle_prevent_sleep(self, _):
        if not self._update_mp_config(
                lambda c: {**c,
                           "prevent_sleep": not c.get("prevent_sleep", False)}):
            return
        self._lifecycle.sync_sleep(self._conn.ssh.status, self._conn.paused,
                             self._config.get("prevent_sleep", False))

    def toggle_launch_at_login(self, _):
        # 目标态从磁盘真相推导（#46 复核：内存副本可能滞后于 UI 保存，
        # 与 prevent_sleep 同一口径）
        cfg = load_config()
        enabled = not (cfg or {}).get("launch_at_login", False)
        ok, err = login_item.set_launch_at_login(enabled)
        if not ok:
            rumps.alert(title="Magic Stack", message=f"无法设置登录启动：\n\n{err}")
            self._dirty()
            return
        if not self._update_mp_config(
                lambda c: {**c, "launch_at_login": enabled}):
            return
        self._notify(
            "登录启动：开" if enabled else "登录启动：关",
            "将在下次登录时自动启动。" if enabled else "下次登录不再自动启动。",
        )

    # ── capture ──────────────────────────────────────────

    def toggle_capture(self, _):
        """Toggle capture mode. Off is immediate; on gates through port + CA trust."""
        if self._capture_ctrl.enabled:
            self._intents.set_capture(False)
            return
        capture_port = self._config.get("capture_port", DEFAULT_CAPTURE_PORT)
        if not self._check_port(capture_port, "抓包"):
            return
        if ca_trust.is_trusted():
            self._intents.set_capture(True)
            return

        def on_result(trusted):
            if trusted:
                self._intents.set_capture(True)
            else:
                self._dirty()

        ca_trust.show_ca_trust_guide(on_result=on_result)

    def open_capture_dir(self, _):
        self._intents.open_capture_dir()

    def open_today_jsonl(self, _):
        from capture import capture_store
        try:
            d = capture_store.prepare(
                self._config.get("capture_dir", DEFAULT_CAPTURE_DIR))
            today = time.strftime("%Y-%m-%d")
            path = os.path.join(d, f"{today}.jsonl")
            if os.path.exists(path):
                subprocess.Popen(["open", "-t", path])
            else:
                subprocess.Popen(["open", d])
        except OSError:
            actions_log.exception("Failed to open today's JSONL")

    # ── misc ─────────────────────────────────────────────

    def open_log(self, _):
        try:
            subprocess.Popen(["open", self._log_path])
        except OSError:
            actions_log.exception("Failed to open log")

    def show_log_window(self, _):
        try:
            show_log_window(self._log_buffer)
        except Exception:
            actions_log.exception("show_log_window failed")

    def about(self, _):
        rumps.alert(
            title="Magic Stack",
            message=(
                f"版本 v{self.VERSION_DISPLAY}\n"
                "住进菜单栏的本地 AI 网络栈——路由它，隧道它，看见它。\n"
                "\n"
                "【代 理】SSH 隧道 HTTP→SOCKS5 代理（:8888），多隧道并行\n"
                "　　　　 + ssh -L 端口转发；密码只存钥匙串，断线自动重连，\n"
                "　　　　 系统代理事务式管理，Chromium 应用可单独经代理启动\n"
                "\n"
                "【远程挂载】NFS over SSH：断线自动卸载/恢复重挂，\n"
                "　　　　　 远程一键安装，挂载失败可见可修\n"
                "\n"
                "【AI 路由】三协议网关（:9527）：Anthropic / OpenAI Chat /\n"
                "　　　　 Responses（Codex）入站，路由到 GLM/DeepSeek/Kimi/\n"
                "　　　　 Qwen/OpenAI 等（失配自动转换）；一个 Key 配好\n"
                "　　　　 全部 Agent（Claude Code/Codex/OpenCode/ZCode）；\n"
                "　　　　 缓存感知；用量与余额统计；支持 Docker 部署\n"
                "\n"
                "【抓 包】TLS 解密 AI API（6 家）落 JSONL，其余放行（:8080）\n"
                "\n"
                "【信 任】零遥测，全回环（配置 :9528 按需监听），原子写入，\n"
                "　　　　 2000+ 测试钉住行为契约\n"
                "\n"
                "设置窗（⌘,）配置一切；「复制 AI 助手指令」可让 AI 代配置。\n"
                "开源（MIT）：github.com/benz-ai-x/Magic-AI-Router"))

    # ── proxied app launch ───────────────────────────────

    def make_launch_proxied(self, entry):
        def cb(_):
            self._launch_app_proxied(entry)
        return cb

    def _launch_app_proxied(self, entry):
        """Launch a Chromium app with --proxy-server."""
        name = entry["name"]
        path = entry.get("path") or chromium_proxy.app_path(entry)
        if not path:
            rumps.alert(title="Magic Stack", message=f"未找到 {name}.app")
            return
        http_listen = netloc.format_listen("127.0.0.1", int(self._config["http_listen_port"]))
        if chromium_proxy.is_running(path):
            resp = rumps.alert(
                title="Magic Stack",
                message=(f"{name} 已在运行。需先退出、再经代理重新启动才生效。\n\n"
                         "是否退出并经代理重开？"),
                ok="退出并重开", cancel="取消",
            )
            if not resp:
                return
            chromium_proxy.quit_app(path)
            # Waiting for the process to exit blocks the menu callback for up
            # to 5 s — hand off to the tick loop (see _tick_relaunch).
            self._relaunch_waiter = chromium_proxy.RelaunchWaiter(
                path, name, http_listen)
            return
        ok, err = chromium_proxy.launch(path, http_listen)
        if not ok:
            rumps.alert(title="Magic Stack", message=f"启动失败：\n\n{err}")
            return
        rumps.alert(
            title="Magic Stack",
            message=(f"已经代理启动 {name}（→ {http_listen}）。\n\n"
                     "• 仅本次启动的实例走代理；从 Dock 直接开的不算\n"
                     f"• Magic-Proxy 未运行时 {name} 将联网失败、不会直连"),
        )

    # ── port check ───────────────────────────────────────

    def _check_port(self, port, label):
        """Detect port occupancy; prompt user; kill on confirm."""
        owner = port_check.who_owns(port)
        if owner is None:
            return True
        msg = (f"{label} 端口 {port} 被占用:\n\n"
               f"{owner.name} (PID {owner.pid})\n{owner.cmd[:120]}\n\n是否 Kill 它？")
        if rumps.alert(title="Magic Stack", message=msg, ok="是，Kill", cancel="否") != 1:
            return False
        ok, err = port_check.kill(owner.pid)
        if not ok:
            rumps.alert(title="Magic Stack", message=f"Kill 失败: {err}")
            return False
        return True

    def check_both_ports(self):
        """Run port check on SOCKS5 and HTTP ports from current config."""
        self._check_port(self._conn.socks5_port, "SOCKS5")
        try:
            http_port = int(self._config["http_listen_port"])
        except (KeyError, ValueError, TypeError):
            return
        self._check_port(http_port, "HTTP")

    # ── preferences / quit ───────────────────────────────

    def show_preferences(self, _):
        self._open_config_window("")

    def show_prefs_forwards(self, _):
        """端口映射空态深链：偏好设置 → 服务器（SSH 隧道服务卡转发表）。"""
        self._open_config_window("#servers")

    def show_prefs_mounts(self, _):
        """远程挂载空态深链：偏好设置 → 服务器（NFS 服务卡挂载表）。"""
        self._open_config_window("#servers")

    def show_agent_setup(self, _):
        """ADR-010 M5：菜单「配置 Agent…」深链——设置窗直达快速接入向导。"""
        self._open_config_window("#quickstart")

    def _open_config_window(self, fragment):
        try:
            # ADR-009：设置窗本身是配置服务持有者——先置位再开窗
            # （show_config_window 关旧窗的回调在调用内触发，晚置位会让
            # 旧窗关闭误判"无持有者"而停掉刚要用的服务）。
            # 启停全经 lifecycle 单一归宿（架构评审 C1：删直调
            # config_server.start() 的第二条启动路径）。
            if not self._set_config_holders(window_open=True):
                # 启动失败：服务未在听——刻意只清位不收敛（收敛无益，
                # 常驻开关持有者的收敛交给下一个自然事件）
                self._config_window_open = False
                rumps.alert(title="Magic Stack", message="配置服务端口被占用，无法打开设置。")
                return
            show_config_window(
                self._config_server.url + fragment, on_action=self._bridge_action,
                auth_headers={"Authorization":
                              f"Bearer {self._config_server.token}"},
                on_close=self._on_config_window_closed)
        except Exception as e:
            logger.exception("show_preferences failed")
            # 服务可能已在听——清位必须收敛（R2-4 修复点：曾漏收敛，
            # 零持有者时 :9528 常驻到下一个偶然事件）
            self._set_config_holders(window_open=False)
            rumps.alert(title="Magic Stack", message=f"打开设置失败:\n\n{e!r}")

    def _on_config_window_closed(self):
        """设置窗真关闭（webview_window windowWillClose）→ 释放持有者。"""
        self._set_config_holders(window_open=False)

    def _set_config_holders(self, *, window_open=None, copy_latch=None):
        """ADR-009 持有者唯一写口：置位/清位与 :9528 收敛是一个动作
        （架构评审 R2-4：变更位与收敛位曾靠各调用点配对记性——except
        路径漏收敛，零持有者时服务常驻到下一个偶然事件）。返回收敛
        结果。

        唯一刻意不收敛的路径是开窗启动失败分支（服务未在听，收敛无
        益——常驻开关持有者的收敛交给下一个自然事件）。"""
        if window_open is not None:
            self._config_window_open = window_open
        if copy_latch is not None:
            self._copy_api_latch = copy_latch
        return self._sync_config_server()

    def _sync_config_server(self):
        """ADR-009 持有状态机收敛：三持有者任一在场即监听 :9528，否则
        释放。返回收敛结果（False = 想起但端口被占用）——开窗路径据此
        提示；无变更的收敛（如 UI 保存翻转 config_api_enabled）直接调
        用本方法。"""
        return self._lifecycle.sync_config_server(config_server_wanted(
            self._config_window_open,
            bool(self._config.get("config_api_enabled")),
            self._copy_api_latch))

    def toggle_config_api(self, _):
        """系 统 ▸「配置 API 服务」开关（ADR-009）：目标态从磁盘真相推导
        （#46 口径，与 prevent_sleep/launch_at_login 同款）。"""
        cfg = load_config()
        enabled = not (cfg or {}).get("config_api_enabled", False)
        if not self._update_mp_config(
                lambda c: {**c, "config_api_enabled": enabled}):
            return
        self._sync_config_server()
        self._notify(
            "配置 API 服务：开" if enabled else "配置 API 服务：关",
            "浏览器与 AI 助手可经 :9528 访问。" if enabled
            else "端口已释放；打开设置窗或复制指令时会按需开启。")

    def copy_agent_instructions(self, _):
        """菜单栏页脚「复制 AI 助手指令」（v0.9.1）——免开设置窗直通。
        意图体在 UserIntents（ADR-009 闩锁 + pbcopy + 通知）。"""
        self._intents.copy_agent_instructions()

    def _bridge_action(self, action):
        """App-level bridge actions from the settings window.

        纯翻译层（架构评审 R5 候选 1）：action 字符串 → intents 意图
        调用；guard 分派/线程纪律/通知/dirty 全在 services/intents 的
        单一归宿（此前此处与菜单回调是两套手写 adapter）。消息经
        WKScriptMessageHandler 到主线程；慢操作由 intents 派 daemon
        线程，窗口与菜单保持响应（#68：跨线程调用安全——
        ConnectionCoordinator 的 _lifecycle_lock 已归状态机所有者）。
        """
        kind = action.get("type")
        if kind == ACTION_RECONNECT_PROXY:
            self._intents.reconnect_proxy_or_forward(
                action.get("tunnel_id"),
                guarded=bool(action.get("if_connected")))
        elif kind == ACTION_FORWARD_SESSION:
            tid = action.get("tunnel_id")
            if not tid:
                return
            self._intents.forward_session(tid, action.get("action"))
        elif kind == ACTION_NFS_MOUNT_TOGGLE:
            tid = action.get("tunnel_id")
            name = action.get("name")
            if not tid or not name:
                return
            self._intents.mount(tid, name, action.get("action"))
        elif kind == ACTION_OPEN_PATH and action.get("kind") == "captureDir":
            self.open_capture_dir(None)
        elif kind == ACTION_COPY_AGENT_INSTRUCTIONS:
            self.copy_agent_instructions(None)

    def _shutdown(self):
        """退出清理唯一归宿（幂等）。

        顺序契约：NFS 先卸载（hard 挂载断隧道前必须卸，防 Finder 卡死）
        → 系统代理恢复 → SSH 停止 → 服务线 → 配置服务（后者由
        LifecycleRuntime.quit 持有）。"""
        if getattr(self, "_shutdown_done", False):
            return
        self._shutdown_done = True
        self._mounts.unmount_all()
        self._lifecycle.quit(self._conn.stop_all)

    def quit_app(self, _):
        self._shutdown()
        rumps.quit_application()


if __name__ == "__main__":
    if os.environ.get("MAGIC_PROXY_SMOKE_TEST") == "1":
        logger.info("Magic Stack smoke import OK: v%s", VERSION)
        # mount 域（ADR-007）依赖分析守卫：app.py 顶层 import 会让
        # PyInstaller 把 mount 包编进 PYZ——这里真导入一次，漏收集在
        # 打包冒烟即红，而非装到 /Applications 后首挂载才炸
        from mount import (  # noqa: F401
            coordinator, mount_control, nfs_session, remote_setup)
        # frozen 冒烟（issue #2）：契约解析 + 实际 spawn bundled mitmdump
        # 加载 addon，判据单一归宿在 capture/resources；失败原因直达
        # stderr（windowed 包 logger 不落终端，print 才可见）。
        from capture.resources import (
            CaptureResourcesError, resolve_capture_resources, smoke_capture_boot)
        try:
            ok, detail = smoke_capture_boot(resolve_capture_resources({}))
        except CaptureResourcesError as exc:
            print(f"frozen resource contract FAILED: {exc.msg}", file=sys.stderr)
            raise SystemExit(1)
        if not ok:
            print(f"frozen capture smoke FAILED: {detail}", file=sys.stderr)
            raise SystemExit(1)
        logger.info("frozen capture smoke OK: %s", detail)
    else:
        MagicProxyApp().run()
