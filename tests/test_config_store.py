"""Tests for config_store.py — 配置存储（路径注册表 + 原子写原语 + sp 编排）。

Seams under test:
- PATHS registry: read at call time, single redirect point for tests
- atomic_write: mkstemp + chmod + os.replace, optional pre-write backup
- sp_load/sp_load_raw/sp_load_masked: orchestration over suanpan.config
- （#46 后 sp 写径唯一归宿 ConfigStateStore——save 测试随 sp_save 删除）
- suanpan_listen: validated listen with schema-default fallback chain
"""
from services import sp_config
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

from shared import config_store
class TestPathsRegistry(unittest.TestCase):
    def test_defaults_point_at_home(self):
        self.assertEqual(config_store.DEFAULT_PATHS["mp"],
                         os.path.expanduser("~/.magic-proxy.json"))
        self.assertEqual(config_store.DEFAULT_PATHS["sp"],
                         os.path.expanduser("~/.suanpan.yaml"))

    def test_get_path_reads_live_registry(self):
        # (session conftest already sandboxes PATHS — this verifies call-time read)
        with patch.dict(config_store.PATHS, {"mp": "/tmp/redirected.json"}):
            self.assertEqual(config_store.get_path("mp"), "/tmp/redirected.json")


class TestAtomicWrite(unittest.TestCase):
    def test_writes_content(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.json")
            self.assertTrue(config_store.atomic_write(p, '{"a": 1}'))
            with open(p) as f:
                self.assertEqual(f.read(), '{"a": 1}')

    def test_applies_0600_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "secret.yaml")
            config_store.atomic_write(p, "k: v")
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_creates_parent_dirs(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sub", "dir", "c.yaml")
            self.assertTrue(config_store.atomic_write(p, "x"))
            self.assertTrue(os.path.exists(p))

    def test_backup_copies_previous_content(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.yaml")
            config_store.atomic_write(p, "v1")
            config_store.atomic_write(p, "v2", backup=True)
            with open(p + ".bak") as f:
                self.assertEqual(f.read(), "v1")
            with open(p) as f:
                self.assertEqual(f.read(), "v2")

    def test_no_backup_by_default(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "c.yaml")
            config_store.atomic_write(p, "v1")
            config_store.atomic_write(p, "v2")
            self.assertFalse(os.path.exists(p + ".bak"))

    def test_failure_returns_false(self):
        with tempfile.TemporaryDirectory() as d:
            blocker = os.path.join(d, "blocker")
            open(blocker, "w").close()  # regular file where a dir is needed
            p = os.path.join(blocker, "c.yaml")
            self.assertFalse(config_store.atomic_write(p, "x"))


class TestSpOrchestration(unittest.TestCase):
    """sp 写径经 ConfigStateStore（#46 后 sp_save 删除）；此处钉
    store 落盘 → sp_load/sp_load_raw/sp_load_masked 的读取编排。"""

    def _commit(self, path, sp):
        from mpconf.config_state import ConfigStateStore
        store = ConfigStateStore(sp_path=path)
        plan = store.prepare(sp=sp)
        assert plan.ok, plan.errors
        result = store.commit(plan)
        assert result.ok, result.errors

    def test_commit_and_load_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sp.yaml")
            self._commit(p, {"providers": {"A": {
                "base_url": "http://a.example"}}})
            cfg = sp_config.sp_load(path=p)
            self.assertIn("A", cfg.providers)
            # 0600 — the YAML holds API keys
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_prepare_rejects_ghost_route_writes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sp.yaml")
            from mpconf.config_state import ConfigStateStore
            plan = ConfigStateStore(sp_path=p).prepare(
                sp={"providers": {}, "router": {"default": "ghost/m"}})
            self.assertFalse(plan.ok)
            self.assertTrue(any("ghost" in e for e in plan.errors))
            self.assertFalse(os.path.exists(p))

    def test_load_raw_and_masked(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sp.yaml")
            self._commit(p, {"providers": {"A": {
                "base_url": "http://a.example", "api_key": "sk-1234567890"}}})
            raw = sp_config.sp_load_raw(path=p)
            self.assertEqual(raw["providers"]["A"]["api_key"], "sk-1234567890")
            masked = sp_config.sp_load_masked(path=p)
            self.assertIsNone(masked["providers"]["A"]["api_key"])
            self.assertTrue(masked["providers"]["A"]["api_key_set"])


class TestSuanpanListen(unittest.TestCase):
    def test_reads_validated_listen(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sp.yaml")
            # 直接落盘（读侧兼容旧 listen 字符串才是本测的对象）
            with open(p, "w") as f:
                f.write('listen: "127.0.0.1:9999"\nproviders: {}\n')
            self.assertEqual(sp_config.suanpan_listen(path=p), "127.0.0.1:9999")

    def test_missing_file_falls_back_to_schema_default(self):
        self.assertEqual(sp_config.suanpan_listen(path="/nonexistent/sp.yaml"),
                         "127.0.0.1:9527")

    def test_corrupt_file_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "sp.yaml")
            with open(p, "w") as f:
                f.write("listen: [not, a, string]")
            self.assertEqual(sp_config.suanpan_listen(path=p), "127.0.0.1:9527")

    def test_missing_suanpan_deps_falls_back(self):
        with patch.dict(sys.modules, {"suanpan.config": None}):
            self.assertEqual(sp_config.suanpan_listen(), "127.0.0.1:9527")


class TestSpImportErrorTolerance(unittest.TestCase):
    """suanpan deps missing (no pydantic) — the app must still function."""

    def test_load_raw_returns_empty(self):
        with patch.dict(sys.modules, {"suanpan.config": None}):
            self.assertEqual(sp_config.sp_load_raw(), {})

    def test_load_masked_returns_empty(self):
        with patch.dict(sys.modules, {"suanpan.config": None}):
            self.assertEqual(sp_config.sp_load_masked(), {})

    # sp_save 的 ImportError 容错随函数删除（#46）：写径唯一归宿
    # ConfigStateStore，其 pydantic 缺席行为在 test_config_state 钉住


if __name__ == "__main__":
    unittest.main()
