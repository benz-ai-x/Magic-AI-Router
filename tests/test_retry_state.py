"""Tests for RetryScheduler (extracted from MagicProxyApp)."""
from unittest.mock import MagicMock, patch

from tunnel.retry_scheduler import RetryScheduler


def test_repeated_error_schedules_only_one_timer():
    scheduler = RetryScheduler()
    timer = MagicMock()
    timer.is_alive.return_value = True
    with patch("threading.Timer", return_value=timer) as factory:
        scheduler.handle_error()
        scheduler.handle_error()
    factory.assert_called_once()
    assert scheduler.retries == 1


def test_cancel_invalidates_old_callback():
    scheduler = RetryScheduler()
    scheduler.handle_error()  # schedule a timer
    scheduler.cancel()        # invalidate it
    assert not scheduler.consume_due()  # the callback (if it fires) is a no-op


def test_reset_clears_retry_count():
    scheduler = RetryScheduler()
    scheduler.handle_error()
    assert scheduler.retries == 1
    scheduler.reset()
    assert scheduler.retries == 0


def test_continues_at_capped_delay_beyond_table():
    """#85：表耗尽后不放弃，按 max_delay 封顶继续调度。"""
    scheduler = RetryScheduler(delays=(1, 2), max_delay=60)
    with patch("threading.Timer", return_value=MagicMock()) as factory:
        scheduler._timer = None
        scheduler.handle_error()  # retry 1（表内 1s）
        scheduler._timer = None  # simulate timer completed
        scheduler.handle_error()  # retry 2（表内 2s）
        scheduler._timer = None
        scheduler.handle_error()  # retry 3（表外 → 封顶 60s）
    assert scheduler.retries == 3
    assert factory.call_args_list[2].args[0] == 60
