"""网关健康对账策略单一归宿（架构评审 R5 候选 3）。

「何时判死、何时重建」的策略参数——审计节奏（audit_every 拍）/ 连失配
阈值（miss_threshold，吸收合法 reload 的 3-5s 端口空窗）/ 失败退避
（heal_backoff）/ 忙位互斥——此前 macOS（LifecycleRuntime 1s tick）与
Docker（entry 主循环）各持一份，docker 侧是丢了阈值与退避的简化手抄：
单采样落进 reload 空窗即误判僵尸态，start() 内含 stop()，与 reload
线程竞态（CONTEXT.md 部署形态条目：docker/entry.py 只装配，抄写函数
体=回归）。

谓词（audit_fn，语义单一归宿在 SuanpanRuntime.audit）与自愈动作
（start_fn）由调用方注入；两个 adapter 只负责喂拍与选择执行器——
macOS 传 worker submit（绝不在 1s tick 线程跑重建），Docker 主循环
内联执行（容器主线程无并发面）。用户停止/崩溃（stopped）绝不拉起。
"""
from __future__ import annotations

import logging
import time


class GatewayWatchdog:
    """僵尸态对账策略：连失配阈值 + 失败退避 + 忙位。tick() 喂一拍。"""

    def __init__(self, audit_fn, start_fn, *,
                 error_fn=lambda: "",
                 log=None, clock=time.monotonic,
                 audit_every=5, miss_threshold=3, heal_backoff=30.0,
                 submit=None):
        self._audit_fn = audit_fn
        self._start_fn = start_fn
        self._error_fn = error_fn
        self._log = log or logging.getLogger("magic-proxy.gateway-watchdog")
        self._clock = clock
        self._audit_every = max(1, int(audit_every))
        self._miss_threshold = max(1, int(miss_threshold))
        self._heal_backoff = heal_backoff
        self._submit = submit or self._run_job_inline
        self._tick = 0
        self._misses = 0
        self._healing = False
        self._next_heal = 0.0

    @staticmethod
    def _run_job_inline(job):
        job()

    def tick(self):
        """喂一拍。审计每 audit_every 拍一次；失配连达 miss_threshold
        且不在退避窗、不在自愈中，才派发重建动作。"""
        self._tick += 1
        if self._tick % self._audit_every != 0:
            return
        if self._healing:
            return
        if self._audit_fn() != "mismatch":
            self._misses = 0
            return
        self._misses += 1
        if self._misses < self._miss_threshold:
            return
        if self._clock() < self._next_heal:
            return
        self._healing = True
        self._submit(self._heal_job)

    def _heal_job(self):
        """重建网关（start 内含僵尸 stop + join）。复位在 finally
        单出口——忙位/计数绝不因失败路径泄漏。"""
        try:
            ok = self._start_fn()
            if ok:
                self._log.info("网关对账自愈：检测到僵尸态"
                               "（running 但端口无人听），已重建")
            else:
                self._log.warning("网关对账自愈失败：%s",
                                  (self._error_fn() or "未知原因")[:160])
            self._next_heal = 0.0 if ok else \
                self._clock() + self._heal_backoff
        finally:
            self._misses = 0
            self._healing = False
