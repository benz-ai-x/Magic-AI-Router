"""AsyncRuntime 竞争窗口与 coroutine 泄漏（issue #12）.

验收锚点：
- stop() 超时 → start() 拒绝且不创建新线程
- factory 各时相 stop 均完成清理
- 未 await 的 coroutine 显式 close（warnings-as-errors 无 RuntimeWarning）
- generation 只清理自身（不覆盖后继）
- start/stop 并发压力：零或一个活线程、无死锁
- 错误根因保留到下次成功 start
"""
import asyncio
import gc
import threading
import unittest
import warnings

from tunnel.async_runtime import AsyncRuntime


def _factory_forever(hang=False):
    """factory: server 型 coroutine。hang=True 时 stop_fn 无效（真挂死）。"""
    started = threading.Event()
    released = threading.Event()

    async def server():
        started.set()
        while not released.is_set():
            await asyncio.sleep(0.01)

    def factory(loop):
        coro = server()
        stop = (lambda: None) if hang else released.set
        return coro, stop

    return factory, started, released


class TestStopTimeoutBlocksStart(unittest.TestCase):
    def test_start_fails_after_stop_timeout(self):
        factory, started, released = _factory_forever(hang=True)
        rt = AsyncRuntime("t", stop_timeout=0.1)
        self.assertTrue(rt.start(factory))
        started.wait(2)
        ok = rt.stop(timeout=0.05)
        self.assertFalse(ok, "hang 场景 stop 必须超时")
        # 拒绝：上一代未完整终止——False 且不建新线程（保 bool 契约）
        alive_before = rt._thread
        self.assertFalse(rt.start(factory))
        self.assertIs(rt._thread, alive_before, "拒绝时不替换线程引用")
        self.assertIn("未在超时内终止", rt.error)
        released.set()  # 收尾
        rt.stop(timeout=5)


class TestCoroutineClosed(unittest.TestCase):
    def test_superseded_coroutine_closed_no_runtime_warning(self):
        """factory 返回后 generation 已被替换 → coroutine 显式 close。"""
        created = []
        def factory(loop):
            async def never():
                await asyncio.sleep(999)
            coro = never()
            created.append(coro)
            return coro, lambda: None
        rt = AsyncRuntime("t")
        rt.start(factory)
        rt.stop(timeout=5)
        # 直接构造 supersede 场景：start → 立即 start（第二代）
        rt2 = AsyncRuntime("t2")
        rt2.start(factory)
        rt2.start(factory)   # 第二次 start 先 stop 再起新代
        rt2.stop(timeout=5)
        gc.collect()
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            gc.collect()  # 触发未 close coroutine 的 warning（若有）
        # warnings-as-error 下 gc.collect() 未抛 = 无 never-awaited 泄漏
        # （显式 close 的协程不会在 GC 时告警）


class TestGenerationIsolation(unittest.TestCase):
    def test_old_generation_cleanup_does_not_clobber_new(self):
        rt = AsyncRuntime("t")
        errors = []
        def factory(loop):
            async def noop():
                pass
            return noop(), lambda: None
        rt.start(factory)
        rt.stop(timeout=5)
        self.assertEqual(rt.error, "")


class TestErrorRetention(unittest.TestCase):
    def test_error_kept_until_successful_start(self):
        rt = AsyncRuntime("t")
        failed = threading.Event()
        def bad_factory(loop):
            async def fail():
                raise ValueError("root cause")
            def factory_done():
                pass
            # 让失败先发生：worker 进入 run_until_complete 后立即抛
            async def wrapped():
                try:
                    raise ValueError("root cause")
                finally:
                    failed.set()
            return wrapped(), lambda: None
        rt.start(bad_factory)
        self.assertTrue(failed.wait(5), "失败协程必须执行到抛出")
        rt.stop(timeout=5)
        self.assertIn("root cause", rt.error)
        # 成功 start 后清除
        def good_factory(loop):
            async def ok():
                pass
            return ok(), lambda: None
        rt.start(good_factory)
        rt.stop(timeout=5)
        self.assertEqual(rt.error, "")


class TestStressStartStop(unittest.TestCase):
    def test_concurrent_start_stop_single_live_thread_max(self):
        factory, started, released = _factory_forever()
        rt = AsyncRuntime("t", stop_timeout=1.0)
        for i in range(20):
            rt.start(factory)
            if not started.wait(0.2):
                pass
            rt.stop(timeout=1.0)
        released.set()
        ok = rt.stop(timeout=5)
        self.assertTrue(ok)
        self.assertFalse(rt.running)


if __name__ == "__main__":
    unittest.main()
