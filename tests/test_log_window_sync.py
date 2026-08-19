"""Unit tests for LogWindow.sync_ render-cursor logic (requirement 4).

The NSWindow/NSTextView are GUI (manual SIT), but ``sync_`` is pure append
arithmetic over a LogBuffer — so we drive it headless with a FakeTextView that
records exactly what the real NSTextView would receive.

Covers regression C-1: once the ring buffer saturates, ``len(snapshot)`` is
constant forever, so a length-based cursor froze the window. The fix diffs on
the buffer's monotonic ``total`` instead; these tests pin that behavior.
"""
import logging
import unittest

from log_window import LogBuffer, LogWindow


class _FakeScroller:
    def floatValue(self):
        return 1.0  # pretend the view is pinned to the bottom → auto-scroll path


class _FakeScrollView:
    def verticalScroller(self):
        return _FakeScroller()


class _FakeTextView:
    """Stand-in for NSTextView recording append operations.

    ``sync_`` only ever inserts at the end (range length 0), so a plain append
    faithfully mirrors real behavior for these tests.
    """

    def __init__(self):
        self._s = ""

    def string(self):
        return self._s

    def setString_(self, s):
        self._s = s

    def replaceCharactersInRange_withString_(self, rng, s):
        self._s += s  # sync_ always inserts at end (length-0 range)

    def enclosingScrollView(self):
        return _FakeScrollView()

    def scrollRangeToVisible_(self, rng):
        pass


class TestLogWindowSync(unittest.TestCase):
    def setUp(self):
        self.logger = logging.getLogger("test.logwindow.sync")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

    def tearDown(self):
        for h in list(self.logger.handlers):
            self.logger.removeHandler(h)

    def _new_window(self, buf):
        self.logger.addHandler(buf)
        w = LogWindow.alloc().init()
        w._buffer = buf
        w._text = _FakeTextView()
        w._rendered = 0
        return w

    def test_first_sync_renders_all_below_capacity(self):
        buf = LogBuffer(capacity=100)
        w = self._new_window(buf)
        for i in range(5):
            self.logger.info("line %d", i)
        w.sync_(None)
        text = str(w._text.string())
        self.assertIn("line 0", text)
        self.assertIn("line 4", text)
        self.assertEqual(w._rendered, 5)

    def test_sync_appends_new_lines_after_ring_saturates(self):
        """C-1 regression: after >capacity emits, sync_ must keep appending,
        not freeze. Old bug used len(snapshot) as cursor → once the ring filled
        it equalled _rendered forever → new lines never rendered."""
        buf = LogBuffer(capacity=5)
        w = self._new_window(buf)

        # Fill past capacity: 12 emits into a 5-line ring.
        for i in range(12):
            self.logger.info("line %d", i)
        w.sync_(None)
        self.assertIn("line 11", str(w._text.string()))
        self.assertEqual(w._rendered, 12)

        # Emit 3 MORE lines while the ring is already saturated.
        for i in range(12, 15):
            self.logger.info("line %d", i)
        before = str(w._text.string())
        w.sync_(None)
        after = str(w._text.string())

        self.assertGreater(len(after), len(before),
                           "sync_ froze after ring saturation (C-1)")
        self.assertIn("line 13", after)
        self.assertIn("line 14", after)
        self.assertEqual(w._rendered, 15)

    def test_sync_resets_view_when_reader_lags_past_capacity(self):
        """If more than capacity lines arrive between two syncs, the owed lines
        were dropped — sync_ clears the view and shows the surviving tail."""
        buf = LogBuffer(capacity=5)
        w = self._new_window(buf)

        for i in range(3):
            self.logger.info("early %d", i)
        w.sync_(None)
        self.assertIn("early 2", str(w._text.string()))

        # Lag: emit 20 more (total 23, ring holds last 5 → all "early" dropped).
        for i in range(3, 23):
            self.logger.info("late %d", i)
        w.sync_(None)
        text = str(w._text.string())
        self.assertNotIn("early", text)         # pre-lag lines gone from ring
        self.assertIn("late 22", text)          # newest survives
        self.assertEqual(w._rendered, 23)

    def test_sync_noop_when_nothing_new(self):
        buf = LogBuffer(capacity=100)
        w = self._new_window(buf)
        for i in range(3):
            self.logger.info("x %d", i)
        w.sync_(None)
        once = str(w._text.string())
        # Re-sync with no new emits → text unchanged, cursor unchanged.
        w.sync_(None)
        self.assertEqual(str(w._text.string()), once)
        self.assertEqual(w._rendered, 3)


if __name__ == "__main__":
    unittest.main()
