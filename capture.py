"""mitmdump subprocess manager (ADR-022 Task 2).

Inherits the shared SubprocessMonitor lifecycle (start/stop/check/reap).
Config flows one-way via env vars to the mitmproxy addon; the addon only
reads those + writes to disk. proxy.py itself is untouched — mitmdump
talks to it purely as an upstream HTTP CONNECT proxy.
"""
import logging
import os
import re
import socket
from datetime import datetime
from capture_store import DEFAULT_CAPTURE_DIR, DEFAULT_CAPTURE_PORT, prepare as prepare_capture_dir
from subprocess_monitor import SubprocessMonitor

logger = logging.getLogger("magic-proxy.capture")

PORT_PROBE_TIMEOUT = 0.1  # seconds for starting-state port probe

# Matches ai_capture_addon.py's write_jsonl() naming: <capture_dir>/<local
# date %Y-%m-%d>.jsonl. Anything else in capture_dir is left untouched by
# cleanup_expired_captures.
_DATE_FILE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl(?:\.1)?$")

# One-way env-var contract injected into the mitmdump child. The addon reads
# these; capture.py never reads anything back from the addon.
ENV_CAPTURE_DIR = "MAGIC_PROXY_CAPTURE_DIR"
ENV_CAPTURE_RAW_SSE = "MAGIC_PROXY_CAPTURE_RAW_SSE"
ENV_PRESERVE_STREAMING = "MAGIC_PROXY_PRESERVE_STREAMING"

DEFAULT_UPSTREAM = "http://127.0.0.1:8888"
LOOPBACK_HOST = "127.0.0.1"


def cleanup_expired_captures(capture_dir: str, retention_days: int) -> int:
    """Delete daily JSONL capture files older than the retention window
    (ADR-022 Task 5 AC-1).

    Semantics (locked): retention_days > 0 -> delete files whose age in
    days is >= retention_days (keeps exactly retention_days days of data,
    today inclusive -- today's file is never touched for any
    retention_days >= 1); retention_days <= 0 -> no-op, unbounded
    retention. Never raises -- every failure mode (missing dir, unlistable
    dir, a single file's delete failing) is caught and logged so a
    retention hiccup can't block capture mode from starting. Returns the
    number of files actually deleted.
    """
    if retention_days <= 0:
        return 0
    if not os.path.isdir(capture_dir):
        return 0

    try:
        entries = os.listdir(capture_dir)
    except OSError:
        logger.warning("capture retention: could not list %s", capture_dir)
        return 0

    today = datetime.now().date()
    deleted = 0
    for name in entries:
        m = _DATE_FILE_RE.match(name)
        if not m:
            continue
        try:
            file_date = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        age_days = (today - file_date).days
        if age_days < retention_days:
            continue
        path = os.path.join(capture_dir, name)
        try:
            os.remove(path)
            deleted += 1
            logger.info("capture retention: deleted expired %s (age=%dd)", name, age_days)
        except OSError:
            logger.warning("capture retention: failed to delete %s", path)
    return deleted


class CaptureMonitor(SubprocessMonitor):
    """Manages the mitmdump capture-mode subprocess."""

    _PROCESS_NAME = "mitmdump"

    def start(
        self,
        mitmdump_bin: str,
        addon_path: str,
        capture_port: int = DEFAULT_CAPTURE_PORT,
        upstream: str = DEFAULT_UPSTREAM,
        capture_dir: str = None,
        raw_sse: bool = False,
        preserve_streaming: bool = False,
        retention_days: int = 0,
    ):
        """Start the mitmdump capture subprocess."""
        self.stop()

        try:
            resolved_capture_dir = prepare_capture_dir(capture_dir or DEFAULT_CAPTURE_DIR)
        except OSError as exc:
            self._status = "error"
            self._error_msg = str(exc)
            return False
        try:
            cleanup_expired_captures(resolved_capture_dir, retention_days)
        except Exception:
            logger.exception("capture retention: cleanup pass failed, continuing to start")

        cmd = [
            mitmdump_bin,
            "--mode", f"upstream:{upstream}",
            "--listen-host", LOOPBACK_HOST,
            "--listen-port", str(capture_port),
            "-s", addon_path,
        ]

        env = os.environ.copy()
        env[ENV_CAPTURE_DIR] = resolved_capture_dir
        env[ENV_CAPTURE_RAW_SSE] = "1" if raw_sse else "0"
        env[ENV_PRESERVE_STREAMING] = "1" if preserve_streaming else "0"

        return self._start_process(cmd, env=env)

    def _probe_ready(self, port):
        """Raw TCP connect probe — mitmdump accepts connections when ready."""
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.settimeout(PORT_PROBE_TIMEOUT)
            s.connect(("127.0.0.1", port))
            return True
        except (ConnectionRefusedError, OSError):
            return False
        finally:
            s.close()
