"""Service coordinator: thin orchestration shell over service-level modules.

Candidate-1 refactor: ServiceCoordinator 从「持有子模块 + 暴露直通属性 +
封装 suanpan toggle/reload/restart 元组格式化」瘦身为「只负责 per-tick
capture/sys_proxy 检查 + caffeinate 收敛 + 退出清理」的薄编排层。

SuanpanRuntime / SystemProxyController / CaptureController 子模块现在由
MagicProxyApp 直接持有；suanpan toggle/reload/restart 的通知文案组装逻辑
（原 ``toggle_suanpan()`` / ``reload_suanpan()`` / ``restart_suanpan()``
返回 ``(running, address, error)`` 元组）也上提到 app.py。

保留的接口：
  __init__(...)             — 构造子模块（App 通过 ``_suanpan`` 等属性直接持有返回值）
  set_capture_state_fn(fn)  — 构造完成后补设 capture_state 闭包（F7 修复）
  tick(capture_port)        — per-second：capture 检查 + sys proxy sync
  sync_sleep(...)           — caffeinate 断言收敛（edge-triggered）
  stop_all()                — 退出清理
"""
from __future__ import annotations

import logging

from sysctl import sleep_blocker
from capture.capture import CaptureMonitor
from capture.capture_controller import CaptureController
from services.suanpan_runtime import SuanpanRuntime
from sysctl.sys_proxy_controller import SystemProxyController

logger = logging.getLogger("magic-proxy.service")


def _should_prevent_sleep(status, paused, flag):
    """Only hold caffeinate while connected, not paused, and opted in."""
    return bool(flag and status == "connected" and not paused)


class ServiceCoordinator:
    """Construct + orchestrate AI router, capture mode, system proxy, sleep blocker."""

    def __init__(
        self,
        config_fn,
        ssh_monitor,
        capture_state_fn,
        paused_fn,
        on_menu_dirty,
        initial_sys_proxy_on=False,
    ):
        self._suanpan = SuanpanRuntime()
        self._capture = CaptureMonitor()
        self._capture_ctrl = CaptureController(
            self._capture, config_fn=config_fn, on_dirty=on_menu_dirty)
        self._sys_proxy = SystemProxyController(
            ssh_monitor=ssh_monitor,
            capture_state=capture_state_fn,
            config_fn=config_fn,
            paused_fn=paused_fn,
            on_dirty=on_menu_dirty,
            initial_on=initial_sys_proxy_on,
        )
        self._blocker = sleep_blocker.CaffeinateBlocker()
        self._caffeinate_on = False

    def set_capture_state_fn(self, capture_state_fn):
        """补设 capture_state 闭包（F7：解除构造期前向引用耦合）。

        ServiceCoordinator 构造期 capture_state_fn 可以是占位值；
        App 在拿到直属子模块引用后调用本方法绑定真正的 capture_state_fn。
        """
        self._sys_proxy._capture_state = capture_state_fn

    def tick(self, capture_port):
        """Per-second: capture check + system proxy sync."""
        if self._capture_ctrl.enabled:
            self._capture.check(capture_port)
        self._sys_proxy.sync()

    def sync_sleep(self, ssh_status, paused, prevent_sleep_flag):
        """Converge caffeinate assertion (edge-triggered)."""
        desired = _should_prevent_sleep(ssh_status, paused, prevent_sleep_flag)
        if desired and not self._caffeinate_on:
            self._blocker.acquire()
            self._caffeinate_on = self._blocker.is_running
        elif not desired and self._caffeinate_on:
            self._blocker.release()
            self._caffeinate_on = False

    def stop_all(self):
        """Quit cleanup: stop gateway, release sleep, stop capture."""
        if self._suanpan.running:
            self._suanpan.stop()
        self._blocker.release()
        self._capture.stop(blocking=False)
