"""Capture-mode controller — owns the 抓包模式 (capture mode) state machine.

Mirrors SystemProxyController's discipline: holds the in-memory-only enabled
flag (钉死约束 1), converges the mitmdump subprocess on enable/disable, and
derives menu labels from state. Does NOT touch rumps UI — enable() returns
False when mitmdump can't be resolved, leaving any alert to the caller.
No auto-restart on crash (钉死约束 2): only a user toggle retries.
"""
import logging
import time

from capture import ca_trust
from mpconf import netloc
from capture.capture_store import DEFAULT_CAPTURE_PORT
from util import truncate as _truncate
from capture.resources import CaptureResourcesError, resolve_capture_resources

logger = logging.getLogger("magic-proxy.capture_ctrl")

# menu_title() runs once per UI tick; the `security verify-cert` subprocess
# behind the trust check must not spawn every second at idle. 30s staleness
# is acceptable for a menu label; the toggle path (MagicProxyApp) always does
# a live check, so gating decisions never read this cache.
TRUST_CACHE_TTL = 30.0




class CaptureController:
    """Owns capture-mode enabled state + mitmdump convergence + menu labels.

    Args:
        monitor: CaptureMonitor (subprocess lifecycle).
        config_fn: callable → current config dict.
        on_dirty: callback when the menu needs rebuild (optional).
    """

    def __init__(self, monitor, config_fn, on_dirty=None):
        self._monitor = monitor
        self._config_fn = config_fn
        self._on_dirty = on_dirty
        self._enabled = False  # in-memory ONLY (钉死约束 1)
        self._preflight_error = None
        self._trust_cache = None  # (expiry_monotonic, trusted: bool) | None

    @property
    def enabled(self):
        return self._enabled

    @property
    def status(self):
        return self._monitor.status

    @property
    def error_msg(self):
        if self._preflight_error:
            return self._preflight_error
        return self._monitor.error_msg

    def enable(self):
        """Resolve mitmdump + start the capture subprocess.

        Returns True if enabled (bin resolved or already running/starting);
        False if mitmdump can't be resolved or the subprocess failed to
        spawn (#40) — the caller surfaces the UI on False. On False the
        enabled flag stays False.
        """
        if self._monitor.status != "stopped":
            self._enabled = True
            return True
        cfg = self._config_fn()
        try:
            res = resolve_capture_resources(cfg)
        except CaptureResourcesError as exc:
            # issue #2：preflight 失败不进 enabled，可行动错误直达菜单文案
            self._preflight_error = exc.msg
            logger.warning("capture mode preflight failed: %s", exc.msg)
            return False
        self._enabled = True
        self._preflight_error = None
        self._trust_cache = None  # enabled titles never read trust state
        started = self._monitor.start(
            mitmdump_bin=res.mitmdump_bin,
            addon_path=res.addon_path,
            capture_port=cfg.get("capture_port", DEFAULT_CAPTURE_PORT),
            upstream=f"http://{netloc.format_listen('127.0.0.1', int(cfg['http_listen_port']))}",
            capture_dir=res.capture_dir,
            retention_days=cfg.get("retention_days", 7),
        )
        if not started:
            self._enabled = False
            return False
        logger.info("capture mode: starting mitmdump")
        return True

    def disable(self):
        """Stop the capture subprocess if running."""
        self._preflight_error = None  # 用户已行动：过期预检错误不再残留
        if self._monitor.status != "stopped":
            self._monitor.stop()
            logger.info("capture mode: stopped")
        self._enabled = False

    def _ca_trusted(self):
        """TTL-cached trust check for menu labels (display-only)."""
        now = time.monotonic()
        if self._trust_cache and now < self._trust_cache[0]:
            return self._trust_cache[1]
        trusted = ca_trust.is_trusted()
        self._trust_cache = (now + TRUST_CACHE_TTL, trusted)
        return trusted

    def menu_title(self):
        """Derive the capture menu title from state + a live CA-trust check."""
        s = self._monitor.status
        if s == "error":
            return "抓包模式：异常"
        if self._enabled and s == "running":
            return "抓包模式：开"
        if self._enabled:
            return "抓包模式：启动中…"
        if not self._ca_trusted():
            return "抓包模式：关（需信任证书）"
        return "抓包模式：关"

    def error_hint(self):
        """Detail line for a crashed mitmdump; None when not applicable."""
        if self._monitor.status == "error" and self._monitor.error_msg:
            return f"  {_truncate(self._monitor.error_msg, 80)}"
        return None
