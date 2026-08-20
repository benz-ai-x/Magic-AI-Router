"""ProviderPrewarmer（issue #15）：有界并发、总预算、可取消.

readiness 不被单个慢 Provider 阻塞；预热失败不改变 Provider 配置；
取消/shutdown 不遗留任务。
"""
import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock

from suanpan.prewarmer import ProviderPrewarmer


def _providers(n, slow_names=()):
    return {f"p{i}": MagicMock(base_url=f"https://p{i}.test",
                                enabled=(f"p{i}" not in slow_names))
            for i in range(n)}


class TestBoundedConcurrency(unittest.TestCase):
    def test_total_budget_caps_wait(self):
        """总预算固定：N 个超时 Provider 不线性拖慢 readiness。"""
        pw = ProviderPrewarmer(max_concurrent=3, total_budget=0.3)
        slow = {f"s{i}": MagicMock(base_url=f"https://s{i}.test", enabled=True)
                for i in range(8)}
        client = MagicMock(spec=[])
        async def hang(*a, **kw):
            await asyncio.sleep(5)
        client.head = hang
        t0 = time.monotonic()
        asyncio.run(pw.warm(slow, client))
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0,
                        f"8 个超时 Provider 等了 {elapsed:.2f}s——总预算泄漏")

    def test_readiness_not_blocked_by_one_slow_provider(self):
        pw = ProviderPrewarmer(max_concurrent=2, total_budget=0.5)
        providers = {
            "slow": MagicMock(base_url="https://slow.test", enabled=True),
            "fast": MagicMock(base_url="https://fast.test", enabled=True),
        }
        done = []
        async def head(url, **kw):
            if "slow" in url:
                await asyncio.sleep(5)
            done.append(url)
        client = MagicMock(spec=[])
        client.head = head
        t0 = time.monotonic()
        asyncio.run(pw.warm(providers, client))
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertTrue(any("fast" in u for u in done),
                        "快 Provider 必须能先完成")

    def test_prewarm_failure_does_not_change_config(self):
        pw = ProviderPrewarmer()
        providers = _providers(2)
        client = MagicMock(spec=[])
        async def fail(*a, **kw):
            raise OSError("unreachable")
        client.head = fail
        asyncio.run(pw.warm(providers, client))
        for p in providers.values():
            self.assertTrue(p.enabled, "预热失败不得改 Provider 配置")

    def test_cancel_leaves_no_tasks(self):
        async def run():
            pw = ProviderPrewarmer(max_concurrent=2, total_budget=30)
            providers = _providers(4)
            client = MagicMock(spec=[])
            async def hang(*a, **kw):
                await asyncio.sleep(10)
            client.head = hang
            task = asyncio.ensure_future(pw.warm(providers, client))
            await asyncio.sleep(0.05)  # 让子任务先跑起来
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            pending = [t for t in asyncio.all_tasks()
                       if t is not asyncio.current_task()]
            self.assertEqual(pending, [], "取消后不遗留子任务")
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
