"""Tests for async_runtime.py — AsyncRuntime lifecycle."""
import asyncio
import time
import unittest

from tunnel.async_runtime import AsyncRuntime


class TestInitialProperties(unittest.TestCase):
    def test_not_running_on_init(self):
        rt = AsyncRuntime("test")
        self.assertFalse(rt.running)

    def test_no_error_on_init(self):
        rt = AsyncRuntime("test")
        self.assertEqual(rt.error, "")


def _make_factory():
    """Return a coro_factory that sleeps forever + cancels via task.cancel()."""
    def factory(loop):
        async def main():
            await asyncio.sleep(100)

        task = loop.create_task(main())

        def stop_fn():
            loop.call_soon_threadsafe(task.cancel)

        return task, stop_fn

    return factory


class TestStartStop(unittest.TestCase):
    def test_start_and_stop_simple_coroutine(self):
        rt = AsyncRuntime("test", stop_timeout=2)
        rt.start(_make_factory())
        time.sleep(0.3)
        self.assertTrue(rt.running)
        self.assertTrue(rt.stop())
        self.assertFalse(rt.running)

    def test_stop_returns_true_when_not_running(self):
        rt = AsyncRuntime("test")
        self.assertTrue(rt.stop())

    def test_running_false_after_stop_signal_before_thread_death(self):
        rt = AsyncRuntime("test", stop_timeout=10)
        rt.start(_make_factory())
        time.sleep(0.3)
        self.assertTrue(rt.running)
        # Signal stop (sets stop_event) but don't join yet
        with rt._lock:
            rt._stop_event.set()
        # running should now be False even though thread may still be alive
        self.assertFalse(rt.running)
        rt.stop()

    def test_error_captured_on_exception(self):
        rt = AsyncRuntime("test", stop_timeout=2)

        def factory(loop):
            async def main():
                raise RuntimeError("boom")

            task = loop.create_task(main())

            def stop_fn():
                loop.call_soon_threadsafe(task.cancel)

            return task, stop_fn

        rt.start(factory)
        time.sleep(0.3)
        self.assertIn("boom", rt.error)

    def test_start_replaces_previous_generation(self):
        rt = AsyncRuntime("test", stop_timeout=2)
        rt.start(_make_factory())
        time.sleep(0.2)
        first_running = rt.running
        rt.start(_make_factory())
        time.sleep(0.2)
        self.assertTrue(first_running)
        self.assertTrue(rt.running)
        rt.stop()


class TestPendingTaskCleanup(unittest.TestCase):
    def test_leftover_background_task_is_cancelled_on_exit(self):
        # The main awaitable completes while a background task is still pending;
        # the finally block must cancel + gather it before closing the loop.
        rt = AsyncRuntime("test", stop_timeout=2)

        def factory(loop):
            async def background():
                await asyncio.sleep(100)
            loop.create_task(background())

            async def main():
                return  # completes immediately, leaving background pending
            task = loop.create_task(main())

            def stop_fn():
                loop.call_soon_threadsafe(task.cancel)
            return task, stop_fn

        rt.start(factory)
        time.sleep(0.3)
        # Worker finished cleanly; no error, thread gone
        self.assertEqual(rt.error, "")
        self.assertFalse(rt.running)


class TestStopFnRuntimeError(unittest.TestCase):
    def test_stop_fn_runtime_error_swallowed_and_timeout_reported(self):
        rt = AsyncRuntime("test", stop_timeout=0.1)

        def factory(loop):
            async def main():
                await asyncio.sleep(5)
            task = loop.create_task(main())

            def stop_fn():
                raise RuntimeError("cannot signal stop")
            return task, stop_fn

        rt.start(factory)
        time.sleep(0.2)
        # stop_fn raises RuntimeError (swallowed) and the thread outlives the
        # tiny join timeout, so stop() returns False.
        self.assertFalse(rt.stop())


if __name__ == "__main__":
    unittest.main()
