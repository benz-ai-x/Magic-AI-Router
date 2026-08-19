"""Tests for retry_scheduler.RetryScheduler — backoff, due-flag, invalidation."""
import time
import unittest

from retry_scheduler import RetryScheduler


class TestInitialState(unittest.TestCase):
    def test_retries_start_at_zero(self):
        self.assertEqual(RetryScheduler().retries, 0)

    def test_not_due_initially(self):
        self.assertFalse(RetryScheduler().consume_due())


class TestHandleError(unittest.TestCase):
    def test_schedules_retry_and_increments(self):
        rs = RetryScheduler(delays=(0.01,))
        rs.handle_error()
        self.assertEqual(rs.retries, 1)

    def test_retry_becomes_due_after_delay(self):
        rs = RetryScheduler(delays=(0.01,))
        rs.handle_error()
        time.sleep(0.1)
        self.assertTrue(rs.consume_due())
        # due flag resets after consumption
        self.assertFalse(rs.consume_due())

    def test_gives_up_after_max_attempts(self):
        rs = RetryScheduler(delays=(0.01, 0.01))
        rs.handle_error()
        time.sleep(0.05)
        rs.consume_due()
        rs.handle_error()
        time.sleep(0.05)
        rs.consume_due()
        # Third call exceeds len(delays)=2 -> no further retry scheduled
        rs.handle_error()
        self.assertEqual(rs.retries, 2)

    def test_no_duplicate_timer_while_pending(self):
        rs = RetryScheduler(delays=(5,))
        rs.handle_error()
        first_timer = rs._timer
        rs.handle_error()  # timer still alive -> early return
        self.assertIs(rs._timer, first_timer)
        self.assertEqual(rs.retries, 1)
        rs.cancel()


class TestCancel(unittest.TestCase):
    def test_cancel_resets_state(self):
        rs = RetryScheduler(delays=(0.01,))
        rs.handle_error()
        time.sleep(0.1)
        rs.cancel()
        self.assertEqual(rs.retries, 0)
        self.assertFalse(rs.consume_due())

    def test_cancel_invalidates_pending_timer(self):
        rs = RetryScheduler(delays=(0.05,))
        rs.handle_error()
        rs.cancel()
        time.sleep(0.1)
        # Timer fired but generation mismatch -> not due
        self.assertFalse(rs.consume_due())


class TestReset(unittest.TestCase):
    def test_reset_clears_counter(self):
        rs = RetryScheduler(delays=(0.01,))
        rs.handle_error()
        rs.reset()
        self.assertEqual(rs.retries, 0)


if __name__ == "__main__":
    unittest.main()
