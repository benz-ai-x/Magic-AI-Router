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
from mpconf import config_store
from shellui.bridge_protocol import ACTION_OPEN_PATH, ACTION_RECONNECT_PROXY
from capture.capture import DEFAULT_CAPTURE_DIR, DEFAULT_CAPTURE_PORT
from mpconf.config import load_config, save_config, merge_config, DEFAULT_CONFIG
from services.config_server import ConfigServer
from shellui.log_window import LogBuffer, show_log_window
from shellui.webview_window import show_config_window
from shellui.menu_builder import MenuBuilder, MenuState, _status_color_for_connection
from services.stats import Stats
from tunnel.connection_coordinator import ConnectionCoordinator
from services.service_coordinator import ServiceCoordinator
from util import build_stamp, version_display, resource_path

LOG_DIR = os.path.expanduser("~/Library/Logs")
LOG_PATH = os.path.join(LOG_DIR, "MagicProxy.log")
VERSION = "0.4.9"
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


def _is_stale_instance(cmd):
    """True if a port owner's command line is a previous instance of THIS app.

    #40: matches the packaged binary name or the dev-mode script (basename
    of sys.argv[0]) as a path component ("…/Magic AI Router" — survives names
    with spaces) or as a standalone whitespace-delimited token ("python3
    app.py") — never a bare substring — so a foreign service that merely
    contains a similar word is spared.
    """
    if not cmd:
        return False
    script = os.path.basename(sys.argv[0])
    if not script:
        return False
    return "/" + script in cmd or script in cmd.split()


def _clear_app_ports(config_port=9528, suanpan_port=9527):
    """Kill stale previous instances of this app on its own ports.

    #40: only processes whose command line identifies this app are killed.
    A foreign service that happens to listen on 9527/9528 is spared (and
    warned about) — killing it would be destroying someone else's process.
    """
    self_pid = os.getpid()
    for port in (config_port, suanpan_port):
        owner = port_check.who_owns(port)
        if not owner or owner.pid == self_pid:
            continue
        if not _is_stale_instance(owner.cmd):
            logger.warning(
                "Port %d occupied by PID %d (%s) — not our process, leaving it alone",
                port, owner.pid, owner.name)
            continue
        logger.info("Port %d occupied by PID %d (%s) — killing",
                    port, owner.pid, owner.name)
        ok, err = port_check.kill(owner.pid)
        if ok:
            logger.info("Killed PID %d on port %d", owner.pid, port)
        else:
            logger.warning("Failed to kill PID %d on port %d: %s",
                           owner.pid, port, err)


