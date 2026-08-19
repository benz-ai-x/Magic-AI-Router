"""Caffeinate-based sleep prevention (Layer 1, no privileges).

While the SSH tunnel is connected, holding a caffeinate assertion keeps the
Mac from idle/system sleeping so the proxy and tunnel stay alive. We bind
caffeinate to this process with ``-w <pid>`` so it exits automatically when
the app dies (clean quit *or* crash) — no pidfile/flag crash-recovery
bookkeeping is needed, unlike pmset-based blockers.

Only ``-i`` (idle) and ``-s`` (system) sleep are blocked; the display is
allowed to sleep to save power (a background proxy doesn't need the screen
on). This is deliberately narrower than KeepRunning's ``-dimsu``.
"""
import logging
import os
import subprocess

logger = logging.getLogger("magic-proxy.sleep_blocker")

CAFFEINATE_BIN = "/usr/bin/caffeinate"


class CaffeinateBlocker:
    """Idempotent lifecycle around a single caffeinate child process.

    ``acquire``/``release`` are both safe to call repeatedly; the assertion is
    held exactly once. ``is_running`` reflects whether the child is alive, so
    callers can latch state off it (a failed ``acquire`` reports not-running
    and is retried on the next call).
    """

    def __init__(self, bin_path=CAFFEINATE_BIN):
        self._bin = bin_path
        self._proc = None
        self._failed = False  # latches after first OSError so we log once

    @property
    def is_running(self):
        return self._proc is not None and self._proc.poll() is None

    def acquire(self):
        if self.is_running:
            return
        # -i: prevent idle sleep; -s: prevent system sleep; -w <pid>: exit
        # when our process exits (crash safety without crash-recovery state).
        args = [self._bin, "-i", "-s", "-w", str(os.getpid())]
        try:
            self._proc = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._failed = False
            logger.info("caffeinate started (pid %s)", self._proc.pid)
        except OSError:
            # Missing binary, exec denied, etc. — non-fatal: the machine may
            # sleep, but the app itself keeps running. Log once until a
            # successful release resets the latch for a clean retry later.
            if not self._failed:
                logger.exception("failed to start caffeinate; sleep prevention inactive")
            self._failed = True
            self._proc = None

    def release(self):
        proc = self._proc
        self._proc = None
        self._failed = False  # allow a clean retry on the next acquire
        if proc is None or proc.poll() is not None:
            return  # nothing live to stop
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            logger.warning("caffeinate did not exit on SIGTERM; SIGKILL'd")
