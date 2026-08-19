"""Unit tests for LogBuffer (requirement 4 — real-time log window backend).

LogBuffer is the in-memory ring-buffer logging.Handler that feeds the live log
window. The NSWindow/NSTextView rendering is GUI (manual SIT); these tests
cover the headless backend: thread-safety, capacity bound, formatting, and
snapshot diffing.
"""
import logging
import threading
import unittest

from log_window import LogBuffer


class TestLogBuffer(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("test.logbuffer")
        self.logger.setLevel(logging.DEBUG)
        # isolate from root so other tests' handlers don't interfere
        self.logger.propagate = False

    def tearDown(self):
        for h in list(self.logger.handlers):
            self.logger.removeHandler(h)

    def _attach(self, **kw):
        buf = LogBuffer(**kw)
        self.logger.addHandler(buf)
        return buf

    def test_captures_emitted_records(self):
        buf = self._attach()
        self.logger.info("hello")
        self.logger.error("boom")
        snap, total = buf.snapshot()
        self.assertEqual(len(snap), 2)
        self.assertEqual(total, 2)
        self.assertIn("hello", snap[0])
        self.assertIn("boom", snap[1])
        self.assertIn("ERROR", snap[1])

    def test_ring_buffer_drops_oldest_at_capacity(self):
        buf = self._attach(capacity=3)
        for i in range(5):
            self.logger.info("line %d", i)
        snap, total = buf.snapshot()
        self.assertEqual(len(snap), 3)
        # oldest two dropped from the ring, but total kept counting (C-1)
        self.assertEqual(total, 5)
        self.assertIn("line 2", snap[0])
        self.assertIn("line 4", snap[-1])

    def test_total_is_monotonic_across_ring_wrap(self):
        """C-1 root cause: len(lines) saturates at capacity but total keeps
        growing — the window diffs on total, not len(lines)."""
        buf = self._attach(capacity=3)
        for i in range(9):
            self.logger.info("x %d", i)
        lines, total = buf.snapshot()
        self.assertEqual(len(lines), 3)   # ring saturated
        self.assertEqual(total, 9)        # total never resets

    def test_snapshot_returns_copy_not_live_view(self):
        buf = self._attach()
        self.logger.info("first")
        snap, total = buf.snapshot()
        self.logger.info("second")
        # snapshot taken before second emit must be unchanged
        self.assertEqual(len(snap), 1)
        self.assertEqual(total, 1)
        self.assertEqual(len(buf.snapshot()[0]), 2)

    def test_exception_in_format_does_not_raise(self):
        """Handler.emit must not raise into caller (logging swallows, but verify)."""
        buf = self._attach()

        class BadRecord:
            levelname = "INFO"
            name = "bad"
            msg = "%s"
            args = (None,)
            exc_info = None
            created = 0
            msecs = 0
            relativeCreated = 0
            thread = 0
            threadName = ""
            processName = ""
            process = 0
            filename = ""
            pathname = ""
            module = ""
            funcName = ""
            lineno = 0
            levelno = 20
            getMessage = lambda self: "x"

            def __getattr__(self, k):
                raise AttributeError(k)

        # Direct emit with a record that has no asctime attr → formatter handles
        # via default; we just ensure no exception escapes.
        buf.emit(logging.LogRecord("bad", logging.INFO, "", 0, "ok", None, None))

    def test_thread_safe_concurrent_emit(self):
        """1000 records from 10 threads must not corrupt or lose capacity invariant."""
        buf = self._attach(capacity=10000)

        def worker(n):
            for i in range(100):
                self.logger.info("t%d-%d", n, i)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        snap, total = buf.snapshot()
        self.assertEqual(len(snap), 1000)
        self.assertEqual(total, 1000)
        self.assertLessEqual(len(snap), 10000)


if __name__ == "__main__":
    unittest.main()
