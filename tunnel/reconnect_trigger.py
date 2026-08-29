"""唤醒事件 → 立即重连触发器（#86）。

ReconnectTrigger —— 事件去抖限频（纯 Python 可单测）。
WakeEventSource —— NSWorkspace 唤醒通知安装；PyObjC 缺失时 start()
返回 False 静默降级。事件只做「提前触发重连」（跳过退避），不改
连接状态机语义。网络变化事件源（SCNetworkReachability）需可选依赖
pyobjc-framework-SystemConfiguration，未安装时由 #85 的无限退避兜底
（ssh 退出 → ≤60s 内自动重试）。
"""
import logging
import threading
import time

logger = logging.getLogger("magic-proxy.reconnect")


class ReconnectTrigger:
    """事件风暴限频：min_interval 窗口内只放行首个事件。

    回调在触发线程（通知中心所在线程）执行，锁外调用——回调可安全
    再取协调器生命周期锁。
    """

    def __init__(self, on_reconnect, min_interval=2.0, clock=time.monotonic):
        self._on_reconnect = on_reconnect
        self._min_interval = min_interval
        self._clock = clock
        self._last_fire = None
        self._lock = threading.Lock()

    def notify(self):
        """事件源入口：窗口外首个事件触发回调，窗口内丢弃。"""
        now = self._clock()
        with self._lock:
            if (self._last_fire is not None
                    and now - self._last_fire < self._min_interval):
                return
            self._last_fire = now
        self._on_reconnect()


_OBSERVER_CLS = None


def _wake_observer_cls():
    """惰性构建并缓存 ObjC 观察者类——同名 ObjC 类每进程只能定义一次。"""
    global _OBSERVER_CLS
    if _OBSERVER_CLS is None:
        from Foundation import NSObject

        class WakeObserver(NSObject):
            # PyObjC 约束：方法名不得以单下划线开头（selector 冲突）。
            # 回调经实例属性注入（类只定义一次，回调各source不同）。
            def onWake_(self, _note):
                self.handler()

        _OBSERVER_CLS = WakeObserver
    return _OBSERVER_CLS


class WakeEventSource:
    """NSWorkspaceDidWakeNotification 观察者；start() 幂等。"""

    def __init__(self, on_wake):
        self._on_wake = on_wake
        self._observer = None

    def start(self):
        """注册唤醒观察；PyObjC 不可用返回 False（调用方静默降级）。"""
        if self._observer is not None:
            return True
        try:
            import AppKit
            import objc  # noqa: F401 — 仅探测可用性
        except Exception:
            logger.info("PyObjC 不可用——唤醒重连触发器未安装")
            return False
        observer = _wake_observer_cls().alloc().init()
        observer.handler = self._on_wake
        self._observer = observer
        center = AppKit.NSWorkspace.sharedWorkspace().notificationCenter()
        center.addObserver_selector_name_object_(
            observer, "onWake:",
            AppKit.NSWorkspaceDidWakeNotification, None)
        logger.info("唤醒重连触发器已安装（NSWorkspaceDidWakeNotification）")
        return True
