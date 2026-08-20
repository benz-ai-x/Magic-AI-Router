"""ConfigStateStore（issue #6）：配置持久化的唯一事务边界.

Seam S1 —— load/prepare/commit 三段式：候选配置在首次 mutation 前完成
全部校验；两文件 + Keychain 的提交经 journal 可恢复；invalid 主文件永不
覆盖最后已知良好的 .bak；on_sp_saved 只在完整提交后触发。
"""
import json
import os
import tempfile
import unittest
from unittest import mock  # noqa: F401
from pathlib import Path

from mpconf import config_state
from mpconf.config_state import ConfigStateStore, LoadResult


class TestLoadStates(unittest.TestCase):
    """验收：load 区分 missing / valid / invalid / io_error。"""

    def test_missing_when_never_written(self):
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStateStore(
                mp_path=str(Path(d) / "magic-proxy.json"),
                sp_path=str(Path(d) / "suanpan.yaml"))
            result = store.load()
            self.assertEqual(result.mp_state, "missing")
            self.assertEqual(result.sp_state, "missing")

    def test_valid_after_wellformed_files(self):
        with tempfile.TemporaryDirectory() as d:
            mp = Path(d) / "magic-proxy.json"
            sp = Path(d) / "suanpan.yaml"
            mp.write_text('{"tunnels": []}')
            sp.write_text("listen_port: 9527\n")
            store = ConfigStateStore(mp_path=str(mp), sp_path=str(sp))
            result = store.load()
            self.assertEqual(result.mp_state, "valid")
            self.assertEqual(result.sp_state, "valid")
            self.assertEqual(result.mp_data, {"tunnels": []})

    def test_invalid_json_reported_not_folded_to_empty(self):
        with tempfile.TemporaryDirectory() as d:
            mp = Path(d) / "magic-proxy.json"
            mp.write_text("{corrupt json")
            store = ConfigStateStore(mp_path=str(mp), sp_path=str(Path(d) / "s.yaml"))
            result = store.load()
            self.assertEqual(result.mp_state, "invalid")

    def test_io_error_distinguished(self):
        with tempfile.TemporaryDirectory() as d:
            mp = Path(d) / "magic-proxy.json"
            mp.write_text("{}")
            mp.chmod(0o000)
            store = ConfigStateStore(mp_path=str(mp), sp_path=str(Path(d) / "s.yaml"))
            try:
                result = store.load()
            finally:
                mp.chmod(0o644)
            self.assertEqual(result.mp_state, "io_error")


class TestPrepareValidation(unittest.TestCase):
    """验收：所有候选配置在首次 mutation 前完成 schema 与跨引用校验。"""

    def _store(self):
        with tempfile.TemporaryDirectory() as d:
            yield ConfigStateStore(
                mp_path=str(Path(d) / "magic-proxy.json"),
                sp_path=str(Path(d) / "suanpan.yaml"))

    def _prepare(self, mp=None, sp=None):
        with tempfile.TemporaryDirectory() as d:
            store = ConfigStateStore(
                mp_path=str(Path(d) / "magic-proxy.json"),
                sp_path=str(Path(d) / "suanpan.yaml"))
            return store.prepare(mp or {}, sp or {})

    def test_port_out_of_range_rejected(self):
        plan = self._prepare(mp={"http_listen_port": 99999})
        self.assertFalse(plan.ok)
        self.assertTrue(any("端口" in e for e in plan.errors))

    def test_port_zero_rejected(self):
        plan = self._prepare(sp={"listen_port": 0})
        self.assertFalse(plan.ok)
        self.assertTrue(any("端口" in e for e in plan.errors))

    def test_negative_timeout_rejected(self):
        plan = self._prepare(sp={"server": {"request_timeout_s": -1}})
        self.assertFalse(plan.ok)

    def test_negative_retention_rejected(self):
        plan = self._prepare(mp={"retention_days": -3})
        self.assertFalse(plan.ok)

    def test_bad_base_url_origin_rejected(self):
        plan = self._prepare(sp={"providers": {
            "p1": {"base_url": "ftp://x", "api_key": "k"}}})
        self.assertFalse(plan.ok)
        self.assertTrue(any("base_url" in e for e in plan.errors))

    def test_rule_referencing_missing_provider_rejected(self):
        plan = self._prepare(sp={
            "providers": {"p1": {"base_url": "https://a.test", "api_key": "k",
                                  "models": ["m"]}},
            "rules": [{"match_prefix": "claude-", "route_to": "ghost/m"}]})
        self.assertFalse(plan.ok)
        self.assertTrue(any("route_to" in e or "ghost" in e for e in plan.errors))

    def test_valid_candidate_produces_plan(self):
        plan = self._prepare(mp={"http_listen_port": 8888},
                             sp={"listen_port": 9527})
        self.assertTrue(plan.ok, plan.errors)
        self.assertEqual(plan.errors, [])


