"""Shared asyncio runtime: lock-provable state machine (issue #12).

ProxyRuntime and SuanpanRuntime both need "spawn a daemon thread that owns an
asyncio event loop, run a server coroutine, and stop cleanly before starting a
new generation." This module factors that lifecycle into a single deep module.

状态机（锁下判定）：STOPPED → STARTING → RUNNING → STOPPING → STOPPED；
异常路径 RUNNING → FAILED（根因保留到下次成功 start）。start 只在上一代
完整终止后发布新代；stop 超时 → start 拒绝（RuntimeError）且不建新线程。
每个 awaitable 恰好被 await 或 close 一次。
"""
from __future__ import annotations

import asyncio
import logging
import threading

logger = logging.getLogger("magic-proxy.async_runtime")

_STOPPED = "STOPPED"
_STARTING = "STARTING"
_RUNNING = "RUNNING"
_STOPPING = "STOPPING"
_FAILED = "FAILED"


class AsyncRuntime:
    """Own one generation of asyncio loop + thread; stop before replacement."""

    def __init__(self, name: str, stop_timeout: float = 5.0):
        self._name = name
        self._stop_timeout = stop_timeout
        self._lock = threading.Lock()
        self._state = _STOPPED
        self._generation = 0
        self._thread = None
        self._loop = None
        self._stop_fn = None
        self._stop_event = None
        self._error = ""

    @property
    def running(self) -> bool:
        with self._lock:
            if self._state != _RUNNING:
                return False
            # 旧契约兼容：stop_event 已置位 = 不再运行（即便线程未死）
            return bool(self._stop_event and not self._stop_event.is_set())

    @property
    def error(self) -> str:
        with self._lock:
            return self._error

    def start(self, coro_factory) -> bool:
        """Start a new generation. 拒绝条件：上一代未完整终止——返回 False
        且不创建新线程（error 记录根因，调用方可重试或放弃）。"""
        if not self._shutdown_previous():
            with self._lock:
                self._error = "上一代线程未在超时内终止，拒绝 start"
            return False
        generation = 0
        stop_event = threading.Event()
        with self._lock:
            if self._state not in (_STOPPED, _FAILED):
                with self._lock:
                    pass
                self._error = f"状态 {self._state} 不可启动"
                return False
            self._generation += 1
            generation = self._generation
            self._stop_event = stop_event
            self._state = _STARTING
            self._error = "" if self._state == _STARTING else self._error

        def worker():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            awaitable = None
            try:
                if stop_event.is_set():
                    return
                awaitable, stop_fn = coro_factory(loop)
                with self._lock:
                    if generation != self._generation:
                        # Superseded — close un-awaited coroutine
                        if asyncio.iscoroutine(awaitable):
                            awaitable.close()
                            awaitable = None
                        return
                    self._loop = loop
                    self._stop_fn = stop_fn
                if stop_event.is_set():
                    try:
                        stop_fn()
                    except Exception:
                        pass
                    return
                with self._lock:
                    if self._state == _STARTING:
                        self._state = _RUNNING
                loop.run_until_complete(awaitable)
                awaitable = None  # 已完整 await
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.exception("%s generation %d stopped", self._name, generation)
                with self._lock:
                    if generation == self._generation:
                        self._error = str(exc)
                        self._state = _FAILED
            finally:
                if asyncio.iscoroutine(awaitable):
                    awaitable.close()  # 任何路径未 await 的都显式 close
                try:
                    pending = asyncio.all_tasks(loop)
                    for t in pending:
                        t.cancel()
                    if pending:
                        loop.run_until_complete(
                            asyncio.gather(*pending, return_exceptions=True))
                finally:
                    loop.close()
                with self._lock:
                    if generation == self._generation:
                        self._loop = None
                        self._stop_fn = None
                        self._stop_event = None
                        self._thread = None
                        if self._state != _FAILED:
                            self._state = _STOPPED

        thread = threading.Thread(
            target=worker, name=f"{self._name}-{generation}", daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()
        return True

    def _shutdown_previous(self) -> bool:
        """stop 上一代并确认线程终止；超时 False。"""
        with self._lock:
            if self._state in (_STOPPED, _FAILED) and not (
                    self._thread and self._thread.is_alive()):
                return True
            self._state = _STOPPING
        return self.stop()

    def stop(self, timeout: float | None = None) -> bool:
        """Signal stop and join the worker thread."""
        timeout = timeout if timeout is not None else self._stop_timeout
        with self._lock:
            thread = self._thread
            stop_fn = self._stop_fn
            stop_event = self._stop_event
            if self._state == _STARTING:
                self._state = _STOPPING
        if stop_event:
            stop_event.set()
        if stop_fn:
            try:
                stop_fn()
            except RuntimeError:
                pass
        if thread and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        alive = bool(thread and thread.is_alive())
        if alive:
            logger.error("%s thread did not stop within %.1fs", self._name, timeout)
            with self._lock:
                if self._state != _FAILED:
                    self._state = _RUNNING if not stop_event else self._state
        else:
            with self._lock:
                if self._state not in (_FAILED,):
                    self._state = _STOPPED
        return not alive
