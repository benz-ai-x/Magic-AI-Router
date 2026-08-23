#!/usr/bin/env python3
"""Magic AI Router — macOS menu bar app for HTTP→SOCKS5 over SSH tunnel."""
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time

from AppKit import NSApplication, NSMenu, NSMenuItem
import rumps

from capture import ca_trust
from capture import chromium_proxy
from sysctl import keychain
from sysctl import login_item
from mpconf import netloc
from sysctl import port_check
from shellui.bridge_protocol import (ACTION_COPY_AGENT_INSTRUCTIONS,
    ACTION_OPEN_PATH, ACTION_RECONNECT_PROXY)
from capture.capture import DEFAULT_CAPTURE_DIR, DEFAULT_CAPTURE_PORT
from mpconf.config import (  # noqa: F401 — DEFAULT_CONFIG 是模块导出符号
    DEFAULT_CONFIG, IdentityMigrationError, load_config, merge_config)
from shellui.log_window import LogBuffer, show_log_window
from shellui.webview_window import show_config_window
from shellui.menu_builder import MenuBuilder, MenuState, _status_color_for_connection
from mpconf.config_state import ConfigStateStore
from services.stats import Stats
from tunnel.connection_coordinator import ConnectionCoordinator
from services.lifecycle_runtime import LifecycleRuntime
from util import build_stamp, version_display, resource_path

LOG_DIR = os.path.expanduser("~/Library/Logs")
LOG_PATH = os.path.join(LOG_DIR, "MagicProxy.log")
VERSION = "0.6.1"
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




