"""System proxy convergence controller.

Edge-triggered state machine that converges macOS system proxy settings
(networksetup) to match the desired state: (on AND connected AND not paused).
Extracted from MagicProxyApp to isolate 6 ivars + ~120 lines of proxy
transaction logic.
"""
import logging

from sysctl import system_proxy
from capture.capture_store import DEFAULT_CAPTURE_PORT

logger = logging.getLogger("magic-proxy.sys_proxy_ctrl")


class SystemProxyController:
    """Owns the system proxy apply/release lifecycle.

    Args:
        ssh_monitor: SSHMonitor (reads .status)
        capture_state: callable → (capture_enabled: bool, capture_status: str)
        config_fn: callable → current config dict
        paused_fn: callable → bool (whether proxy is paused)
        on_dirty: callback when menu needs rebuild (optional)
        initial_on: initial on/off state from config
    """

    def __init__(self, ssh_monitor, capture_state, config_fn, paused_fn,
                 on_dirty=None, initial_on=False):
        self._ssh = ssh_monitor
        self._capture_state = capture_state
        self._config_fn = config_fn
        self._paused_fn = paused_fn
        self._on_dirty = on_dirty
        self._on = initial_on
        self._applied = False
        self._applied_target = None
        self._error = ""
        self._snapshot = None
        self._desired = None
        recovered, recover_err = system_proxy.recover_stale_transaction()
        if not recovered:
            self._error = recover_err
            logger.warning("system proxy recovery deferred: %s", recover_err)

    @property
    def on(self):
        return self._on

    @property
    def error(self):
        return self._error

    def toggle(self):
        self._on = not self._on
        self._error = ""
        self.sync()
        if self._on_dirty:
            self._on_dirty()

    def _target_host(self):
        # HTTP proxy always binds to loopback (config validates this).
        return "127.0.0.1"

    def _target_port(self):
        cap_enabled, cap_status = self._capture_state()
        if cap_enabled and cap_status == "running":
            return self._config_fn().get("capture_port", DEFAULT_CAPTURE_PORT)
        try:
            return int(self._config_fn()["http_listen_port"])
        except (KeyError, TypeError, ValueError):
            return None

    def sync(self):
        """Converge system proxy to (on AND connected AND not paused)."""
        target_host = self._target_host()
        target_port = self._target_port()
        desired = (
            self._on
            and self._ssh.status == "connected"
            and not self._paused_fn()
            and target_port is not None
        )
        if target_port is None and self._on:
            self._error = "invalid http_listen_port"
            logger.warning("system proxy: invalid http_listen_port %r",
                           self._config_fn().get("http_listen_port"))

        target = (target_host, target_port)
        if desired and (not self._applied or self._applied_target != target):
            if not self._applied:
                self._snapshot = system_proxy.snapshot()
                if not self._snapshot:
                    self._error = "could not safely snapshot network proxy settings"
                    logger.warning("system proxy not applied: no readable settings snapshot")
                    return
            ok, err, desired_state = system_proxy.apply_transaction(
                target_host, target_port, system_proxy.DEFAULT_BYPASS,
                self._snapshot,
            )
            self._applied = ok
            self._applied_target = target if ok else None
            self._desired = desired_state if ok else self._desired
            if not ok:
                if desired_state is None:
                    self._snapshot = None
                    self._desired = None
                else:
                    self._desired = desired_state
                    self._on = False
                    err = f"{err}; system proxy turned off due to apply failure"
            self._error = "" if ok else err
            if ok:
                logger.info("system proxy applied: %s:%s", target_host, target_port)
            else:
                logger.warning("system proxy apply failed: %s", err)
        elif not desired and self._snapshot:
            ok, err = system_proxy.release_transaction(
                self._snapshot, self._desired,
            )
            self._applied = not ok
            if ok:
                self._applied_target = None
                self._snapshot = None
                self._desired = None
            self._error = "" if ok else err
            if ok:
                logger.info("system proxy cleared")
            else:
                logger.warning("system proxy clear failed: %s", err)

    def quit_cleanup(self):
        """Release system proxy on app quit."""
        if self._snapshot:
            ok, err = system_proxy.release_transaction(
                self._snapshot, self._desired)
            if not ok:
                logger.warning("system proxy restore on quit failed: %s", err)
            else:
                self._applied = False
                self._snapshot = None
                self._desired = None
