"""Shared asyncio runtime: one generation of loop + thread, stop before replace.

ProxyRuntime and SuanpanRuntime both need "spawn a daemon thread that owns an
asyncio event loop, run a server coroutine, and stop cleanly before starting a
new generation." This module factors that lifecycle into a single deep module.

Interface: start(factory) / stop() / running / error.
The factory builds the asyncio awaitable + a thread-safe stop callable.
"""
from __future__ import annotations

import asyncio
import logging
import threading

logger = logging.getLogger("magic-proxy.async_runtime")


class AsyncRuntime:
    """Own one generation of asyncio loop + thread; stop before replacement."""

    def __init__(self, name: str, stop_timeout: float = 5.0):
        self._name = name
        self._stop_timeout = stop_timeout
        self._lock = threading.Lock()
        self._generation = 0
        self._thread = None
        self._loop = None
        self._stop_fn = None
        self._stop_event = None
        self._error = ""

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(
                self._thread
                and self._thread.is_alive()
                and self._stop_event
                and not self._stop_event.is_set()
            )

    @property
    def error(self) -> str:
        with self._lock:
            return self._error

    def start(self, coro_factory) -> bool:
        """Start a new generation.

        coro_factory: callable(loop) -> (awaitable, stop_fn)
          - awaitable: passed to loop.run_until_complete()
          - stop_fn: called from stop() to signal shutdown (thread-safe)
        """
        self.stop()
        with self._lock:
            self._generation += 1
            generation = self._generation
            stop_event = threading.Event()
            self._stop_event = stop_event
            self._error = ""

        def worker():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                if stop_event.is_set():
                    return
                awaitable, stop_fn = coro_factory(loop)
                with self._lock:
                    if generation != self._generation:
                        # Superseded — close un-awaited coroutine to avoid
                        # "coroutine was never awaited" RuntimeWarning.
                        if asyncio.iscoroutine(awaitable):
                            awaitable.close()
                        return
                    self._loop = loop
                    self._stop_fn = stop_fn
                if stop_event.is_set():
                    try:
                        stop_fn()
                    except Exception:
                        pass
                    return
                loop.run_until_complete(awaitable)
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.exception("%s generation %d stopped", self._name, generation)
                with self._lock:
                    if generation == self._generation:
                        self._error = str(exc)
            finally:
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
                loop.close()
                with self._lock:
                    if generation == self._generation:
                        self._loop = None
                        self._stop_fn = None
                        self._stop_event = None
                        self._thread = None

        thread = threading.Thread(
            target=worker, name=f"{self._name}-{generation}", daemon=True)
        with self._lock:
            self._thread = thread
        thread.start()
        return True

    def stop(self, timeout: float | None = None) -> bool:
        """Signal stop and join the worker thread."""
        timeout = timeout if timeout is not None else self._stop_timeout
        with self._lock:
            thread = self._thread
            stop_fn = self._stop_fn
            stop_event = self._stop_event
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
        return not alive