class MagicProxyApp(rumps.App):
    def __init__(self):
        try:
            cfg = load_config()
        except IdentityMigrationError as exc:
            # 迁移可行动错误（显式重复 id）：绝不带病运行——弹窗给出
            # 处置指引后退出，原配置文件未被动过
            rumps.alert(
                "Magic AI Router",
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

        # Non-blocking quit→relaunch state machine for proxied app launches
        self._relaunch_waiter = None

        # Services (AI router + capture + system proxy + sleep + config
        # server): LifecycleRuntime 持有全部构造/启动/退出顺序（架构候选
        # 2+3）——app 只经合法属性面取子模块引用，不再两阶段构造、不再
        # 私有属性掏取，「抓包正在运行」在 lifecycle 内单一投影。
        self._lifecycle = LifecycleRuntime(
            config_fn=lambda: self._config,
            ssh_monitor=self._conn.ssh,
            paused_fn=lambda: self._conn.paused,
            on_menu_dirty=lambda: setattr(self._menu_builder, "last_struct_key", None),
            initial_sys_proxy_on=self._config.get("system_proxy_default", False),
        )
        self._suanpan = self._lifecycle.suanpan
        self._capture_ctrl = self._lifecycle.capture_ctrl
        self._sys_proxy = self._lifecycle.sys_proxy
        self._capture = self._lifecycle.capture
        self._config_server = self._lifecycle.config_server
        if not self._lifecycle.start_all():
            # 单实例守卫失败（issue #3）：用户可见的清晰错误，绝不以
            # 僵尸实例形态继续起菜单。
            rumps.alert("Magic AI Router",
                        "已有 Magic AI Router 实例在运行。\n\n"
                        "本次启动已退出——请通过菜单栏使用现有实例，"
                        "或先退出它再重新启动。")
            raise SystemExit(0)

        # Menu
        self._menu_builder = MenuBuilder(
            self, self._make_menu_state)

        super().__init__(
            name="Magic AI Router",
            title="⚫",
            quit_button=None,
        )
        self._install_edit_menu()
        self._menu_builder.build()

        if cfg is None or not self._config.get("tunnels"):
            self.show_preferences(None)
        else:
            self.check_both_ports()
            self._conn.start()

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
        if tunnel and tunnel.get("auth_type") == "password":
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
            capture_menu_title=cap.menu_title(),
            capture_error_hint=cap.error_hint(),
            suanpan_running=sp.running,
            suanpan_error=sp.error,
            suanpan_listen_address=sp.listen_address() if sp.running else "",
            current_tunnel=self._conn.current_tunnel,
            prevent_sleep_title="防睡眠：开" if self._config.get("prevent_sleep") else "防睡眠：关",
            launch_login_title="登录启动：开" if self._config.get("launch_at_login") else "登录启动：关",
        )

    # ── tick ─────────────────────────────────────────────

    def _on_tick(self, _):
        self._stats.tick()
        self._conn.handle_retry()

        # Set icon from pre-check status (matches original ordering)
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

        # Services
        self._lifecycle.tick(self._config.get("capture_port", DEFAULT_CAPTURE_PORT))
        self._lifecycle.sync_sleep(s, self._conn.paused,
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
            rumps.alert(title="Magic AI Router",
                        message=f"{w.name} 未能及时退出，请手动退出后重试。")
            return
        ok, err = chromium_proxy.launch(w.path, payload)
        if not ok:
            rumps.alert(title="Magic AI Router",
                        message=f"启动失败：\n\n{err}")
            return
        rumps.alert(
            title="Magic AI Router",
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
                "Magic AI Router",
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
        rumps.notification("Magic AI Router", subtitle, message)

    # ── connection ───────────────────────────────────────

    def cancel_connection(self, _):
        self._conn.cancel()

    def reconnect(self, _):
        def reload_cfg():
            try:
                cfg = load_config()
            except IdentityMigrationError as exc:
                # 与 __init__ 的处置一致：迁移可行动错误不得在菜单回调里
                # 裸抛——保持现有连接并给出指引。桥接重连在 daemon 线程
                # （#68）：NSAlert 必须回主线程（host_key_flow 同款
                # AppHelper.callAfter 正解）。
                from PyObjCTools import AppHelper
                AppHelper.callAfter(
                    rumps.alert,
                    "Magic AI Router",
                    f"配置包含重复的隧道 id，已保持现有连接。\n\n{exc}\n\n"
                    "请打开配置文件修正重复 id 后重试。")
                return
            if cfg:
                self._config = merge_config(cfg)
        self._conn.restart(reload_cfg)
        self._dirty()

    def toggle_pause(self, _):
        self._conn.toggle_pause()
        self._sys_proxy.sync()
        self._lifecycle.sync_sleep(self._conn.ssh.status, self._conn.paused,
                             self._config.get("prevent_sleep", False))

    def toggle_system_proxy(self, _):
        self._sys_proxy.toggle()

    def make_switch_tunnel(self, idx):
        def switch(_):
            if idx == self._config.get("current_tunnel", 0) and self._conn.ssh.status == "connected":
                return
            if not self._update_mp_config(
                    lambda c: {**c, "current_tunnel": idx}):
                return
            self.reconnect(None)
        return switch

    # ── suanpan ──────────────────────────────────────────
    # SuanpanRuntime 的公开方法组装直接写在 App 的菜单回调里，不再多一层
    # 间接（#43 重构落地）。

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
            rumps.alert(title="Magic AI Router", message=f"无法设置登录启动：\n\n{err}")
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
            self._capture_ctrl.disable()
            self._dirty()
            return
        capture_port = self._config.get("capture_port", DEFAULT_CAPTURE_PORT)
        if not self._check_port(capture_port, "抓包"):
            return
        if ca_trust.is_trusted():
            self._enable_capture_or_alert()
            return

        def on_result(trusted):
            if trusted:
                self._enable_capture_or_alert()
            else:
                self._dirty()

        ca_trust.show_ca_trust_guide(on_result=on_result)

    def _enable_capture_or_alert(self):
        if not self._capture_ctrl.enable():
            rumps.alert(title="Magic AI Router",
                        message="找不到 mitmdump 可执行文件，无法开启抓包模式。")
        self._dirty()

    def open_capture_dir(self, _):
        from capture import capture_store
        try:
            # #70 W13：经 prepare 带标记建目录——裸 makedirs（无标记、
            # 0755）曾让 prepare 拒「非本应用创建的现有目录」，抓包模式
            # 永远无法启动（菜单动作打败自家安全契约）
            d = capture_store.prepare(
                self._config.get("capture_dir", DEFAULT_CAPTURE_DIR))
            subprocess.Popen(["open", d])
        except OSError:
            actions_log.exception("Failed to open capture dir")

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
        rumps.alert(title="Magic AI Router",
                    message=f"版本 v{self.VERSION_DISPLAY}\nSSH 隧道 HTTP→SOCKS5 代理菜单栏应用")

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
            rumps.alert(title="Magic AI Router", message=f"未找到 {name}.app")
            return
        http_listen = netloc.format_listen("127.0.0.1", int(self._config["http_listen_port"]))
        if chromium_proxy.is_running(path):
            resp = rumps.alert(
                title="Magic AI Router",
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
            rumps.alert(title="Magic AI Router", message=f"启动失败：\n\n{err}")
            return
        rumps.alert(
            title="Magic AI Router",
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
        if rumps.alert(title="Magic AI Router", message=msg, ok="是，Kill", cancel="否") != 1:
            return False
        ok, err = port_check.kill(owner.pid)
        if not ok:
            rumps.alert(title="Magic AI Router", message=f"Kill 失败: {err}")
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
        """Open the web-based config panel in a webview window."""
        try:
            if not self._config_server.start():
                rumps.alert(title="Magic AI Router", message="配置服务端口被占用，无法打开设置。")
                return
            show_config_window(
                self._config_server.url, on_action=self._bridge_action,
                auth_headers={"Authorization":
                              f"Bearer {self._config_server.token}"})
        except Exception as e:
            logger.exception("show_preferences failed")
            rumps.alert(title="Magic AI Router", message=f"打开设置失败:\n\n{e!r}")

    def _copy_agent_instructions(self):
        """原生侧拼装 AI 助手指令（#70 S13）：token 不出原生进程——
        JS 侧 URLSearchParams 在 #10 删 query 认证后恒空，复制出的
        指令带空 Bearer 必 401。此处持 expected_token 直接上剪贴板。"""
        token = self._config_server.token
        url = self._config_server.url
        text = (
            "我在用 Magic AI Router（macOS 菜单栏应用）。\n"
            f"请先读 {url}agent.md 了解产品功能和配置方法。\n"
            "当前配置 API（需要 token）：\n"
            f'  curl -H "Authorization: Bearer {token}" {url}api/state\n'
            "你可以通过这个 API 读取和修改我的配置，帮我完成设置。")
        proc = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
        proc.communicate(text.encode())
        self._notify("已复制 AI 助手指令", "含 token 的 curl 已就绪")

    def _bridge_action(self, action):
        """App-level bridge actions from the settings window.

        Arrives on the main thread via WKScriptMessageHandler. The reconnect
        path blocks up to ~10 s (subprocess joins) — dispatch to a daemon
        thread so the window and menu stay responsive; the cross-thread call
        is safe（#68：ConnectionCoordinator 的 _lifecycle_lock 已归状态机
        所有者——注释从民俗变机制；on_sp_saved 同样跨线程）。
        """
        kind = action.get("type")
        if kind == ACTION_RECONNECT_PROXY:
            threading.Thread(target=self.reconnect, args=(None,),
                             name="BridgeReconnect", daemon=True).start()
        elif kind == ACTION_OPEN_PATH and action.get("kind") == "captureDir":
            self.open_capture_dir(None)
        elif kind == ACTION_COPY_AGENT_INSTRUCTIONS:
            self._copy_agent_instructions()

    def quit_app(self, _):
        # 退出顺序契约由 LifecycleRuntime.quit 持有（系统代理恢复先于 SSH 停止）
        self._lifecycle.quit(self._conn.stop_all)
        rumps.quit_application()


if __name__ == "__main__":
    if os.environ.get("MAGIC_PROXY_SMOKE_TEST") == "1":
        logger.info("Magic AI Router smoke import OK: v%s", VERSION)
        # frozen 冒烟（issue #2）：契约解析 + 实际 spawn bundled mitmdump
        # 加载 addon，判据单一归宿在 capture.resources；失败原因直达
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
