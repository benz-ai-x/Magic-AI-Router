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
import time
from concurrent.futures import ThreadPoolExecutor

from sysctl import port_check, sleep_blocker
from sysctl.instance_owner import InstanceOwner
from capture.capture import CaptureMonitor
from capture.capture_controller import CaptureController
from services.suanpan_runtime import SuanpanRuntime
from services.config_server import ConfigServer
from sysctl.sys_proxy_controller import SystemProxyController
from services import sp_config
from shared import netloc
from shared.defaults import DEFAULT_GATEWAY_PORT

logger = logging.getLogger("magic-proxy.lifecycle")


def _should_prevent_sleep(status, paused, flag):
    """Only hold caffeinate while connected, not paused, and opted in."""
    return bool(flag and status == "connected" and not paused)


def config_server_wanted(window_open, api_enabled, copy_latch):
    """配置服务持有状态机（ADR-009）——纯函数，菜单/关窗/手势三入口共用。

    任一持有者在场即需要监听 :9528：设置窗开着 / config_api_enabled
    常驻开关 / 「复制 AI 助手指令」会话闩锁（agent 后续 curl 依赖）。
    全部离场即释放端口——默认态零配置面监听。
    """
    return bool(window_open or api_enabled or copy_latch)


# ── 网关健康对账（watchdog）参数 ────────────────────────────────
_GW_AUDIT_EVERY = 5      # 审计节奏：每 5 拍（1s tick → 5s）
_GW_MISS_THRESHOLD = 3   # 连续失配阈值：≈15s 检出，合法 reload 空窗 3-5s 不误触
_GW_HEAL_BACKOFF = 30    # 自愈失败退避（对齐 MOUNT_RETRY_BACKOFF 先例）




def report_port_occupancy(config_port=9528,
                           suanpan_port=DEFAULT_GATEWAY_PORT):
    """启动期端口占用报告（issue #3）：占用只是线索，启动期永不发信号。

    进程死亡即释放监听 socket——真正需要回收的旧实例只剩**陈旧锁**
    （由 InstanceOwner.acquire 的接管路径清理）。仍被占用的端口只可能是
    活进程（本应用旧实例由单胜守卫拦截并给出用户可见错误；其余为外来
    进程），一律清晰告警、人工处置。port_check.kill 的 SIGTERM→SIGKILL
    升级保留给显式人工/工具路径。
    """
    self_pid = os.getpid()
    for port in (config_port, suanpan_port):
        if port is None:
            continue  # None = 本次不绑定该端口（ADR-009 配置 API 默认关）
        po = port_check.who_owns(port)
        if not po or po.pid == self_pid:
            continue
        logger.warning(
            "Port %d occupied by PID %d (%s) — 不自动处理；如为本应用旧实例"
            "请从其菜单栏退出，外来进程请手动确认后处置", port, po.pid, po.name)


