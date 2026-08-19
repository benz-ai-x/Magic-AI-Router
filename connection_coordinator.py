"""Connection lifecycle coordinator: SSH tunnel + proxy runtime + retry + host-key.

Owns the connection state machine that was previously scattered across MagicProxyApp.
The App delegates start/stop/reconnect/pause to this module.

Interface:
  start()           — start proxy background + SSH connection sequence
  handle_retry()    — check retry scheduler, connect if due (call before icon)
  check_ssh()       — SSH status check + host-key handling (call after icon)
  restart(cfg_fn)   — full stop + config reload + restart
  cancel()          — cancel in-flight connection
  toggle_pause()    — pause/resume; returns new paused state
  stop_all()        — stop everything for quit

Properties: ssh, paused, proxy_running, current_tunnel, socks5_port
"""
from __future__ import annotations

import logging

from proxy import ProxyRuntime, SSHMonitor
from retry_scheduler import RetryScheduler
from host_key_flow import HostKeyFlow
from stats import Stats

logger = logging.getLogger("magic-proxy.connection")


class ConnectionCoordinator:
    """Own SSH tunnel + proxy runtime + retry + host-key lifecycle."""

    def __init__(
        self,
        stats: Stats,
        ssh_log_sink,
        get_config,
        get_tunnel_password,
    ):
        self._ssh = SSHMonitor(line_sink=ssh_log_sink)
        self._proxy_runtime = ProxyRuntime(stats)
        self._retry = RetryScheduler()
        self._proxy_running = False
        self._paused = False
        self._get_config = get_config
        self._get_tunnel_password = get_tunnel_password
        self._host_key = HostKeyFlow(
            ssh_monitor=self._ssh,
            get_tunnel=lambda: self.current_tunnel,
            get_socks5_port=lambda: self.socks5_port,
            get_password=lambda: (
                self._get_tunnel_password(self.current_tunnel)
                if self.current_tunnel else ""
            ),
            on_connect=lambda: self._ssh.start(
                self.current_tunnel, self.socks5_port,
                self._get_tunnel_password(self.current_tunnel)),
            on_reconnect=self.start_ssh,
        )

    # ── config-derived properties ───────────────────────

    @property
    def _config(self):
        return self._get_config()

    @property
    def current_tunnel(self):
        idx = self._config.get("current_tunnel", 0)
        tunnels = self._config.get("tunnels", [])
        return tunnels[idx] if 0 <= idx < len(tunnels) else None

    @property
    def socks5_port(self):
        return self._config.get("socks5_port", 1080)

    # ── public properties ───────────────────────────────

    @property
    def ssh(self):
        return self._ssh

    @property
    def paused(self):
        return self._paused

    @property
    def proxy_running(self):
        return self._proxy_running

    # ── lifecycle ───────────────────────────────────────

    def start(self):
        """Start proxy background + initiate SSH connection sequence."""
        self._start_background()
        self.start_ssh()

    def start_ssh(self):
        self._retry.cancel()
        self._paused = False
        self._host_key.start_check()

    def handle_retry(self):
        """Check retry scheduler; connect if due. Call before setting status icon."""
        if self._retry.consume_due():
            self._retry_connect()

    def check_ssh(self):
        """Check SSH status and handle errors. Call after setting status icon."""
        if self._paused:
            return
        if self._ssh.status not in ("connecting", "connected", "stopped"):
            return
        self._ssh.check(self.socks5_port)
        if self._ssh.status == "connected":
            self._retry.reset()
        elif self._ssh.status == "error":
            if self._ssh.is_host_key_changed and not self._host_key.change_prompted:
                self._host_key.begin_replacement()
            else:
                self._retry.handle_error()

    def restart(self, reload_config_fn):
        """Full stop + config reload + restart."""
        self._retry.cancel()
        self._host_key.cancel()
        self._ssh.stop()
        self._proxy_running = False
        self._proxy_runtime.stop()
        reload_config_fn()
        self._start_background()
        self.start_ssh()

    def cancel(self):
        """Cancel an in-flight SSH connection attempt."""
        self._retry.cancel()
        self._host_key.cancel()
        self._ssh.stop()
        self._proxy_running = False
        self._proxy_runtime.stop()
        logger.info("connection cancelled by user")

    def toggle_pause(self):
        """Pause/resume proxy. Returns new paused state."""
        self._paused = not self._paused
        if self._paused:
            # timeout=0: signal the worker and return immediately — joining
            # here blocks the menu callback for up to 5 s (#40).
            self._proxy_runtime.stop(timeout=0)
            self._proxy_running = False
        else:
            self._start_background()
            if self._ssh.status in ("stopped", "error"):
                self.start_ssh()
        return self._paused

    def stop_all(self):
        """Stop SSH + proxy for quit (non-blocking)."""
        self._retry.cancel()
        self._host_key.cancel()
        self._ssh.stop(blocking=False)
        self._proxy_runtime.stop()
        self._proxy_running = False

    # ── internals ───────────────────────────────────────

    def _start_background(self):
        if self._proxy_runtime.running:
            return
        proxy_config = {
            "socks5_port": self.socks5_port,
            "http_listen_port": self._config["http_listen_port"],
        }
        self._proxy_running = self._proxy_runtime.start(proxy_config)

    def _retry_connect(self):
        if self._ssh.status in ("connected", "connecting"):
            return
        tunnel = self.current_tunnel
        if tunnel:
            self._ssh.start(tunnel, self.socks5_port,
                            self._get_tunnel_password(tunnel))
