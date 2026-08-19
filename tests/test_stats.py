"""Tests for stats.py — traffic statistics + rate calculation."""
import unittest

from stats import Stats


class TestStatsTick(unittest.TestCase):
    def test_tick_calculates_rate(self):
        s = Stats()
        s.record_up(1000)
        s.record_down(2000)
        s.tick()
        snap = s.snapshot()
        self.assertGreater(snap["rate_up"], 0)
        self.assertGreater(snap["rate_down"], 0)

    def test_tick_updates_baseline(self):
        s = Stats()
        s.record_up(500)
        s.tick()
        snap1 = s.snapshot()
        s.record_up(500)
        s.tick()
        snap2 = s.snapshot()
        # After second tick, total should reflect all bytes
        self.assertEqual(snap2["total_up"], 1000)

    def test_tick_with_no_traffic(self):
        s = Stats()
        s.tick()
        snap = s.snapshot()
        self.assertEqual(snap["rate_up"], 0)
        self.assertEqual(snap["rate_down"], 0)


class TestStatsSnapshot(unittest.TestCase):
    def test_snapshot_returns_all_fields(self):
        s = Stats()
        s.inc_connections()
        s.record_up(42)
        s.record_down(99)
        snap = s.snapshot()
        self.assertEqual(snap["active_connections"], 1)
        self.assertEqual(snap["total_up"], 42)
        self.assertEqual(snap["total_down"], 99)
        self.assertIn("rate_up", snap)
        self.assertIn("rate_down", snap)


class TestStatsConnections(unittest.TestCase):
    def test_dec_connections_clamps_at_zero(self):
        s = Stats()
        s.dec_connections()  # should not go negative
        self.assertEqual(s.snapshot()["active_connections"], 0)

    def test_inc_dec_roundtrip(self):
        s = Stats()
        s.inc_connections()
        s.inc_connections()
        s.dec_connections()
        self.assertEqual(s.snapshot()["active_connections"], 1)