def _read_suanpan_port():
    """Read the gateway port via 配置存储; fallback to DEFAULT_GATEWAY_PORT."""
    try:
        return netloc.parse_listen(sp_config.suanpan_listen(), default_port=DEFAULT_GATEWAY_PORT)[1]
    except Exception:
        return DEFAULT_GATEWAY_PORT


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
        runtime_state_fn=None,
        on_mp_saved=None,
        clock=time.monotonic,
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
            on_mp_saved=on_mp_saved,
            port=cfg.get("config_port", 9528),
            runtime_state_fn=runtime_state_fn,
        )
        # 网关健康对账（watchdog）：状态旗标 vs 端口真相。挂载协调器
        # 同款纪律——tick 主线程只做轻检查，自愈动作丢 worker。
        self._clock = clock
        self._gw_workers = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="GatewayHeal")
        self._gw_tick = 0        # 计拍（每 _GW_AUDIT_EVERY 拍审计一次）
        self._gw_misses = 0      # 连续失配计数
        self._gw_healing = False  # 自愈 worker 忙位
        self._gw_next_heal = 0.0  # 失败退避截止（clock 基准）

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

    # ── reload 链内化：配置服务线程 → 网关线程，不出模块 ──────────
    def _on_sp_saved(self):
        # #71 W9 收敛：reload-or-start 语义单一归宿在 SuanpanRuntime
        self._suanpan.reload_or_start()

    # ── 生命周期 ────────────────────────────────────────────────
    def start_all(self):
        """启动顺序即契约：实例锁单胜守卫 → 端口占用报告 → 配置服务 → 网关自启。

        单实例守卫（issue #3）：锁被活实例持有时本次启动不接管任何
        服务，返回 False——由编排器（app.py）转为用户可见错误后退出。
        """
        if not self._owner.acquire():
            logger.error("已有 Magic AI Router 实例在运行（实例锁被持有）——"
                         "本次启动不接管服务")
            return False
        # 跨文件提交崩溃恢复（issue #6）：journal 残留则幂等重放补齐
        # （与 Docker 形态同一归宿 recover_pending_txn）
        from mpconf.config_state import recover_pending_txn
        recover_pending_txn()
        config_port = (self._config_fn() or {}).get("config_port", 9528)
        # ADR-009：配置 API 默认不常驻——仅显式开关打开时随应用启动
        # 监听；默认态零配置面端口（设置窗/复制指令手势按需起停）。
        api_enabled = bool((self._config_fn() or {}).get("config_api_enabled"))
        report_port_occupancy(
            config_port if api_enabled else None, _read_suanpan_port())
        if api_enabled and not self._config_server.start():
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

    def sync_config_server(self, wanted):
        """按持有状态收敛 :9528（ADR-009）：wanted=True 起服务（幂等），
        False 即释放端口。调用方 app.py 经 config_server_wanted 重算。"""
        if wanted:
            if not self._config_server.start():
                logger.warning("Config server failed to start on :%d",
                               self._config_server.port)
                return False
            return True
        self._config_server.stop()
        return True

    def tick(self, capture_port):
        """Per-second: capture check + system proxy sync + 网关对账."""
        if self._capture_ctrl.enabled:
            self._capture.check(capture_port)
        self._sys_proxy.sync()
        self._reconcile_gateway()

    def _reconcile_gateway(self):
        """网关健康对账（watchdog）：running 旗标 vs 端口真相（挂载
        协调器同款 reconcile 纪律——主线程只做轻检查，动作丢 worker）。

        谓词只认一种失配：running 且端口无人听（僵尸态）。用户显式
        停止/崩溃（stopped）绝不拉起；合法 reload 的端口空窗由
        「阈值连续失配」吸收。失败退避防死循环重试。
        """
        self._gw_tick += 1
        if self._gw_tick % _GW_AUDIT_EVERY != 0:
            return
        if self._gw_healing:
            return
        verdict = self._suanpan.audit()
        if verdict != "mismatch":
            self._gw_misses = 0
            return
        self._gw_misses += 1
        if self._gw_misses < _GW_MISS_THRESHOLD:
            return
        if self._clock() < self._gw_next_heal:
            return
        self._gw_healing = True
        self._gw_workers.submit(self._gateway_heal_job)

    def _gateway_heal_job(self):
        """worker 线程：重建网关（start 内含僵尸 stop + 3s join，
        绝不在主线程 tick 里跑）。忙位/计数复位在 finally 单出口。"""
        try:
            ok = self._suanpan.start()
            if ok:
                logger.info("网关对账自愈：检测到僵尸态（running 但端口"
                            "无人听），已重建")
            else:
                logger.warning("网关对账自愈失败：%s",
                               (self._suanpan.error or "未知原因")[:160])
            self._gw_next_heal = 0.0 if ok else \
                self._clock() + _GW_HEAL_BACKOFF
        finally:
            self._gw_misses = 0
            self._gw_healing = False

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
        self._gw_workers.shutdown(wait=False)
        self._blocker.release()
        self._capture.stop(blocking=False)