class MagicProxyApp(rumps.App):
    def __init__(self):
        cfg = load_config()
        self._config = merge_config(cfg)
        self._stats = Stats()
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

        # Services (AI router + capture + system proxy + sleep)
        # Candidate-1: ServiceCoordinator 不再暴露 suanpan / capture_ctrl /
        # sys_proxy / capture 直通属性，MagicProxyApp 直接持有子模块。
        # F7 修复：原先 capture_state_fn 闭包引用了「正在构造中的 self._svc」，
        # 现在两阶段构造——先以占位 capture_state_fn 构造 ServiceCoordinator，
        # 拿到直属子模块引用后再补设真正的 capture_state_fn。
        self._svc = ServiceCoordinator(
            config_fn=lambda: self._config,
            ssh_monitor=self._conn.ssh,
            capture_state_fn=lambda: (False, ""),
            paused_fn=lambda: self._conn.paused,
            on_menu_dirty=lambda: setattr(self._menu_builder, "last_struct_key", None),
            initial_sys_proxy_on=self._config.get("system_proxy_default", False),
        )
        self._suanpan = self._svc._suanpan
        self._capture_ctrl = self._svc._capture_ctrl
        self._sys_proxy = self._svc._sys_proxy
        self._capture = self._svc._capture
        # 现在直属属性都已绑定，补设真正读取 capture_ctrl 的 capture_state_fn。
        self._svc.set_capture_state_fn(
            lambda: (self._capture_ctrl.enabled, self._capture_ctrl.status))

        # Config server
        config_port = self._config.get("config_port", 9528)
        self._config_server = ConfigServer(
            on_sp_saved=lambda: self._suanpan.reload(), port=config_port,
            capture_state=lambda: (
                self._capture_ctrl.enabled
                and self._capture_ctrl.status == "running"))
        sp_port = self._read_suanpan_port()
        _clear_app_ports(config_port, sp_port)
        self._config_server.start()

        # AI router gateway auto-starts with the app (loopback-only);
        # users can still stop it from the AI 路由 menu.
        sp = self._suanpan
        if not sp.start():
            logger.warning("Suanpan gateway auto-start failed: %s", sp.error[:120])

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
    def _read_suanpan_port():
        """Read the gateway port via 配置存储; fallback to 9527."""
        try:
            return netloc.parse_listen(config_store.suanpan_listen(), default_port=9527)[1]
        except Exception:
            return 9527

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
        self._svc.tick(self._config.get("capture_port", DEFAULT_CAPTURE_PORT))
        self._svc.sync_sleep(s, self._conn.paused,
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

    def _notify(self, subtitle, message=""):
        rumps.notification("Magic AI Router", subtitle, message)

    # ── connection ───────────────────────────────────────

    def cancel_connection(self, _):
        self._conn.cancel()

    def reconnect(self, _):
        def reload_cfg():
            cfg = load_config()
            if cfg:
                self._config = merge_config(cfg)
        self._conn.restart(reload_cfg)
        self._dirty()

    def toggle_pause(self, _):
        self._conn.toggle_pause()
        self._sys_proxy.sync()
        self._svc.sync_sleep(self._conn.ssh.status, self._conn.paused,
                             self._config.get("prevent_sleep", False))

    def toggle_system_proxy(self, _):
        self._sys_proxy.toggle()

    def make_switch_tunnel(self, idx):
        def switch(_):
            if idx == self._config.get("current_tunnel", 0) and self._conn.ssh.status == "connected":
                return
            self._config["current_tunnel"] = idx
            save_config(self._config)
            self.reconnect(None)
        return switch

    # ── suanpan ──────────────────────────────────────────
    # Candidate-1: 原先 ServiceCoordinator.toggle_suanpan / reload_suanpan /
    # restart_suanpan 把 SuanpanRuntime 的公开方法封装成
    # (running, address, error) / (ok, error) 元组。现在这些组装直接写在
    # App 的菜单回调里，不再多一层间接。

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
        self._config["prevent_sleep"] = not self._config.get("prevent_sleep", False)
        save_config(self._config)
        self._svc.sync_sleep(self._conn.ssh.status, self._conn.paused,
                             self._config.get("prevent_sleep", False))
        self._dirty()

    def toggle_launch_at_login(self, _):
        enabled = not self._config.get("launch_at_login", False)
        ok, err = login_item.set_launch_at_login(enabled)
        if not ok:
            rumps.alert(title="Magic AI Router", message=f"无法设置登录启动：\n\n{err}")
            self._dirty()
            return
        self._config["launch_at_login"] = enabled
        save_config(self._config)
        self._dirty()
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
        d = os.path.expanduser(self._config.get("capture_dir", DEFAULT_CAPTURE_DIR))
        try:
            os.makedirs(d, exist_ok=True)
            subprocess.Popen(["open", d])
        except OSError:
            actions_log.exception("Failed to open capture dir")

    def open_today_jsonl(self, _):
        d = os.path.expanduser(self._config.get("capture_dir", DEFAULT_CAPTURE_DIR))
        today = time.strftime("%Y-%m-%d")
        path = os.path.join(d, f"{today}.jsonl")
        try:
            os.makedirs(d, exist_ok=True)
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
            show_config_window(self._config_server.auth_url,
                               on_action=self._bridge_action)
        except Exception as e:
            logger.exception("show_preferences failed")
            rumps.alert(title="Magic AI Router", message=f"打开设置失败:\n\n{e!r}")

    def _bridge_action(self, action):
        """App-level bridge actions from the settings window.

        Arrives on the main thread via WKScriptMessageHandler. The reconnect
        path blocks up to ~10 s (subprocess joins) — dispatch to a daemon
        thread so the window and menu stay responsive; the cross-thread call
        is safe (ConnectionCoordinator owns its locking, and on_sp_saved
        already crosses threads the same way).
        """
        kind = action.get("type")
        if kind == ACTION_RECONNECT_PROXY:
            threading.Thread(target=self.reconnect, args=(None,),
                             name="BridgeReconnect", daemon=True).start()
        elif kind == ACTION_OPEN_PATH and action.get("kind") == "captureDir":
            self.open_capture_dir(None)

    def quit_app(self, _):
        # Match original close order: sys_proxy cleanup BEFORE ssh stop
        self._sys_proxy.quit_cleanup()
        self._conn.stop_all()
        self._svc.stop_all()
        self._config_server.stop()
        rumps.quit_application()


if __name__ == "__main__":
    if os.environ.get("MAGIC_PROXY_SMOKE_TEST") == "1":
        logger.info("Magic AI Router smoke import OK: v%s", VERSION)
    else:
        MagicProxyApp().run()
