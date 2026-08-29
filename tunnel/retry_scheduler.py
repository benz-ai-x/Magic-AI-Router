"""SSH retry scheduler with exponential backoff.

Tracks retry attempts and schedules reconnects with increasing delays.
Uses a generation counter for invalidation — starting a new SSH attempt
or cancelling invalidates all pending timers from previous generations.
"""
import logging
import threading

logger = logging.getLogger("magic-proxy.retry")

DEFAULT_DELAYS = (5, 15, 45)  # seconds
DEFAULT_MAX_DELAY = 60        # seconds — 退避封顶（#85：耗尽表后按此节奏无限重试）


class RetryScheduler:
    """Owns retry state: counter, timer, generation, lock.

    #85：永不放弃——表内按表退避，表外按 max_delay 封顶无限重试；
    停止重试只能经 cancel()（用户暂停/停止/重启）。
    """

    def __init__(self, delays=DEFAULT_DELAYS, max_delay=DEFAULT_MAX_DELAY):
        self._delays = delays
        self._max_delay = max_delay
        self._retries = 0
        self._timer = None
        self._due = False
        self._generation = 0
        self._lock = threading.Lock()

    @property
    def retries(self):
        return self._retries

    def handle_error(self):
        """Schedule a retry (always — backoff capped at max_delay)."""
        with self._lock:
            if self._timer and self._timer.is_alive():
                return
            delay = (self._delays[self._retries]
                     if self._retries < len(self._delays)
                     else self._max_delay)
            self._retries += 1
            generation = self._generation
            self._timer = threading.Timer(
                delay, self._mark_due, args=(generation,))
            self._timer.daemon = True
            self._timer.start()
        logger.info("SSH retry #%d scheduled in %ds", self._retries, delay)

    def _mark_due(self, generation):
        with self._lock:
            if generation != self._generation:
                return
            self._due = True
            self._timer = None

    def consume_due(self):
        """Return True if a retry is due (called from tick). Resets the flag."""
        with self._lock:
            due = self._due
            self._due = False
            return due

    def cancel(self):
        """Cancel all pending retries, reset counter, invalidate timers."""
        with self._lock:
            self._generation += 1
            self._due = False
            if self._timer:
                self._timer.cancel()
                self._timer = None
            self._retries = 0

    def reset(self):
        """Reset retry counter on successful connection (no timer cancellation)."""
        with self._lock:
            self._retries = 0
