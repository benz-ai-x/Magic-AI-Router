"""配置存储（ConfigStore）— canonical paths + safe-write pipeline.

Owns:
- PATHS: the single authoritative registry of config file locations.
  Read at call time everywhere, so tests redirect ONE point (patch.dict)
  and can never land on the user's real config files.
- atomic_write(): the shared safe-write primitive (mkstemp + chmod +
  os.replace, optional pre-write .bak backup) used by all state writers.

Owns NOT: config content semantics. merge/migrate (Magic Proxy side) and
pydantic validation / api_key_set 契约 (Suanpan side) stay in config.py and
suanpan/config.py respectively; sp_* 编排桥在 services/sp_config.py。

原属 mpconf——被 tunnel/sysctl/services 四域共用作持久化原语后迁入
叶子层（P1；分层契约见 tests/test_arch_imports.py）。
"""
import logging
import os
import shutil
import tempfile

logger = logging.getLogger("magic-proxy.config_store")

DEFAULT_PATHS = {
    "mp": os.path.expanduser("~/.magic-proxy.json"),
    "sp": os.path.expanduser("~/.suanpan.yaml"),
    "claude_settings": os.path.expanduser("~/.claude/settings.json"),
}

# Live registry — tests redirect this (patch.dict), production never does.
PATHS = dict(DEFAULT_PATHS)


def get_path(name):
    """Canonical path for config file `name` ("mp" | "sp"), read at call time."""
    return PATHS[name]


def atomic_write(path, text, *, mode=0o600, backup=False):
    """Write `text` to `path` atomically (mkstemp + chmod + os.replace).

    With backup=True, the existing file is first copied to path+'.bak' —
    a bad save must never destroy the previous config unrecoverably.
    Returns True on success, False on OSError (logged).
    """
    d = os.path.dirname(path) or "."
    try:
        os.makedirs(d, exist_ok=True)
        if backup and os.path.exists(path):
            shutil.copy2(path, path + ".bak")
        fd, tmp = tempfile.mkstemp(
            dir=d, prefix="." + os.path.basename(path) + ".", suffix=".tmp")
    except OSError:
        logger.exception("atomic_write: cannot stage temp file for %s", path)
        return False
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        return True
    except OSError:
        logger.exception("atomic_write: failed writing %s", path)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
