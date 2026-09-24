"""GatewayWatchdog 策略单测（架构评审 R5 候选 3）。

组合面（LifecycleRuntime.tick 驱动 / docker _watchdog_loop 驱动）已在
test_lifecycle_runtime / test_docker_entry 覆盖；此处钉策略本体的
执行器语义：忙位互斥（submit 入队不等待——真实 worker 异步）、默认
内联执行（docker 主循环）、退避窗。
"""
import unittest
from unittest.mock import MagicMock

from services.gateway_watchdog import GatewayWatchdog


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class TestGatewayWatchdog(unittest.TestCase):

    def test_busy_flag_blocks_audits_until_job_finishes(self):
        # macOS 执行纪律：submit 入队不等待——忙位期间不再审计、不重建
        clock = _Clock()
        runner = MagicMock()
        runner.audit.return_value = "mismatch"
        runner.start.return_value = True
        queued = []

        wd = GatewayWatchdog(
            runner.audit, runner.start, clock=clock,
            audit_every=1, miss_threshold=1,
            submit=lambda job: queued.append(job))

        wd.tick()                      # 失配即入队（threshold=1）
        self.assertEqual(len(queued), 1)
        audits_after_dispatch = runner.audit.call_count
        for _ in range(5):
            wd.tick()                  # 忙位：audit 早退
        self.assertEqual(runner.audit.call_count, audits_after_dispatch)
        self.assertEqual(runner.start.call_count, 0)  # job 未跑不重建

        queued[0]()                    # worker 完成：复位忙位/计数
        self.assertEqual(runner.start.call_count, 1)
        wd.tick()                      # 再失配 → 再入队
        self.assertEqual(len(queued), 2)

    def test_default_submit_runs_inline(self):
        # docker 执行纪律：默认内联——tick 调用栈内完成重建
        runner = MagicMock()
        runner.audit.return_value = "mismatch"
        runner.start.return_value = True
        wd = GatewayWatchdog(runner.audit, runner.start,
                             audit_every=1, miss_threshold=1)
        wd.tick()
        self.assertEqual(runner.start.call_count, 1)

    def test_failed_heal_backoff_window(self):
        clock = _Clock()
        runner = MagicMock()
        runner.audit.return_value = "mismatch"
        runner.start.return_value = False
        wd = GatewayWatchdog(runner.audit, runner.start, clock=clock,
                             audit_every=1, miss_threshold=1,
                             heal_backoff=10.0)
        wd.tick()
        self.assertEqual(runner.start.call_count, 1)
        clock.advance(5.0)             # 退避窗内：不再派发
        wd.tick()
        self.assertEqual(runner.start.call_count, 1)
        clock.advance(6.0)             # 窗外 → 重试
        wd.tick()
        self.assertEqual(runner.start.call_count, 2)

    def test_successful_heal_clears_backoff(self):
        clock = _Clock()
        runner = MagicMock()
        runner.audit.return_value = "mismatch"
        runner.start.return_value = True
        wd = GatewayWatchdog(runner.audit, runner.start, clock=clock,
                             audit_every=1, miss_threshold=1,
                             heal_backoff=10.0)
        wd.tick()
        wd.tick()                      # 成功无退避：立即连击
        self.assertEqual(runner.start.call_count, 2)


if __name__ == "__main__":
    unittest.main()
