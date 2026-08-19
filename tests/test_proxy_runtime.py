import asyncio
import time
from unittest.mock import patch

from proxy import ProxyRuntime
from stats import Stats


async def _fake_proxy(config, stats, control):
    control["server"] = object()
    await asyncio.Event().wait()


def test_restart_stops_old_generation_before_new_one():
    runtime = ProxyRuntime(Stats())
    with patch("proxy.run_proxy", side_effect=_fake_proxy):
        runtime.start({"socks5_port": 1080, "http_listen_port": 8888})
        first = runtime._rt._thread
        runtime.start({"socks5_port": 1081, "http_listen_port": 8889})
        second = runtime._rt._thread
        assert first is not second
        assert not first.is_alive()
        assert second.is_alive()
        assert runtime.stop()
        assert not second.is_alive()


def test_rapid_start_stop_does_not_leave_threads():
    runtime = ProxyRuntime(Stats())
    with patch("proxy.run_proxy", side_effect=_fake_proxy):
        for port in range(8890, 8900):
            runtime.start({"socks5_port": 1080, "http_listen_port": port})
            time.sleep(0.005)
        assert runtime.stop()
        assert not runtime.running
