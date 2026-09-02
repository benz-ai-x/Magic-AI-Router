"""Test-session safety net: never touch the user's real config files.

All config I/O resolves paths from config_store.PATHS at call time, so
redirecting this ONE registry makes it impossible for any test to write
the real ~/.magic-proxy.json / ~/.suanpan.yaml / ~/.claude/settings.json.
(This happened in practice: an unmocked TestWriteSp wiped the user's
Suanpan config 3x.)
"""
from unittest.mock import patch

import pytest

from shared import config_store
@pytest.fixture(autouse=True, scope="session")
def _sandbox_real_config_paths(tmp_path_factory):
    sandbox = tmp_path_factory.mktemp("real-config-sandbox")
    with patch.dict(config_store.PATHS, {
        "mp": str(sandbox / "magic-proxy.json"),
        "sp": str(sandbox / "suanpan.yaml"),
        "claude_settings": str(sandbox / "claude-settings.json"),
    }):
        yield
