"""Launch-at-login via a user LaunchAgent (classic login-item mechanism).

Writes/removes ``~/Library/LaunchAgents/<label>.plist`` with ``RunAtLoad``
true; launchd picks it up at next login and runs the packaged app. This works
on every macOS version and needs no framework binding.

Note on the SMAppService detour: the modern ``SMAppService.mainApp()`` API
(macOS 13+) was the first choice, but ``mainApp`` is absent from the
pyobjc-framework-ServiceManagement 12.2.1 metadata (register/unregister/status
are present; mainApp is not) — so the binding can't reach it. Rather than bump
pyobjc or objc_msgSend around the gap, this LaunchAgent path is the robust,
version-independent fallback that doesn't depend on the binding at all.

Only inside the packaged .app is ``sys.executable`` the bundle binary; in dev
mode (``python3 app.py``) it's the interpreter, so we reject up front instead
of registering the wrong executable.
"""
import logging
import os
import plistlib
import subprocess
import sys

from mpconf import config_store
logger = logging.getLogger("magic-proxy.login_item")

# PyInstaller sets sys._MEIPASS inside a frozen .app; its absence means we're
# running from source, where sys.executable is the interpreter, not the app.
FROZEN = hasattr(sys, "_MEIPASS")
LABEL = "com.benzai.magic-ai-router.login"


def _plist_path():
    return os.path.join(os.path.expanduser("~/Library/LaunchAgents"),
                        LABEL + ".plist")


def set_launch_at_login(enabled):
    """Install or remove the login LaunchAgent.

    Returns ``(ok, err)``. ``err`` is ``""`` on success, otherwise a short
    user-facing Chinese string. We only write/remove the plist — launchd
    loads ``~/Library/LaunchAgents`` at next login, so there's no immediate
    ``launchctl load`` (that would spawn a duplicate of the already-running
    app). Disable does a best-effort ``unload`` before removing the file.
    """
    if not FROZEN:
        return False, "登录启动仅在打包后的 Magic AI Router.app 内可用，开发模式不支持。"
    exe = sys.executable
    if not exe or not os.path.exists(exe):
        return False, f"无法定位应用可执行文件：{exe!r}"
    plist_path = _plist_path()
    try:
        if enabled:
            # #40: staged atomic write (mkstemp + os.replace) — a crash
            # mid-write must never leave a corrupt plist. XML format so the
            # payload flows through atomic_write's text pipeline.
            xml = plistlib.dumps({
                "Label": LABEL,
                "ProgramArguments": [exe],
                "RunAtLoad": True,
                "KeepAlive": False,
                "LaunchOnlyOnce": True,
            }, fmt=plistlib.FMT_XML).decode("utf-8")
            if not config_store.atomic_write(plist_path, xml):
                return False, "写入登录项失败"
        else:
            # Best-effort unload (harmless if not loaded), then remove the
            # plist so launchd won't relaunch the app at the next login.
            subprocess.run(["launchctl", "unload", plist_path],
                           capture_output=True)
            if os.path.exists(plist_path):
                os.unlink(plist_path)
    except OSError as exc:
        logger.exception("login LaunchAgent %s failed",
                         "install" if enabled else "remove")
        return False, f"写入登录项失败：{exc}"
    return True, ""
