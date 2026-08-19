"""Launch Chromium-based desktop apps through Magic-Proxy via --proxy-server.

Covers apps whose engine is Chromium — ChatGPT/Codex (a custom Chromium shell)
and Claude (Electron). All honor Chromium's --proxy-server switch, so a per-app
proxy needs no system proxy and is fail-closed (traffic dies if the proxy is
down). Chromium is single-instance, so the flag only lands on a fresh launch —
a second `open --args` on a running instance is swallowed by the single-instance
lock. Callers must quit any running instance first.

To support a new app, add an entry to KNOWN_APPS. Process name, quit target and
main-binary path are all derived from the resolved .app bundle name.

Side-effecting helpers shell out and never raise; launch()/quit_app() return
(ok, err).
"""
import logging
import os
import re
import subprocess
import time

logger = logging.getLogger("magic-proxy.chromium_proxy")

_TIMEOUT = 5

# Registry of known Chromium-based apps. Add an entry to support a new one.
KNOWN_APPS = [
    {"key": "chatgpt", "name": "ChatGPT", "bundle_id": "com.openai.codex",
     "default_path": "/Applications/ChatGPT.app"},
    {"key": "claude", "name": "Claude", "bundle_id": "com.anthropic.claudefordesktop",
     "default_path": "/Applications/Claude.app"},
    # Electron app. --proxy-server covers TCP (login/gateway/API); WebRTC voice
    # (UDP) is not proxied and may fail or bypass — expected, documented.
    {"key": "discord", "name": "Discord", "bundle_id": "com.hnc.Discord",
     "default_path": "/Applications/Discord.app"},
]


def app_path(entry):
    """Resolve an app entry's bundle path, or None if not installed.

    Tries the default location, then Spotlight by bundle id.
    """
    default = entry.get("default_path")
    if default and os.path.isdir(default):
        return default
    bundle_id = entry.get("bundle_id")
    if not bundle_id:
        return None
    try:
        cp = subprocess.run(
            ["mdfind", f"kMDItemCFBundleIdentifier == '{bundle_id}'"],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    for line in cp.stdout.splitlines():
        line = line.strip()
        if line.endswith(".app") and os.path.isdir(line):
            return line
    return None


def installed_apps():
    """Known apps that are actually installed, each with a resolved 'path'."""
    out = []
    for entry in KNOWN_APPS:
        path = app_path(entry)
        if path:
            out.append({**entry, "path": path})
    return out


def _app_stem(path):
    """'/Applications/Claude.app' -> 'Claude' (the launchable / process name)."""
    base = os.path.basename(path.rstrip("/"))
    return base[:-4] if base.endswith(".app") else base


def _apple_script_quote(stem):
    """Escape a stem for interpolation into an AppleScript string literal.

    #40: an app name containing a quote must not break out of the tell
    string (AppleScript-level escaping, not shell — the -e argument is
    passed directly to osascript).
    """
    return stem.replace("\\", "\\\\").replace('"', '\\"')


def launch_args(http_listen):
    """Chromium switches that route the app through http_listen. Pure/testable.

    "host:port" applies the proxy to every scheme; Chromium leaves loopback
    direct by default, which is fine (these apps' API calls are remote).
    """
    return [f"--proxy-server={http_listen}"]


def is_running(path):
    """True if the app's main process is running (helpers excluded)."""
    base = os.path.basename(path.rstrip("/"))
    # #40: pgrep -f treats the pattern as an (extended) regex — escape it
    # so a literal path with metacharacters can't mis-match other apps.
    match = re.escape(f"{base}/Contents/MacOS/{_app_stem(path)}")
    try:
        cp = subprocess.run(
            ["pgrep", "-f", match],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return cp.returncode == 0


def quit_app(path):
    """Ask the app to quit (graceful). Returns (ok, err)."""
    try:
        cp = subprocess.run(
            ["osascript", "-e",
             f'tell application "{_apple_script_quote(_app_stem(path))}" to quit'],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)
    if cp.returncode != 0:
        return False, (cp.stderr or "").strip()
    return True, ""


def wait_until_stopped(path, timeout=5.0, interval=0.25):
    """Poll until the app is no longer running, up to timeout. Returns bool."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_running(path):
            return True
        time.sleep(interval)
    return not is_running(path)


class RelaunchWaiter:
    """Non-blocking quit→relaunch coordinator (#40).

    quit_app() returns immediately; waiting for the process to actually
    exit used to block the menu callback up to 5 s.  Instead, poll step()
    once per app tick — it returns an action only when the state machine
    completes.
    """

    def __init__(self, path, name, http_listen, timeout=5.0):
        self.path = path
        self.name = name
        self.http_listen = http_listen
        self.deadline = time.monotonic() + timeout

    def step(self, is_running_fn=None):
        """Return ("launch", http_listen) when the app has exited,
        ("timeout", None) past the deadline, else None."""
        if is_running_fn is None:
            is_running_fn = is_running
        if not is_running_fn(self.path):
            return ("launch", self.http_listen)
        if time.monotonic() >= self.deadline:
            return ("timeout", None)
        return None


def launch(path, http_listen):
    """Launch the app fresh with the proxy flag. Returns (ok, err).

    Assumes no instance is running (single-instance lock would otherwise drop
    the args). `open -a … --args …` passes the switches to the new process.
    """
    try:
        cp = subprocess.run(
            ["open", "-a", path, "--args", *launch_args(http_listen)],
            capture_output=True, text=True, timeout=_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        return False, str(e)
    if cp.returncode != 0:
        return False, (cp.stderr or "").strip()
    return True, ""
