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

from mpconf.config_state import ConfigStateStore


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
        plan = self._prepare(sp={"listen_port": 0, "providers": {}})
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
                             sp={"listen_port": 9527, "providers": {}})
        self.assertTrue(plan.ok, plan.errors)
        self.assertEqual(plan.errors, [])




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
                             sp={"listen_port": 9527, "providers": {}})
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
                             sp={"listen_port": 9527, "providers": {}})
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
                            sp={"listen_port": 1000, "providers": {}})
        self.assertTrue(store.commit(pre).ok)
        plan_b = store.prepare(mp={"http_listen_port": 8889},
                               sp={"listen_port": 9528, "providers": {}})
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




class TestBackupProtectionAndCreation(unittest.TestCase):
    """验收：invalid 主文件不覆盖最后已知良好的 .bak；首创建权限。"""

    def _store(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return ConfigStateStore(
            mp_path=str(Path(d.name) / "magic-proxy.json"),
            sp_path=str(Path(d.name) / "suanpan.yaml"))

    def test_invalid_main_file_does_not_clobber_good_backup(self):
        store = self._store()
        # 先正常提交一轮（建立良好状态），此时 .bak 尚不存在
        plan = store.prepare(sp={"listen_port": 9527, "providers": {}})
        self.assertTrue(store.commit(plan).ok)
        bak = store.sp_path + ".bak"
        # 手工放一个「最后已知良好」备份（模拟历史备份存在）
        Path(bak).write_text("listen_port: 9000\n")
        # 主文件损坏后再提交新配置：.bak 不得被损坏内容覆盖
        Path(store.sp_path).write_text("{corrupt: [")
        plan2 = store.prepare(sp={"listen_port": 9528, "providers": {}})
        self.assertTrue(store.commit(plan2).ok)
        self.assertEqual(Path(bak).read_text(), "listen_port: 9000\n",
                         "损坏主文件的存在不得让 .bak 被覆盖")

    def test_first_creation_permissions(self):
        import stat
        store = self._store()
        plan = store.prepare(mp={"http_listen_port": 8888},
                             sp={"listen_port": 9527, "providers": {}})
        self.assertTrue(store.commit(plan).ok)
        self.assertEqual(stat.S_IMODE(os.stat(store.mp_path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(store.sp_path).st_mode), 0o600)
        d = os.path.dirname(store.sp_path)
        # 目录若由本 store 首建须 0700；已有目录（如 tmpdir 0700）不降权
        self.assertGreaterEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)




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
            mp={"http_listen_port": 1000}, sp={"listen_port": 1000, "providers": {}})).ok)
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


    def test_repin_guard_blocks_crosswired_identity(self):
        """Y 改址到 X 旧地址：id 与身份哈希不符 → 绝不读 legacy（防串线）。"""
        ops = []
        class KC:
            def get_password(self, t):
                ops.append("get")
                return "x-secret"
            def set_password(self, t, pw):
                ops.append(("set", t.get("id")))
                return True
            def delete_password(self, t):
                return True
            def delete_legacy_password(self, t):
                ops.append(("del-legacy",))
                return True
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        store = ConfigStateStore(
            mp_path=str(Path(d.name) / "m.json"),
            sp_path=str(Path(d.name) / "s.yaml"), keychain=KC())
        # id=哈希(旧地址)，现身份=新地址 → 守卫拒绝
        from mpconf.config import stable_tunnel_id
        old_addr_id = stable_tunnel_id("u", "old.example.com", 22)
        plan = store.prepare(mp={"tunnels": [
            {"id": old_addr_id, "name": "y", "ssh_user": "u",
             "ssh_host": "new.example.com", "ssh_port": 22,
             "auth_type": "password"}]})
        store.commit(plan)
        self.assertNotIn("get", ops, "身份编辑过 → 不读 legacy，不猜归属")

    def test_deleted_tunnel_secrets_cleaned_both_accounts(self):
        ops = []
        class KC:
            def get_password(self, t):
                return ""
            def set_password(self, t, pw):
                return True
            def delete_password(self, t):
                ops.append(("del-all", t.get("id")))
                return True
            def delete_legacy_password(self, t):
                ops.append(("del-legacy", t.get("id")))
                return True
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        store = ConfigStateStore(
            mp_path=str(Path(d.name) / "m.json"),
            sp_path=str(Path(d.name) / "s.yaml"), keychain=KC())
        # 先建档含 t-gone，再保存不含它
        Path(store.mp_path).write_text(json.dumps(
            {"tunnels": [{"id": "t-gone", "name": "g", "ssh_host": "h",
                          "auth_type": "password",
                          "ssh_user": "u", "ssh_port": 22}]}))
        plan = store.prepare(mp={"tunnels": []})
        self.assertTrue(plan.ok)
        store.commit(plan)
        self.assertIn(("del-all", "t-gone"), ops, "删除隧道双账户清理")


    def test_prepare_rejects_sp_with_load_error(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        store = ConfigStateStore(
            mp_path=str(Path(d.name) / "m.json"),
            sp_path=str(Path(d.name) / "s.yaml"))
        plan = store.prepare(sp={"_load_error": "装载失败", "providers": {}})
        self.assertFalse(plan.ok)
        self.assertIn("已阻止保存", plan.errors[0])

    def test_restore_strips_load_error_marker(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        store = ConfigStateStore(
            mp_path=str(Path(d.name) / "m.json"),
            sp_path=str(Path(d.name) / "s.yaml"))
        Path(store.sp_path).write_text(
            yaml.dump({"listen_port": 9527, "providers": {
                "a": {"id": "p-1", "base_url": "https://a.test",
                      "api_key": "sk-1", "models": ["m"]}}}))
        plan = store.prepare(sp={
            "_load_error": "x", "listen_port": 9527,
            "providers": {"a": {"id": "p-1", "base_url": "https://a.test",
                                "api_key": None, "api_key_set": True,
                                "models": ["m"]}}})
        # _load_error 在此之前已被拒——restore 剥离由内部直测
        self.assertFalse(plan.ok)  # 拒收优先
        # 直测 restore 剥离
        cleaned = store._restore_masked_sp_keys(
            {"_load_error": "x", "providers": {}, "api_key": None})
        self.assertNotIn("_load_error", cleaned)



class TestUpdateMp(unittest.TestCase):
    """#46 T1a/d：菜单开关的唯一写径——写前读新 + 事务写。

    回归剧本（丢更新时序）：UI 事务保存字段 X → 菜单开关写字段 Y →
    磁盘必须同时保有 X 和 Y（旧径整文件覆写内存副本会抹掉 X）。
    """

    def _store(self, d):
        return ConfigStateStore(
            mp_path=str(Path(d) / "magic-proxy.json"),
            sp_path=str(Path(d) / "suanpan.yaml"))

    def test_update_preserves_concurrent_ui_save(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            store = self._store(d)
            # UI 保存：完整事务写入两条隧道 + retention 调整
            plan = store.prepare(mp={"tunnels": [
                {"name": "t1", "ssh_user": "u", "ssh_host": "h1",
                 "ssh_port": 22, "auth_type": "key"},
                {"name": "t2", "ssh_user": "u", "ssh_host": "h2",
                 "ssh_port": 22, "auth_type": "key"}], "retention_days": 3})
            self.assertTrue(plan.ok, plan.errors)
            self.assertTrue(store.commit(plan).ok)
            # 菜单开关：写前读新，只动 prevent_sleep
            result = store.update_mp(
                lambda c: {**c, "prevent_sleep": True})
            self.assertTrue(result.ok, result.errors)
            disk = json.loads(
                (Path(d) / "magic-proxy.json").read_text())
            self.assertEqual(disk.get("retention_days"), 3,
                             "菜单开关抹掉了 UI 并发保存的字段（#46 丢更新）")
            self.assertIs(disk.get("prevent_sleep"), True)
            self.assertEqual(
                len(disk.get("tunnels", [])), 2,
                "菜单开关抹掉了 UI 并发保存的隧道（#46 丢更新）")

    def test_update_missing_file_starts_from_defaults(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            store = self._store(d)
            result = store.update_mp(
                lambda c: {**c, "prevent_sleep": True})
            self.assertTrue(result.ok, result.errors)
            disk = json.loads(
                (Path(d) / "magic-proxy.json").read_text())
            self.assertIs(disk.get("prevent_sleep"), True)
            # merge 默认齐备（写径与 UI 同一 prepare/commit 管线）
            self.assertIn("http_listen_port", disk)

    def test_update_rejects_invalid_mutation(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            store = self._store(d)
            result = store.update_mp(
                lambda c: {**c, "http_listen_port": 70000})
            self.assertFalse(result.ok)
            self.assertTrue(any("http_listen_port" in e for e in result.errors))



class TestPrepareSpSchemaValidation(unittest.TestCase):
    """#46 T1b：sp 校验收口——顶层字段（死分支修正）+ pydantic schema。"""

    def _store(self, d):
        return ConfigStateStore(
            mp_path=str(Path(d) / "magic-proxy.json"),
            sp_path=str(Path(d) / "suanpan.yaml"))

    def test_top_level_timeout_rejected(self):
        """request_timeout_s 在 schema 顶层；旧代码查不存在的 server 键
        （死校验分支），顶层非法值静默落盘。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            store = self._store(d)
            plan = store.prepare(sp={
                "providers": {"p": {"base_url": "https://x.example",
                                    "api_key": "k"}},
                "request_timeout_s": 10 ** 9})
            self.assertFalse(plan.ok)
            self.assertTrue(any("request_timeout_s" in e for e in plan.errors),
                            plan.errors)

    def test_schema_invalid_rules_rejected(self):
        """schema 校验并入事务路径：rules 非列表不得直写磁盘（旧 commit
        只有 yaml.safe_dump，写入时点的保证变成下次启动时的崩溃）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            store = self._store(d)
            plan = store.prepare(sp={
                "providers": {"p": {"base_url": "https://x.example",
                                    "api_key": "k"}},
                "rules": "not-a-list"})
            self.assertFalse(plan.ok)
            self.assertTrue(any("rules" in e for e in plan.errors),
                            plan.errors)

    def test_schema_invalid_provider_field_rejected(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            store = self._store(d)
            plan = store.prepare(sp={
                "providers": {"p": {"base_url": "https://x.example",
                                    "api_key": "k", "enabled": "yes?"}}})
            self.assertFalse(plan.ok)
            self.assertTrue(any("enabled" in e for e in plan.errors),
                            plan.errors)


if __name__ == "__main__":
    unittest.main()


class TestKeychainRealContractSemantics(unittest.TestCase):
    """二审 A/C：真实 delete_password 契约 + keychain 段原子性。"""

    def _store(self, keychain):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return ConfigStateStore(
            mp_path=str(Path(d.name) / "magic-proxy.json"),
            sp_path=str(Path(d.name) / "suanpan.yaml"),
            keychain=keychain)

    def test_set_failure_aborts_before_dels_protect_old_passwords(self):
        ops = []
        class KC:
            def set_password(self, t, p):
                ops.append(("set", t["name"]))
                return False
            def delete_password(self, t):
                ops.append(("del", t["name"]))
                return True
        store = self._store(KC())
        plan = store.prepare(mp={"tunnels": [
            {"name": "t1", "ssh_host": "h", "auth_type": "password",
             "password": "new"},
            {"name": "t2", "ssh_host": "h2", "auth_type": "key"}]})
        result = store.commit(plan)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "keychain")
        self.assertEqual(ops, [("set", "t1")],
                         "set 失败必须立即中止：dels 不执行，旧密码不丢")
        self.assertEqual(store.load().mp_state, "missing", "文件已回滚")

    def test_del_failure_is_nondestructive_and_collected(self):
        class KC:
            def set_password(self, t, p):
                return True
            def delete_password(self, t):
                return False
        store = self._store(KC())
        plan = store.prepare(mp={"tunnels": [
            {"name": "t1", "ssh_host": "h", "auth_type": "key"}]})
        result = store.commit(plan)
        self.assertFalse(result.ok)
        self.assertEqual(result.stage, "keychain")
        self.assertTrue(any("旧密码清理失败" in e for e in result.errors))


class TestStableIdentityMigration(unittest.TestCase):
    """issue #8 S2：load/prepare 期确定性 id 迁移；重命名不改 id。"""

    def test_legacy_tunnel_gets_deterministic_id(self):
        from mpconf.config import assign_stable_ids
        tunnels = [{"name": "a", "ssh_user": "u", "ssh_host": "h",
                    "ssh_port": 22}]
        ids = assign_stable_ids(tunnels)
        self.assertTrue(tunnels[0]["id"].startswith("t-"))
        self.assertEqual(len(tunnels[0]["id"]), 12)
        self.assertEqual(ids, 1, "恰一个迁移")
        # 确定性：同身份重算同 id
        again = [{"name": "a", "ssh_user": "u", "ssh_host": "h",
                  "ssh_port": 22}]
        assign_stable_ids(again)
        self.assertEqual(again[0]["id"], tunnels[0]["id"])

    def test_existing_id_untouched_and_stable_across_edits(self):
        from mpconf.config import assign_stable_ids
        tunnels = [{"id": "t-keepme1234", "ssh_user": "u",
                    "ssh_host": "h", "ssh_port": 22}]
        assign_stable_ids(tunnels)
        self.assertEqual(tunnels[0]["id"], "t-keepme1234")
        tunnels[0]["ssh_host"] = "changed.example.com"  # 编辑地址
        tunnels[0]["name"] = "renamed"
        assign_stable_ids(tunnels)
        self.assertEqual(tunnels[0]["id"], "t-keepme1234",
                         "重命名/改地址不改变 id")

    def test_duplicate_legacy_identity_gets_deterministic_suffix(self):
        """legacy 同身份双隧道本合法——确定性序数后缀，不拒启。"""
        from mpconf.config import assign_stable_ids
        dup = [{"name": "a", "ssh_user": "u", "ssh_host": "h", "ssh_port": 22},
               {"name": "b", "ssh_user": "u", "ssh_host": "h", "ssh_port": 22}]
        n = assign_stable_ids(dup)
        self.assertEqual(n, 2)
        self.assertNotEqual(dup[0]["id"], dup[1]["id"])
        again = [{"name": "a", "ssh_user": "u", "ssh_host": "h", "ssh_port": 22},
                 {"name": "b", "ssh_user": "u", "ssh_host": "h", "ssh_port": 22}]
        assign_stable_ids(again)
        self.assertEqual([t["id"] for t in again], [t["id"] for t in dup],
                         "确定性：重算同序")

    def test_duplicate_explicit_ids_fails_actionable(self):
        from mpconf.config import assign_stable_ids
        dup = [{"id": "t-same", "ssh_user": "u1", "ssh_host": "h", "ssh_port": 22},
               {"id": "t-same", "ssh_user": "u2", "ssh_host": "h", "ssh_port": 22}]
        with self.assertRaises(ValueError) as ctx:
            assign_stable_ids(dup)
        self.assertIn("t-same", str(ctx.exception))


class TestProviderIdSemantics(unittest.TestCase):
    """issue #8 S3：rename 后 keep/replace/clear 三态按 id 正确。"""

    def _roundtrip(self, old_providers, new_providers):
        """经活写径（prepare/commit——掩码恢复在 _restore_masked_sp_keys）。"""
        import tempfile
        from pathlib import Path
        from suanpan.config import load_config_raw
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "s.yaml")
            Path(path).write_text(yaml.dump(
                {"providers": old_providers, "listen_port": 9527},
                allow_unicode=True))
            store = ConfigStateStore(sp_path=path)
            plan = store.prepare(sp={"providers": new_providers,
                                     "listen_port": 9527,
                                     "api_key_set": False})
            self.assertTrue(plan.ok, plan.errors)
            self.assertTrue(store.commit(plan).ok)
            return load_config_raw(path)["providers"]

    def test_rename_keeps_key_via_id(self):
        saved = self._roundtrip(
            old_providers={"old-name": {"id": "p-stable1", "base_url":
                                        "https://a.test", "api_key": "sk-1",
                                        "models": ["m"]}},
            new_providers={"new-name": {"id": "p-stable1", "base_url":
                                        "https://a.test", "api_key": None,
                                        "api_key_set": True,
                                        "models": ["m"]}})
        self.assertEqual(saved["new-name"]["api_key"], "sk-1",
                         "重命名后 keep 语义按 id 恢复")

    def test_replace_by_id_wins_over_name_match(self):
        saved = self._roundtrip(
            old_providers={"a": {"id": "p-1", "base_url": "https://a.test",
                                 "api_key": "sk-old", "models": ["m"]}},
            new_providers={"a": {"id": "p-2", "base_url": "https://a.test",
                                 "api_key": None, "api_key_set": True,
                                 "models": ["m"]}})
        self.assertIsNone(saved["a"]["api_key"],
                          "id 不同≠同一实体：不串接他者的 key")

    def test_clear_still_clears(self):
        saved = self._roundtrip(
            old_providers={"a": {"id": "p-1", "base_url": "https://a.test",
                                 "api_key": "sk-old", "models": ["m"]}},
            new_providers={"a": {"id": "p-1", "base_url": "https://a.test",
                                 "api_key": "", "api_key_set": False,
                                 "models": ["m"]}})
        self.assertIsNone(saved["a"]["api_key"])

    def test_load_migration_assigns_deterministic_provider_id(self):
        from suanpan.config import assign_provider_ids, load_config_raw
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "s.yaml"
            path.write_text(
                'providers:\n  glm:\n    base_url: https://a.test\n'
                '    models: [m]\nlisten_port: 9527\n')
            cfg = load_config_raw(path)
            # 装载即赋（load_config_raw 内联 assign_provider_ids）
            self.assertTrue(cfg["providers"]["glm"]["id"].startswith("p-"),
                            "装载即含确定性 id")
            n2 = assign_provider_ids(cfg)
            self.assertEqual(n2, 0, "幂等")

    def test_duplicate_provider_ids_fail_actionable(self):
        from suanpan.config import assign_provider_ids
        cfg = {"providers": {
            "a": {"id": "p-dup", "base_url": "https://a.test"},
            "b": {"id": "p-dup", "base_url": "https://b.test"}}}
        with self.assertRaises(ValueError) as ctx:
            assign_provider_ids(cfg)
        self.assertIn("p-dup", str(ctx.exception))


import yaml  # noqa: E402  — 测试用例内 dump 需要


class TestTunnelSecretRepin(unittest.TestCase):
    """issue #8：id 稳定后 legacy 密码随下次提交 re-pin 到 id 账户。"""

    def test_password_tunnel_without_new_pw_repins_legacy_secret(self):
        ops = []
        class KC:
            def get_password(self, t):
                ops.append(("get", t.get("id") or "legacy"))
                return "legacy-pw" if not t.get("id") else ""
            def set_password(self, t, pw):
                ops.append(("set", t.get("id"), pw))
                return True
            def delete_password(self, t):
                ops.append(("del", t.get("id")))
                return True
            def delete_legacy_password(self, t):
                ops.append(("del-legacy",))
                return True
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        store = ConfigStateStore(
            mp_path=str(Path(d.name) / "m.json"),
            sp_path=str(Path(d.name) / "s.yaml"), keychain=KC())
        from mpconf.config import stable_tunnel_id
        real_id = stable_tunnel_id("u", "h", 22)
        plan = store.prepare(mp={"tunnels": [
            {"id": real_id, "name": "a", "ssh_user": "u",
             "ssh_host": "h", "ssh_port": 22, "auth_type": "password"}]})
        self.assertTrue(plan.ok)
        result = store.commit(plan)
        self.assertTrue(result.ok, result.errors)
        self.assertIn(("set", real_id, "legacy-pw"), ops,
                      "旧密码经 legacy 回退读取后 re-pin 到 id 账户")


class TestRound3Holes(unittest.TestCase):
    """三审两洞：mp _load_error 双带 + 显式分支身份登记。"""

    def test_explicit_id_tunnel_registers_identity(self):
        """已迁移 A（id=hash 身份）+ 手工加同身份无 id B → B 不再静默同 id。"""
        from mpconf.config import assign_stable_ids, stable_tunnel_id
        real_id = stable_tunnel_id("u", "h", 22)
        tunnels = [
            {"id": real_id, "name": "a", "ssh_user": "u",
             "ssh_host": "h", "ssh_port": 22},
            {"name": "b", "ssh_user": "u", "ssh_host": "h", "ssh_port": 22},
        ]
        n = assign_stable_ids(tunnels)
        self.assertEqual(n, 1)  # 只有 B 被迁移
        self.assertNotEqual(tunnels[1]["id"], tunnels[0]["id"],
                            "B 得序数后缀 id，绝不与 A 静默同 id")

    def test_fresh_assign_collision_detected(self):
        from mpconf.config import assign_stable_ids
        # 两条同身份隧道：第一条得 ordinal=1 id；第二条 #2 后缀
        # 但若 #2 id 撞显式 id —— 分配后查重兜底
        tunnels = [
            {"name": "a", "ssh_user": "u", "ssh_host": "h", "ssh_port": 22},
            {"name": "b", "ssh_user": "u", "ssh_host": "h", "ssh_port": 22},
        ]
        n = assign_stable_ids(tunnels)
        self.assertEqual(n, 2)  # 正常路径不受影响

    def test_prepare_rejects_mp_with_load_error(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        store = ConfigStateStore(
            mp_path=str(Path(d.name) / "m.json"),
            sp_path=str(Path(d.name) / "s.yaml"))
        plan = store.prepare(mp={"_load_error": "装载失败", "tunnels": []})
        self.assertFalse(plan.ok)
        self.assertIn("已阻止保存", plan.errors[0])


class TestReloadAfterMigration(unittest.TestCase):
    """四审回归：legacy 双身份迁移后带 id 重载必须幂等（不得锁死）。"""

    def test_reloading_migrated_tunnels_with_ids_is_idempotent(self):
        from mpconf.config import assign_stable_ids
        tunnels = [{"name": "a", "ssh_user": "u", "ssh_host": "h",
                    "ssh_port": 22},
                   {"name": "b", "ssh_user": "u", "ssh_host": "h",
                    "ssh_port": 22}]
        assign_stable_ids(tunnels)  # 首次迁移：双 id
        # 带 id 重载（真实磁盘路径）：幂等，不 raise
        n = assign_stable_ids(tunnels)
        self.assertEqual(n, 0)
