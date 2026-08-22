"""配置存储（ConfigStore）— canonical paths + safe-write pipeline.

Owns:
- PATHS: the single authoritative registry of config file locations.
  Read at call time everywhere, so tests redirect ONE point (patch.dict)
  and can never land on the user's real config files.
- atomic_write(): the shared safe-write primitive (mkstemp + chmod +
  os.replace, optional pre-write .bak backup) used by both config stacks.
- sp_* orchestration: entry points over suanpan.config (lazy-imported —
  the app must still launch when pydantic/FastAPI deps are absent).
- suanpan_listen(): validated gateway listen address with fallback chain.

Owns NOT: config content semantics. merge/migrate (Magic Proxy side) and
pydantic validation / api_key_set 契约 (Suanpan side) stay in config.py and
suanpan/config.py respectively.
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


# ── Suanpan orchestration (lazy: suanpan deps are optional) ─────────

def sp_load(path=None):
    """Load and validate the Suanpan config (raises on missing deps/file)."""
    from suanpan.config import load_config
    return load_config(path or get_path("sp"))


def sp_load_raw(path=None):
    """Raw (unmasked) Suanpan config dict; {} on any error or missing deps."""
    try:
        from suanpan.config import load_config_raw
        return load_config_raw(path or get_path("sp"))
    except ImportError:
        return {}


def sp_load_masked(path=None):
    """Suanpan config dict with API keys masked; {} on missing deps."""
    try:
        from suanpan.config import load_config_masked
        return load_config_masked(path or get_path("sp"))
    except ImportError:
        return {}


def suanpan_listen(path=None):
    """Validated gateway listen address; schema default on any failure."""
    try:
        from suanpan.config import AppConfig, load_config
    except ImportError:
        return "127.0.0.1:9527"  # deps missing — last-resort default
    try:
        return load_config(path or get_path("sp")).listen_address()
    except Exception:
        return AppConfig(providers={}).listen_address()
