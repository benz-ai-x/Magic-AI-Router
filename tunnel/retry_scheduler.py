"""SSH retry scheduler with exponential backoff.

Tracks retry attempts and schedules reconnects with increasing delays.
Uses a generation counter for invalidation — starting a new SSH attempt
or cancelling invalidates all pending timers from previous generations.
"""
import logging
import threading

logger = logging.getLogger("magic-proxy.retry")

DEFAULT_DELAYS = (5, 15, 45)  # seconds


class RetryScheduler:
    """Owns retry state: counter, timer, generation, lock."""

    def __init__(self, delays=DEFAULT_DELAYS):
        self._delays = delays
        self._retries = 0
        self._timer = None
        self._due = False
        self._generation = 0
        self._lock = threading.Lock()

    @property
    def retries(self):
        return self._retries

    def handle_error(self):
        """Schedule a retry if attempts remain. Called when SSH exits with error."""
        with self._lock:
            if self._timer and self._timer.is_alive():
                return
            if self._retries >= len(self._delays):
                logger.warning("SSH gave up after %d attempts", self._retries)
                return
            delay = self._delays[self._retries]
            self._retries += 1
            generation = self._generation
            self._timer = threading.Timer(
                delay, self._mark_due, args=(generation,))
            self._timer.daemon = True
            self._timer.start()
        logger.info("SSH retry %d/%d scheduled in %ds",
                    self._retries, len(self._delays), delay)

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