if __name__ == "__main__":
    unittest.main()


class TestCommitTransaction(unittest.TestCase):
    """验收：commit 的 journal 语义与回调次序；两文件间崩溃可恢复。"""

    def _store(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return ConfigStateStore(
            mp_path=str(Path(d.name) / "magic-proxy.json"),
            sp_path=str(Path(d.name) / "suanpan.yaml"))

    def test_happy_path_commits_both_and_fires_callback_last(self):
        store = self._store()
        order = []
        plan = store.prepare(mp={"http_listen_port": 8888},
                             sp={"listen_port": 9527})
        self.assertTrue(plan.ok)
        result = store.commit(plan, on_committed=lambda: order.append("cb"))
        self.assertTrue(result.ok, result.errors)
        order.append("after")
        self.assertEqual(order, ["cb", "after"])
        loaded = store.load()
        self.assertEqual(loaded.mp_data.get("http_listen_port"), 8888)
        self.assertEqual(loaded.sp_data.get("listen_port"), 9527)
        self.assertFalse(os.path.exists(store.journal_path),
                         "完整提交后 journal 必须清除")

    def test_journal_exists_between_mp_and_sp_crash_recoverable(self):
        """模拟两文件提交之间崩溃：journal 残留 → recover() 补齐到一致。"""
        store = self._store()
        plan = store.prepare(mp={"http_listen_port": 8888},
                             sp={"listen_port": 9527})
        # 故障注入：SP 写入阶段抛错（模拟 mp 已提交、sp 未提交即崩溃）
        calls = {"n": 0}
        real_replace = os.replace
        def flaky_replace(src, dst, *a, **kw):
            calls["n"] += 1
            # replace 序：journal(1) → mp(2) → sp(3)；在 SP 安装时崩溃
            if calls["n"] == 3:
                raise OSError("simulated crash between commits")
            return real_replace(src, dst, *a, **kw)
        import mpconf.config_state as cs
        with unittest.mock.patch.object(cs.os, "replace", side_effect=flaky_replace):
            result = store.commit(plan)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "sp")
        self.assertTrue(os.path.exists(store.journal_path),
                        "跨文件窗口必须有 journal")
        # MP 已提交、SP 未提交：中间态可见
        loaded = store.load()
        self.assertEqual(loaded.mp_data.get("http_listen_port"), 8888)
        self.assertIsNone(loaded.sp_data)
        # 恢复：journal 重放补齐 SP
        recovered = store.recover()
        self.assertTrue(recovered)
        loaded = store.load()
        self.assertEqual(loaded.sp_data.get("listen_port"), 9527)
        self.assertFalse(os.path.exists(store.journal_path))

    def test_validation_failure_touches_nothing(self):
        store = self._store()
        plan = store.prepare(mp={"http_listen_port": 99999})
        result = store.commit(plan)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "validate")
        loaded = store.load()
        self.assertEqual(loaded.mp_state, "missing")
        self.assertFalse(os.path.exists(store.journal_path))


if __name__ == "__main__":
    unittest.main()
