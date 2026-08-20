"""ProviderPrewarmer（issue #15）：best-effort 启动预热 adapter.

预热有总预算、并发上限、可取消性——readiness 不被单个慢 Provider
阻塞，预热失败不改变 Provider 配置，取消不遗留子任务。
"""
from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger("magic-proxy.suanpan.prewarm")

_DEFAULT_BUDGET = 5.0
_DEFAULT_CONCURRENCY = 4


class ProviderPrewarmer:
    """启动预热：有界并发 + 总预算 + 可取消（best-effort）。"""

    def __init__(self, max_concurrent=_DEFAULT_CONCURRENCY,
                 total_budget=_DEFAULT_BUDGET):
        self._max_concurrent = max_concurrent
        self._total_budget = total_budget

    async def warm(self, providers: dict, client) -> None:
        """对 enabled Provider 并发预热；总预算到时即收，不遗留任务。

        client：带 async head(url) 的对象（通常是 httpx.AsyncClient）。
        单个失败/超时只记日志，不抛。
        """
        sem = asyncio.Semaphore(self._max_concurrent)
        tasks = []

        async def one(name, p):
            async with sem:
                try:
                    await client.head(f"{p.base_url.rstrip('/')}/v1/messages")
                except Exception:
                    logger.debug("prewarm %s failed", name)

        for name, p in providers.items():
            if getattr(p, "enabled", True):
                tasks.append(asyncio.create_task(one(name, p)))
        if not tasks:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True),
                timeout=self._total_budget,
            )
        except asyncio.TimeoutError:
            logger.debug("prewarm total budget exceeded; %d providers pending",
                         len(tasks))
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
