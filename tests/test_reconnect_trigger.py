"""Tests for tunnel/reconnect_trigger.py — 去抖触发器 + 唤醒事件源（#86）。"""
import sys
import unittest
from unittest.mock import patch

from tunnel.reconnect_trigger import ReconnectTrigger, WakeEventSource


def _make_trigger(min_interval=2.0):
    now = [100.0]
    fired = []
    trigger = ReconnectTrigger(
        on_reconnect=lambda: fired.append(now[0]),
        min_interval=min_interval,
        clock=lambda: now[0])
    return trigger, now, fired


class TestReconnectTrigger(unittest.TestCase):
    def test_first_event_fires_immediately(self):
        trigger, _, fired = _make_trigger()
        trigger.notify()
        self.assertEqual(fired, [100.0])

    def test_storm_within_window_fires_once(self):
        trigger, now, fired = _make_trigger(min_interval=2.0)
        trigger.notify()
        now[0] += 0.5
        trigger.notify()
        now[0] += 0.5
        trigger.notify()  # 仍在一个窗口内（共 1.0s < 2.0s）
        self.assertEqual(len(fired), 1, "事件风暴应合并为一次触发")

    def test_event_after_window_fires_again(self):
        trigger, now, fired = _make_trigger(min_interval=2.0)
        trigger.notify()
        now[0] += 2.5
        trigger.notify()
        self.assertEqual(fired, [100.0, 102.5])


class TestWakeEventSource(unittest.TestCase):
    def test_start_returns_true_with_pyobjc(self):
        source = WakeEventSource(lambda: None)
        self.assertTrue(source.start())

    def test_start_is_idempotent(self):
        source = WakeEventSource(lambda: None)
        source.start()
        self.assertTrue(source.start(), "二次 start 不应重复注册")

    def test_start_returns_false_without_pyobjc(self):
        source = WakeEventSource(lambda: None)
        blocked = {"AppKit": None, "Foundation": None, "objc": None}
        with patch.dict(sys.modules, blocked):
            self.assertFalse(source.start(), "PyObjC 缺失应静默降级返回 False")


if __name__ == "__main__":
    unittest.main()
