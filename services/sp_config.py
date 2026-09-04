"""Suanpan 配置读取桥（services 域）——sp_* 编排自 config_store 迁入。

sp_load / sp_load_raw / sp_load_masked / suanpan_listen 的调用方全部在
services（config_server / claude_code_setup / lifecycle_runtime），且它们
lazy-import suanpan.config（网关依赖可选——缺依赖时 app 仍要能启动）。
此前住在 mpconf/config_store 迫使该文件知道 suanpan 域；拆出后
shared/config_store 回归纯持久化原语（分层见 tests/test_arch_imports.py）。
"""
import logging

from shared.defaults import DEFAULT_GATEWAY_PORT

from shared import config_store

logger = logging.getLogger("magic-proxy.sp_config")


def sp_load(path=None):
    """Load and validate the Suanpan config (raises on missing deps/file)."""
    from suanpan.config import load_config
    return load_config(path or config_store.get_path("sp"))


def sp_load_raw(path=None):
    """Raw (unmasked) Suanpan config dict; {} on any error or missing deps."""
    try:
        from suanpan.config import load_config_raw
        return load_config_raw(path or config_store.get_path("sp"))
    except ImportError:
        return {}


def sp_load_masked(path=None):
    """Suanpan config dict with API keys masked; {} on missing deps."""
    try:
        from suanpan.config import load_config_masked
        return load_config_masked(path or config_store.get_path("sp"))
    except ImportError:
        return {}


def suanpan_listen(path=None):
    """Validated gateway listen address; schema default on any failure."""
    try:
        from suanpan.config import AppConfig, load_config
    except ImportError:
        return f"127.0.0.1:{DEFAULT_GATEWAY_PORT}"  # deps missing — last-resort default
    try:
        return load_config(path or config_store.get_path("sp")).listen_address()
    except Exception:
        return AppConfig(providers={}).listen_address()
