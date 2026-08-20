"""LifecycleRuntime: 后台服务生命周期的单一编排点（架构候选 2+3 落地）。

构造并编排五条服务线（Suanpan 网关 / 抓包 / 系统代理 / 防睡眠 / 配置服务），
MagicProxyApp 只见五个方法：

- ``start_all()``  — 启动顺序即契约：清障自有端口 → 配置服务 → 网关自启
- ``quit(ssh_stop)`` — 退出顺序即契约：系统代理恢复 → ``ssh_stop()`` →
  网关/防睡眠/抓包 → 配置服务（此前这条顺序只活在 app.py 的注释里）
- ``tick(capture_port)`` — per-second：capture 检查 + 系统代理收敛
- ``sync_sleep(...)`` — caffeinate 断言边沿收敛
- ``stop_all()`` — 三条服务线的退出清理（不含系统代理恢复与 SSH）

「抓包正在运行」在本模块持有**单一投影**（读 CaptureController），对两个
消费者内部适配：SystemProxyController 要 ``(enabled, status)`` 元组，
ConfigServer 要 bool。原 ServiceCoordinator 的两阶段构造
（``set_capture_state_fn`` 补设 + 跨对象私有属性写入）随构造期直属引用
而消亡。

Suanpan 保存后的 reload 链（配置服务线程 → 网关线程）内化为
``_on_sp_saved``，不再经 app.py 的中转 lambda。
"""
from __future__ import annotations

import logging
import os
import sys

from sysctl import port_check, sleep_blocker
from sysctl.instance_owner import InstanceOwner
from capture.capture import CaptureMonitor
from capture.capture_controller import CaptureController
from services.suanpan_runtime import SuanpanRuntime
from services.config_server import ConfigServer
from sysctl.sys_proxy_controller import SystemProxyController
from mpconf import config_store, netloc

logger = logging.getLogger("magic-proxy.lifecycle")


def _should_prevent_sleep(status, paused, flag):
    """Only hold caffeinate while connected, not paused, and opted in."""
    return bool(flag and status == "connected" and not paused)




def _clear_stale_ports(config_port=9528, suanpan_port=9527, owner=None):
    """Recover ports held by VERIFIED stale instances of this app (issue #3).

    端口占用只是发现线索；只有实例锁（pid + 启动时间双匹配）证实的自家
    旧实例才回收（port_check.kill 自带 SIGTERM→SIGKILL 升级）。未证实
    的占用者永远不发信号，仅清晰告警——basename 启发式已删除。
    """
    self_pid = os.getpid()
    recovered = set()  # 同一验证过的旧实例可能占着两个端口——只杀一次
    for port in (config_port, suanpan_port):
        po = port_check.who_owns(port)
        if not po or po.pid == self_pid:
            continue
        if po.pid in recovered:
            continue
        if owner is None or not owner.owns_pid(po.pid):
            logger.warning(
                "Port %d occupied by PID %d (%s) — 所有权未证实，不自动处理；"
                "如确认可手动退出该进程后重试", port, po.pid, po.name)
            continue
        logger.info("Port %d held by verified stale instance PID %d — recovering",
                    port, po.pid)
        recovered.add(po.pid)
        ok, err = port_check.kill(po.pid)
        if ok:
            logger.info("Killed PID %d on port %d", po.pid, port)
        else:
            logger.warning("Failed to kill PID %d on port %d: %s",
                           po.pid, port, err)


def _read_suanpan_port():
    """Read the gateway port via 配置存储; fallback to 9527."""
    try:
        return netloc.parse_listen(config_store.suanpan_listen(), default_port=9527)[1]
    except Exception:
        return 9527


class LifecycleRuntime:
    """Construct + start + tear down every background service in one place."""

    def __init__(
        self,
        config_fn,
        ssh_monitor,
        paused_fn,
        on_menu_dirty,
        initial_sys_proxy_on=False,
        instance_owner=None,
    ):
        self._config_fn = config_fn
        self._owner = instance_owner or InstanceOwner()
        self._suanpan = SuanpanRuntime()
        self._capture = CaptureMonitor()
        self._capture_ctrl = CaptureController(
            self._capture, config_fn=config_fn, on_dirty=on_menu_dirty)
        self._sys_proxy = SystemProxyController(
            ssh_monitor=ssh_monitor,
            capture_state=self._capture_state_tuple,
            config_fn=config_fn,
            paused_fn=paused_fn,
            on_dirty=on_menu_dirty,
            initial_on=initial_sys_proxy_on,
        )
        self._blocker = sleep_blocker.CaffeinateBlocker()
        self._caffeinate_on = False
        cfg = config_fn() or {}
        self._config_server = ConfigServer(
            on_sp_saved=self._on_sp_saved,
            port=cfg.get("config_port", 9528),
            capture_state=self._capture_state_bool,
        )

    # ── 直属子模块的合法暴露面（app.py 菜单/桥接需要直接引用）──────
    @property
    def suanpan(self):
        return self._suanpan

    @property
    def capture_ctrl(self):
        return self._capture_ctrl

    @property
    def sys_proxy(self):
        return self._sys_proxy

    @property
    def capture(self):
        return self._capture

    @property
    def config_server(self):
        return self._config_server

    # ── 「抓包正在运行」的单一投影 + 两个消费者的内部适配 ──────────
    def _capture_state_tuple(self):
        return (self._capture_ctrl.enabled, self._capture_ctrl.status)

    def _capture_state_bool(self):
        return bool(self._capture_ctrl.enabled
                    and self._capture_ctrl.status == "running")

    # ── reload 链内化：配置服务线程 → 网关线程，不出模块 ──────────
    def _on_sp_saved(self):
        self._suanpan.reload()

    # ── 生命周期 ────────────────────────────────────────────────
    def start_all(self):
        """启动顺序即契约：实例锁 → 清障自有端口 → 配置服务 → 网关自启。

        并发启动单实例守卫（issue #3）：锁被活实例持有时本次启动不接管
        任何服务，返回 False。
        """
        if not self._owner.acquire():
            logger.error("已有 Magic AI Router 实例在运行（实例锁被持有）——"
                         "本次启动不接管服务")
            return False
        config_port = (self._config_fn() or {}).get("config_port", 9528)
        _clear_stale_ports(config_port, _read_suanpan_port(), self._owner)
        if not self._config_server.start():
            logger.warning("Config server failed to start on :%d", config_port)
        # AI router gateway auto-starts with the app (loopback-only);
        # users can still stop it from the AI 路由 menu.
        if not self._suanpan.start():
            logger.warning("Suanpan gateway auto-start failed: %s",
                           self._suanpan.error[:120])
        return True

    def quit(self, ssh_stop):
        """退出顺序即契约：系统代理恢复 → SSH 停止 → 服务线 → 配置服务。"""
        self._sys_proxy.quit_cleanup()
        ssh_stop()
        self.stop_all()
        self._config_server.stop()
        self._owner.release()

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
        """Quit cleanup for the service lines: gateway, sleep, capture."""
        if self._suanpan.running:
            self._suanpan.stop()
        self._blocker.release()
        self._capture.stop(blocking=False)
