"""Thread-safe traffic statistics collector with rate calculation."""
import time
import threading


class Stats:
    def __init__(self):
        self._lock = threading.Lock()
        self._total_down = 0
        self._total_up = 0
        self._last_down = 0
        self._last_up = 0
        self._last_ts = time.monotonic()
        self._rate_down = 0
        self._rate_up = 0
        self._active_connections = 0

    def record_down(self, n: int):
        with self._lock:
            self._total_down += n

    def record_up(self, n: int):
        with self._lock:
            self._total_up += n

    def inc_connections(self):
        with self._lock:
            self._active_connections += 1

    def dec_connections(self):
        with self._lock:
            if self._active_connections > 0:
                self._active_connections -= 1

    def tick(self):
        """Call every second; recalculates rates."""
        now = time.monotonic()
        with self._lock:
            elapsed = now - self._last_ts
            if elapsed > 0:
                self._rate_down = int((self._total_down - self._last_down) / elapsed)
                self._rate_up = int((self._total_up - self._last_up) / elapsed)
            self._last_down = self._total_down
            self._last_up = self._total_up
            self._last_ts = now

    def snapshot(self):
        """Return a consistent snapshot dict for the UI thread."""
        with self._lock:
            return {
                "total_down": self._total_down,
                "total_up": self._total_up,
                "rate_down": self._rate_down,
                "rate_up": self._rate_up,
                "active_connections": self._active_connections,
            }
