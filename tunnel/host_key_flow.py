"""SSH host-key trust flow controller.

Manages the three-phase host-key lifecycle:
1. First-connect fingerprint verification (inspect → prompt → trust → connect)
2. Key-change replacement detection (error → re-scan → prompt → replace → reconnect)
3. Cancellation (generation counter invalidates in-flight callbacks)

Uses rumps.alert for user prompts and AppHelper.callAfter for main-thread
dispatch. Owns 3 ivars that were previously on MagicProxyApp.
"""
import logging
import threading

import rumps
from tunnel import host_key
from PyObjCTools import AppHelper

logger = logging.getLogger("magic-proxy.host_key_flow")


class HostKeyFlow:
    """Coordinates SSH host-key trust decisions with UI prompts.

    Args:
        ssh_monitor: SSHMonitor (for set_status/set_error/start)
        get_tunnel: callable → current tunnel dict or None
        get_socks5_port: callable → int
        get_password: callable → str (tunnel password)
        on_connect: callable() — invoked when host-key check passes and
                     SSH should start (e.g. self._ssh.start(tunnel, port, pw))
        on_reconnect: callable() — invoked after key replacement succeeds
    """

    def __init__(self, ssh_monitor, get_tunnel, get_socks5_port,
                 get_password, on_connect, on_reconnect):
        self._ssh = ssh_monitor
        self._get_tunnel = get_tunnel
        self._get_port = get_socks5_port
        self._get_password = get_password
        self._on_connect = on_connect
        self._on_reconnect = on_reconnect
        self._generation = 0
        self.checking = False
        self.change_prompted = False

    def start_check(self):
        """Begin host-key inspection for the current tunnel.
        Sets SSH to 'connecting' and launches background inspection."""
        tunnel = self._get_tunnel()
        if not tunnel:
            return
        self._generation += 1
        generation = self._generation
        self.checking = True
        self._ssh.set_status("connecting")

        def inspect_in_background():
            try:
                result = host_key.inspect(tunnel)
            except Exception as e:  # noqa: BLE001 — SSH must not hang in "connecting"
                logger.exception("host-key inspection crashed")
                result = (False, None, None, f"{type(e).__name__}: {e}")
            AppHelper.callAfter(self._finish_check, generation, tunnel, result)

        threading.Thread(
            target=inspect_in_background, name="MagicProxyHostKey", daemon=True,
        ).start()

    def _finish_check(self, generation, tunnel, inspection):
        if generation != self._generation:
            return
        self.checking = False
        known, keys, fingerprints, err = inspection
        if not known:
            if err:
                self._ssh.set_status("error")
                self._ssh.set_error(err)
                rumps.alert(title="SSH 主机密钥检查失败", message=err)
                return
            result = rumps.alert(
                title="确认 SSH 服务器指纹",
                message=(
                    f"服务器：{tunnel.get('ssh_host')}:{tunnel.get('ssh_port', 22)}\n\n"
                    f"SHA256 指纹：\n{fingerprints}\n\n"
                    "请通过可信渠道核对指纹。确认后，Magic AI Router 将严格固定此主机密钥。"
                ),
                ok="信任并连接", cancel="取消",
            )
            if result != 1 or not host_key.accept(keys):
                self._ssh.set_status("stopped")
                logger.info("SSH host-key enrollment cancelled or failed")
                return
        if generation == self._generation:
            self.change_prompted = False
            self._on_connect()

    def begin_replacement(self):
        """Detect key change: re-scan and prompt user to replace."""
        tunnel = self._get_tunnel()
        if not tunnel:
            return
        self.change_prompted = True
        self._generation += 1
        generation = self._generation

        def scan_changed_key():
            try:
                result = host_key.inspect(tunnel, force_scan=True)
            except Exception as e:  # noqa: BLE001 — silent thread death left SSH stuck
                logger.exception("host-key change scan crashed")
                result = (False, None, None, f"{type(e).__name__}: {e}")
            AppHelper.callAfter(self._finish_replacement, generation, tunnel, result)

        threading.Thread(
            target=scan_changed_key, name="MagicProxyHostKeyChange", daemon=True,
        ).start()

    def _finish_replacement(self, generation, tunnel, inspection):
        if generation != self._generation:
            return
        _known, keys, fingerprints, err = inspection
        if err:
            rumps.alert(title="无法获取新的 SSH 指纹", message=err)
            return
        result = rumps.alert(
            title="SSH 主机密钥已变化",
            message=(
                f"服务器：{tunnel.get('ssh_host')}:{tunnel.get('ssh_port', 22)}\n\n"
                f"新的 SHA256 指纹：\n{fingerprints}\n\n"
                "这可能是服务器重装，也可能是中间人攻击。请通过可信渠道核对后再替换。"
            ),
            ok="已核对，替换", cancel="取消",
        )
        if result == 1 and host_key.replace(tunnel, keys):
            self.change_prompted = False
            self._on_reconnect()
        else:
            self._ssh.set_status("stopped")

    def cancel(self):
        """Invalidate all in-flight callbacks via generation bump."""
        self._generation += 1
        self.checking = False
        self.change_prompted = False
