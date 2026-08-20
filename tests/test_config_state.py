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
        # 故障注入 A：SP 安装失败但回滚成功——不暴露部分新状态
        calls = {"n": 0}
        real_replace = os.replace
        def flaky_replace(src, dst, *a, **kw):
            calls["n"] += 1
            # journal(1) → mp(2) → sp(3)；SP 安装时失败
            if calls["n"] == 3:
                raise OSError("simulated sp failure")
            return real_replace(src, dst, *a, **kw)
        import mpconf.config_state as cs
        with mock.patch.object(cs.os, "replace", side_effect=flaky_replace):
            result = store.commit(plan)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "sp")
        loaded = store.load()
        self.assertEqual(loaded.mp_state, "missing",
                         "回滚成功：MP 不残留新状态")
        self.assertEqual(loaded.sp_state, "missing")
        self.assertFalse(os.path.exists(store.journal_path),
                         "回滚成功后 journal 清除")

        # 故障注入 B：真崩溃——安装与回滚都失败，journal 保留待启动恢复
        # 先成功提交一轮建立旧文件（旧内容非空，回滚才有会失败的写）
        pre = store.prepare(mp={"http_listen_port": 1000},
                            sp={"listen_port": 1000})
        self.assertTrue(store.commit(pre).ok)
        plan_b = store.prepare(mp={"http_listen_port": 8889},
                               sp={"listen_port": 9528})
        calls["n"] = 99  # 之后所有 replace 都失败（进程崩溃等价）
        def crash_replace(src, dst, *a, **kw):
            # journal 落盘放行；此后安装与回滚全崩（进程崩溃等价）
            if str(src).endswith(".txn.json.tmp"):
                return real_replace(src, dst, *a, **kw)
            raise OSError("simulated crash, rollback also dead")
        with mock.patch.object(cs.os, "replace", side_effect=crash_replace):
            result_b = store.commit(plan_b)
        self.assertFalse(result_b.ok)
        self.assertTrue(os.path.exists(store.journal_path),
                        "回滚也失败时 journal 必须保留")
        # 启动恢复：journal 重放收敛到一致
        recovered = store.recover()
        self.assertTrue(recovered)
        loaded = store.load()
        self.assertEqual(loaded.mp_data.get("http_listen_port"), 8889)
        self.assertEqual(loaded.sp_data.get("listen_port"), 9528)
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


class TestKeychainTransaction(unittest.TestCase):
    """验收：MP/SP/Keychain 任何可预期失败不暴露部分新状态。"""

    def _store(self, keychain):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return ConfigStateStore(
            mp_path=str(Path(d.name) / "magic-proxy.json"),
            sp_path=str(Path(d.name) / "suanpan.yaml"),
            keychain=keychain)

    def test_password_scheduled_and_written_after_files(self):
        ops = []
        class FakeKC:
            def set_password(self, tunnel, pw):
                ops.append(("set", tunnel["name"], pw))
                return True
            def delete_password(self, tunnel):
                ops.append(("del", tunnel["name"]))
                return True
        store = self._store(FakeKC())
        plan = store.prepare(mp={"tunnels": [
            {"name": "t1", "ssh_host": "h", "auth_type": "password",
             "password": "sekrit"}]})
        self.assertTrue(plan.ok)
        self.assertEqual(plan.mp_candidate["tunnels"][0].get("password"),
                         None, "密码在候选里必须剥离，只进 keychain 计划")
        result = store.commit(plan)
        self.assertTrue(result.ok, result.errors)
        self.assertEqual(ops, [("set", "t1", "sekrit")])
        loaded = store.load()
        self.assertNotIn("password", loaded.mp_data["tunnels"][0])

    def test_auth_switch_away_from_password_schedules_delete(self):
        ops = []
        class FakeKC:
            def set_password(self, t, p):
                ops.append(("set", t["name"]))
                return True
            def delete_password(self, t):
                ops.append(("del", t["name"]))
                return True
        store = self._store(FakeKC())
        plan = store.prepare(mp={"tunnels": [
            {"name": "t1", "ssh_host": "h", "auth_type": "key"}]})
        self.assertTrue(plan.ok)
        result = store.commit(plan)
        self.assertTrue(result.ok)
        self.assertEqual(ops, [("del", "t1")])

    def test_keychain_failure_reports_stage_without_secret(self):
        class FailKC:
            def set_password(self, tunnel, pw):
                return False
            def delete_password(self, tunnel):
                return True
        store = self._store(FailKC())
        plan = store.prepare(mp={"tunnels": [
            {"name": "t1", "ssh_host": "h", "auth_type": "password",
             "password": "sekrit"}]})
        result = store.commit(plan)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "keychain")
        self.assertTrue(all("sekrit" not in e for e in result.errors),
                        "错误不得泄露 secret")
        # 文件已回滚：不暴露「接口失败但部分新状态已生效」
        loaded = store.load()
        self.assertEqual(loaded.mp_state, "missing")


