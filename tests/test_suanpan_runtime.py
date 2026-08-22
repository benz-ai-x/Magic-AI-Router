"""Tests for suanpan_runtime.py — lazy import, config creation, lifecycle.

Seams under test (confirmed):
- running / error / config_path: properties
- _ensure_config: first-run default config file creation
- start: lazy import graceful failure, thread spawn
- stop: should_exit signaling
- listen_address: cached fallback to default
"""
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from mpconf import config_store
from services.suanpan_runtime import SuanpanRuntime, DEFAULT_LISTEN


class TestInitialProperties(unittest.TestCase):
    def test_not_running_on_init(self):
        rt = SuanpanRuntime()
        self.assertFalse(rt.running)

    def test_no_error_on_init(self):
        rt = SuanpanRuntime()
        self.assertEqual(rt.error, "")

    def test_default_config_path(self):
        # 默认路径取自配置存储注册表（会话级沙盒已重定向）
        rt = SuanpanRuntime()
        self.assertEqual(rt.config_path, config_store.get_path("sp"))


class TestEnsureConfig(unittest.TestCase):
    def test_creates_config_if_missing(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sp.yaml")
            rt = SuanpanRuntime()
            rt._config_path = path
            rt._ensure_config()
            self.assertTrue(os.path.exists(path))
            with open(path) as f:
                content = f.read()
            self.assertIn("listen_port:", content)
            self.assertIn("providers: {}", content)

    def test_does_not_overwrite_existing(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sp.yaml")
            with open(path, "w") as f:
                f.write("custom: config")
            rt = SuanpanRuntime()
            rt._config_path = path
            rt._ensure_config()
            with open(path) as f:
                self.assertEqual(f.read(), "custom: config")

    def test_creates_parent_dir(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "dir", "sp.yaml")
            rt = SuanpanRuntime()
            rt._config_path = path
            rt._ensure_config()
            self.assertTrue(os.path.exists(path))


class TestListenAddress(unittest.TestCase):
    def test_returns_default_on_missing_config(self):
        rt = SuanpanRuntime()
        rt._config_path = "/nonexistent/path/to/config.yaml"
        self.assertEqual(rt.listen_address(), DEFAULT_LISTEN)

    def test_caches_result(self):
        rt = SuanpanRuntime()
        rt._cached_listen = "10.0.0.1:1234"
        # Should return cached without reading file
        self.assertEqual(rt.listen_address(), "10.0.0.1:1234")

    def test_reads_from_config_file(self):
        import yaml
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sp.yaml")
            with open(path, "w") as f:
                yaml.dump({"listen_port": 9999, "providers": {}}, f)
            rt = SuanpanRuntime()
            rt._config_path = path
            result = rt.listen_address()
            self.assertEqual(result, "127.0.0.1:9999")
            # Cached after first read
            self.assertEqual(rt._cached_listen, "127.0.0.1:9999")

    def test_reads_legacy_listen_string(self):
        # Backward compat: a legacy "host:port" listen string still resolves,
        # normalized to the loopback host.
        import yaml
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sp.yaml")
            with open(path, "w") as f:
                yaml.dump({"listen": "127.0.0.1:9998", "providers": {}}, f)
            rt = SuanpanRuntime()
            rt._config_path = path
            self.assertEqual(rt.listen_address(), "127.0.0.1:9998")


class TestStartMissingDeps(unittest.TestCase):
    def test_missing_dependency_returns_false(self):
        rt = SuanpanRuntime()
        err = ImportError("No module named 'fastapi'")
        err.name = "fastapi"
        with patch("builtins.__import__", side_effect=err):
            ok = rt.start()
        self.assertFalse(ok)
        self.assertIn("fastapi", rt.error)

    def test_missing_dep_error_message_actionable(self):
        rt = SuanpanRuntime()
        err = ImportError("No module named 'uvicorn'")
        err.name = "uvicorn"
        with patch("builtins.__import__", side_effect=err):
            rt.start()
        self.assertIn("pip3 install", rt.error)


class TestStop(unittest.TestCase):
    def test_stop_with_no_server(self):
        rt = SuanpanRuntime()
        result = rt.stop()
        self.assertTrue(result)  # nothing to stop = success

    def test_stop_delegates_to_async_runtime(self):
        rt = SuanpanRuntime()
        with patch.object(rt._rt, "stop", return_value=True) as mock_stop:
            result = rt.stop(timeout=7)
        mock_stop.assert_called_once_with(7)
        self.assertTrue(result)

    def test_stop_returns_false_if_thread_still_alive(self):
        rt = SuanpanRuntime()
        with patch.object(rt._rt, "stop", return_value=False):
            result = rt.stop(timeout=0.01)
        self.assertFalse(result)


class TestReload(unittest.TestCase):
    def test_reload_when_not_running_is_noop(self):
        rt = SuanpanRuntime()
        self.assertTrue(rt.reload())
        self.assertFalse(rt.running)

    def test_reload_when_running_calls_start(self):
        rt = SuanpanRuntime()
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        rt._rt._thread = mock_thread
        mock_stop = MagicMock()
        mock_stop.is_set.return_value = False
        rt._rt._stop_event = mock_stop
        rt._rt._state = "RUNNING"
        with patch.object(rt, "start", return_value=True) as mock_start:
            result = rt.reload()
        self.assertTrue(result)
        mock_start.assert_called_once()

    def test_reload_clears_cached_listen(self):
        rt = SuanpanRuntime()
        rt._cached_listen = "127.0.0.1:9527"
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = True
        rt._rt._thread = mock_thread
        mock_stop = MagicMock()
        mock_stop.is_set.return_value = False
        rt._rt._stop_event = mock_stop
        rt._rt._state = "RUNNING"  # 状态机口径
        with patch.object(rt, "start", return_value=True):
            rt.reload()
        self.assertEqual(rt._cached_listen, "")


class TestConfigPathOverride(unittest.TestCase):
    def test_start_accepts_custom_config_path(self):
        rt = SuanpanRuntime()
        custom = os.path.expanduser("~/custom-suanpan.yaml")
        with patch("builtins.__import__", side_effect=ImportError("fastapi")):
            rt.start(config_path=custom)
        self.assertEqual(rt.config_path, custom)

    def test_start_rejects_config_path_outside_home(self):
        rt = SuanpanRuntime()
        ok = rt.start(config_path="/etc/suanpan.yaml")
        self.assertFalse(ok)
        self.assertIn("主目录", rt.error)


class TestStartConfigCreateError(unittest.TestCase):
    def test_ensure_config_oserror_returns_false(self):
        rt = SuanpanRuntime()
        with patch.object(rt, "_ensure_config", side_effect=OSError("disk full")):
            ok = rt.start()
        self.assertFalse(ok)
        self.assertIn("无法创建配置文件", rt.error)


def _capture_factory(rt, listen, invoke=False):
    """Run start() with mocked deps, capturing the coroutine factory.

    invoke=True 时在 patch 上下文内调用 factory 并返回其结果（
    uvicorn.Config/Server 的 mock 只在上下文内有效——上下文外调用
    factory 会落真实构造）。
    """
    captured = {}

    def capture(factory):
        captured["f"] = factory
        return True

    mock_config = MagicMock()
    # Code under test composes the listen string via listen_address(); drive
    # the validation paths by having the mock return the address directly.
    mock_config.listen_address.return_value = listen
    import uvicorn
    with patch.object(rt._rt, "start", side_effect=capture), \
         patch("suanpan.config.load_config", return_value=mock_config), \
         patch("suanpan.main.create_app", return_value=MagicMock()), \
         patch.object(uvicorn, "Config") as mock_config_cls, \
         patch.object(uvicorn, "Server") as mock_server_cls, \
         patch.object(rt, "_ensure_config"):
        rt.start()
        if invoke:
            captured["result"] = captured["f"](MagicMock())
    if invoke:
        return captured["result"], mock_config_cls, mock_server_cls
    return captured["f"], mock_server_cls, mock_config_cls


class TestFactoryListenValidation(unittest.TestCase):
    def test_factory_rejects_invalid_listen_format(self):
        rt = SuanpanRuntime()
        factory, _, _ = _capture_factory(rt, "1.2.3.4:not-a-port")
        with self.assertRaisesRegex(ValueError, "invalid listen port"):
            factory(MagicMock())

    def test_factory_rejects_non_loopback_listen(self):
        rt = SuanpanRuntime()
        factory, _, _ = _capture_factory(rt, "0.0.0.0:9527")
        with self.assertRaisesRegex(ValueError, "loopback"):
            factory(MagicMock())

    def test_factory_builds_uvicorn_server_for_loopback(self):
        rt = SuanpanRuntime()
        factory, mock_server_cls, _ = _capture_factory(rt, "127.0.0.1:9527")
        mock_server = MagicMock()
        mock_server_cls.return_value = mock_server
        awaitable, stop_fn = factory(MagicMock())
        self.assertIsNotNone(awaitable)
        stop_fn()
        self.assertTrue(mock_server.should_exit)


class TestBindHostOverride(unittest.TestCase):
    """bind_host 构造参数（Docker 适配 seam）：设置后直接用该地址绑
    uvicorn 并跳过回环守卫；缺省保持 macOS 行为（config 地址 + 强制回环）。"""

    def _config_kwargs(self, rt, listen):
        """跑 start() 并在 patch 上下文内调 factory，取 uvicorn.Config  kwargs。"""
        _result, mock_config_cls, _srv = _capture_factory(rt, listen,
                                                          invoke=True)
        return mock_config_cls.call_args[1]

    def test_override_binds_wildcard(self):
        rt = SuanpanRuntime(bind_host="0.0.0.0")
        self.assertEqual(
            self._config_kwargs(rt, "127.0.0.1:9527")["host"], "0.0.0.0")

    def test_override_skips_loopback_guard(self):
        # 即便 config 写了非回环地址也不报——守卫只属于 macOS 形态
        rt = SuanpanRuntime(bind_host="0.0.0.0")
        self.assertEqual(
            self._config_kwargs(rt, "1.2.3.4:9527")["host"], "0.0.0.0")

    def test_default_still_guards_loopback(self):
        rt = SuanpanRuntime()
        factory, _, _ = _capture_factory(rt, "1.2.3.4:9527")
        with self.assertRaisesRegex(ValueError, "Invalid Suanpan listen"):
            factory(MagicMock())

    def test_override_keeps_config_port(self):
        rt = SuanpanRuntime(bind_host="0.0.0.0")
        self.assertEqual(
            self._config_kwargs(rt, "127.0.0.1:19527")["port"], 19527)


if __name__ == "__main__":
    unittest.main()


class TestFactoryConfigErrorShaping(unittest.TestCase):
    """#47 T4c：配置装载失败必须塑形为可定位中文（文件路径 + 字段路径）
    ——菜单栏 [:40] 截断后仍可读可行动，而非 pydantic 英文堆栈。"""

    def test_bad_config_error_names_file_and_field(self):
        import pathlib
        import tempfile
        import uvicorn
        rt = SuanpanRuntime()
        with tempfile.TemporaryDirectory() as d:
            p = str(pathlib.Path(d) / "suanpan.yaml")
            pathlib.Path(p).write_text("providers: {}\nrules: not-a-list\n")
            rt._config_path = p
            captured = {}

            def capture(factory):
                captured["f"] = factory
                return True

            with patch.object(rt._rt, "start", side_effect=capture), \
                 patch("suanpan.main.create_app", return_value=MagicMock()), \
                 patch.object(uvicorn, "Config"), \
                 patch.object(uvicorn, "Server"), \
                 patch.object(rt, "_ensure_config"):
                rt.start()
            with self.assertRaises(ValueError) as ctx:
                captured["f"](MagicMock())
            msg = str(ctx.exception)
            self.assertIn("suanpan.yaml", msg, msg)
            self.assertIn("rules", msg, msg)
            self.assertIn("配置", msg, msg)

