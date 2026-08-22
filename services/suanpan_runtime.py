"""Thread-owned runtime for the Suanpan AI router gateway.

Thin adapter over AsyncRuntime: lazy-imports FastAPI/uvicorn, builds the uvicorn
server coroutine, and delegates lifecycle to AsyncRuntime.

Suanpan imports (``suanpan.config`` / ``suanpan.main``) are deferred to ``start()``
and ``listen_address()`` so that the host app launches even when FastAPI, uvicorn,
or other gateway dependencies are absent.  The gateway simply reports unavailable
until the user installs them (``pip3 install -r requirements-dev.txt``).
"""
import logging
import os

from tunnel.async_runtime import AsyncRuntime
from mpconf.config_store import DEFAULT_PATHS, get_path

logger = logging.getLogger("magic-proxy.suanpan")

# Compatibility alias for the default — the live value is
# config_store.PATHS["sp"], read at call time in __init__.
DEFAULT_CONFIG_PATH = DEFAULT_PATHS["sp"]
DEFAULT_LISTEN = "127.0.0.1:9527"

# Minimal default config written on first run so the config UI (port 9528
# webview) is immediately usable.  Users then add providers via the settings
# webview or by editing this file directly.
_DEFAULT_CONFIG_YAML = """\
listen_port: 9527
request_timeout_s: 3600
body_limit_mb: 50
providers: {}
router: {}
rules: []
"""


class SuanpanRuntime:
    """Manage the Suanpan gateway lifecycle via AsyncRuntime.

    bind_host 是 Docker 适配的 seam：缺省 None = macOS 形态（监听地址
    取自配置 + 强制回环守卫）；容器形态传 "0.0.0.0"（信任边界=宿主机
    端口映射），回环守卫不适用。
    """

    def __init__(self, bind_host=None):
        self._rt = AsyncRuntime("SuanpanGateway", stop_timeout=3)
        self._config_path = get_path("sp")
        self._import_error = ""
        self._cached_listen = ""
        self._bind_host = bind_host

    @property
    def running(self):
        return self._rt.running

    @property
    def error(self):
        return self._import_error or self._rt.error

    @property
    def config_path(self):
        return self._config_path

    def _ensure_config(self):
        """Create a minimal default config if none exists yet.

        首创建经 ConfigStateStore——与保存同一原子写/0600 权限路径。
        """
        path = self._config_path
        if not os.path.exists(path):
            import yaml as _yaml
            from mpconf.config_state import CommitPlan, ConfigStateStore
            store = ConfigStateStore(sp_path=path)
            # 内置默认是可信静态内容：直构 plan 落 commit（原子写+0600），
            # 不走业务校验（默认里的占位 base_url 会被 prepare 拒绝）
            plan = CommitPlan(True, [], None,
                              _yaml.safe_load(_DEFAULT_CONFIG_YAML))
            if store.commit(plan).ok:
                logger.info("Created default Suanpan config at %s", path)

    def start(self, config_path=None):
        if config_path:
            home = os.path.expanduser("~") + os.sep
            if not os.path.abspath(config_path).startswith(home):
                self._import_error = "config_path 必须在用户主目录下"
                return False
            self._config_path = config_path
        self.stop()
        self._cached_listen = ""
        self._import_error = ""

        # Lazy import: FastAPI/uvicorn/etc. are optional — app.py must launch
        # even when they're absent.
        try:
            from suanpan.config import load_config
            from suanpan.main import create_app
            import uvicorn
        except ImportError as exc:
            self._import_error = (
                f"缺少依赖 {exc.name!r}，请安装：pip3 install -r requirements-dev.txt"
            )
            logger.warning("Suanpan dependencies missing: %s", exc)
            return False

        try:
            self._ensure_config()
        except OSError as exc:
            self._import_error = f"无法创建配置文件：{exc}"
            return False

        config_path_str = self._config_path

        def factory(loop):
            from mpconf import netloc
            config = load_config(config_path_str)
            app = create_app(config, config_path=config_path_str)
            try:
                host, port = netloc.parse_listen(
                    config.listen_address(), default_port=9527)
                if self._bind_host is not None:
                    host = self._bind_host
                else:
                    netloc.require_loopback(host)
            except ValueError as exc:
                raise ValueError(f"Invalid Suanpan listen address: {exc}") from None
            server_config = uvicorn.Config(
                app,
                host=host,
                port=port,
                log_level="warning",
                loop="asyncio",
            )
            server = uvicorn.Server(server_config)

            def stop_fn():
                server.should_exit = True

            return server.serve(), stop_fn

        return self._rt.start(factory)

    def stop(self, timeout=3):
        return self._rt.stop(timeout)

    def reload(self):
        """Hot-reload config: stop and restart if running; no-op if stopped."""
        if self._rt.running:
            self._cached_listen = ""
            return self.start()
        return True

    def listen_address(self):
        """Return the gateway's listen address (cached after first read)."""
        if self._cached_listen:
            return self._cached_listen
        try:
            from suanpan.config import load_config
            config = load_config(self._config_path)
            self._cached_listen = config.listen_address()
            return self._cached_listen
        except Exception:
            return DEFAULT_LISTEN