if __name__ == "__main__":
    unittest.main()


class TestBackupProtectionAndCreation(unittest.TestCase):
    """验收：invalid 主文件不覆盖最后已知良好的 .bak；首创建权限。"""

    def _store(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return ConfigStateStore(
            mp_path=str(Path(d.name) / "magic-proxy.json"),
            sp_path=str(Path(d.name) / "suanpan.yaml"))

    def test_invalid_main_file_does_not_clobber_good_backup(self):
        import stat
        store = self._store()
        # 先正常提交一轮（建立良好状态），此时 .bak 尚不存在
        plan = store.prepare(sp={"listen_port": 9527})
        self.assertTrue(store.commit(plan).ok)
        bak = store.sp_path + ".bak"
        # 手工放一个「最后已知良好」备份（模拟历史备份存在）
        Path(bak).write_text("listen_port: 9000\n")
        # 主文件损坏后再提交新配置：.bak 不得被损坏内容覆盖
        Path(store.sp_path).write_text("{corrupt: [")
        plan2 = store.prepare(sp={"listen_port": 9528})
        self.assertTrue(store.commit(plan2).ok)
        self.assertEqual(Path(bak).read_text(), "listen_port: 9000\n",
                         "损坏主文件的存在不得让 .bak 被覆盖")

    def test_first_creation_permissions(self):
        import stat
        store = self._store()
        plan = store.prepare(mp={"http_listen_port": 8888},
                             sp={"listen_port": 9527})
        self.assertTrue(store.commit(plan).ok)
        self.assertEqual(stat.S_IMODE(os.stat(store.mp_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(store.sp_path).st_mode), 0o600)
        d = os.path.dirname(store.sp_path)
        # 目录若由本 store 首建须 0700；已有目录（如 tmpdir 0700）不降权
        self.assertGreaterEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)


if __name__ == "__main__":
    unittest.main()


class TestFaultInjectionCompletions(unittest.TestCase):
    """验收⑧补齐：journal 写失败 / mp 安装失败 / journal 损坏恢复。"""

    def _store(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return ConfigStateStore(
            mp_path=str(Path(d.name) / "magic-proxy.json"),
            sp_path=str(Path(d.name) / "suanpan.yaml"))

    def test_journal_write_failure_aborts_cleanly(self):
        from unittest import mock
        import mpconf.config_state as cs
        store = self._store()
        plan = store.prepare(mp={"http_listen_port": 8888})
        with mock.patch.object(cs.os, "replace",
                               side_effect=OSError("journal dead")):
            result = store.commit(plan)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "journal")
        loaded = store.load()
        self.assertEqual(loaded.mp_state, "missing", "journal 失败零落盘")

    def test_mp_install_failure_rolls_back_sp_untouched(self):
        from unittest import mock
        import mpconf.config_state as cs
        store = self._store()
        # 先有旧文件
        self.assertTrue(store.commit(store.prepare(
            mp={"http_listen_port": 1000}, sp={"listen_port": 1000})).ok)
        plan = store.prepare(mp={"http_listen_port": 8888})
        calls = {"n": 0}
        real_replace = os.replace
        def fail_mp_install(src, dst, *a, **kw):
            calls["n"] += 1
            # journal(1) 放行；mp 安装(2) 失败
            if calls["n"] == 2:
                raise OSError("mp install dead")
            return real_replace(src, dst, *a, **kw)
        with mock.patch.object(cs.os, "replace", side_effect=fail_mp_install):
            result = store.commit(plan)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "mp")
        loaded = store.load()
        self.assertEqual(loaded.mp_data.get("http_listen_port"), 1000,
                         "MP 回滚到旧内容")
        self.assertEqual(loaded.sp_data.get("listen_port"), 1000,
                         "SP 未被触碰")

    def test_corrupt_journal_recovered_and_cleared(self):
        store = self._store()
        Path(store.journal_path).write_text("{corrupt")
        self.assertTrue(store.recover())
        self.assertFalse(os.path.exists(store.journal_path),
                         "损坏 journal 必须清除，不得永久残留")
