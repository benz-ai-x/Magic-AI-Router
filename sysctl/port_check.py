"""Detect and clear port owners via lsof + POSIX kill.

Stateless: callers decide whether to kill. Both functions never raise.
"""
import logging
import os
import signal
import subprocess
import time
from typing import NamedTuple, Optional

logger = logging.getLogger("magic-proxy.port_check")

_LSOF_TIMEOUT = 5
_PS_TIMEOUT = 5


class PortOwner(NamedTuple):
    pid: int
    name: str   # short process name from lsof (e.g. "ssh")
    cmd: str    # full command line from ps; "" if ps failed


def _parse_lsof_pcn(stdout: str) -> Optional[tuple[int, str]]:
    """Parse `lsof -F pcn` output, return (pid, name) of first listener; None if empty."""
    pid = None
    name = ""
    for line in stdout.splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            if pid is not None:
                # Already have a complete record; stop at the first one.
                break
            try:
                pid = int(val)
            except ValueError:
                pid = None
        elif tag == "c" and pid is not None:
            name = val
    if pid is None:
        return None
    return pid, name


def _cmdline_of(pid: int) -> str:
    """Return full command line of PID via ps, or empty string on failure."""
    try:
        cp = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True, text=True, timeout=_PS_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    if cp.returncode != 0:
        return ""
    return cp.stdout.strip()


def who_owns(port: int) -> Optional[PortOwner]:
    """Return owner of 127.0.0.1:<port> LISTEN, or None if free / lookup failed."""
    try:
        cp = subprocess.run(
            ["lsof", "-nP", f"-iTCP@127.0.0.1:{port}", "-sTCP:LISTEN", "-F", "pcn"],
            capture_output=True, text=True, timeout=_LSOF_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        logger.warning("lsof failed for port %d: %s", port, e)
        return None
    if cp.returncode != 0:
        # lsof exits 1 when no matching listener — port is free.
        return None
    parsed = _parse_lsof_pcn(cp.stdout)
    if parsed is None:
        logger.warning(
            "lsof exited 0 for port %d but output unparseable: %r",
            port, cp.stdout[:200],
        )
        return None
    pid, name = parsed
    return PortOwner(pid=pid, name=name, cmd=_cmdline_of(pid))


def _alive(pid: int) -> bool:
    """POSIX trick: signal 0 raises ProcessLookupError when PID is dead."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we can't signal it; treat as alive.
        return True
    return True


def _wait_dead(pid: int, timeout: float) -> bool:
    """Poll until PID is gone or timeout elapses. Returns True if dead."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(0.05)
        if not _alive(pid):
            return True
    return not _alive(pid)


def kill(pid: int, timeout: float = 1.0) -> tuple[bool, str]:
    """SIGTERM → wait timeout → SIGKILL (with extra 0.5s wait). Returns (ok, err)."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, ""
    except PermissionError:
        return False, f"permission denied (PID {pid})"
    if _wait_dead(pid, timeout):
        return True, ""
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True, ""
    except PermissionError:
        return False, f"permission denied on SIGKILL (PID {pid})"
    if _wait_dead(pid, 0.5):
        return True, ""
    return False, "process still alive after SIGKILL"
