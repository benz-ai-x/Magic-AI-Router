"""Tests for NFS config schema — merge 归一（mpconf.config）与 prepare
校验（mpconf.config_state）的 nfs 节面。
"""
import unittest

from mpconf.config import (merge_config, normalize_nfs, resolve_mount_dir,
                           server_nfs)
from mpconf.config_state import ConfigStateStore


def _server(**nfs):
    t = {"id": "t-abc", "name": "srv",
         "ssh": {"host": "srv", "user": "u", "auth_type": "key"},
         "services": {"ssh": {"forwards": []}}}
    if nfs:
        t["services"]["nfs"] = nfs
    return t


def _mount(name="data", remote_path="/data", local_dir="", auto_mount=False):
    return {"name": name, "remote_path": remote_path,
            "local_dir": local_dir, "auto_mount": auto_mount}


class TestNormalizeNfs(unittest.TestCase):
    def test_absent_folds_to_disabled_default(self):
        n = normalize_nfs(None)
        self.assertEqual(n, {"enabled": False, "local_port": 12049,
                             "squash_to_ssh_user": False, "mounts": []})

    def test_string_port_coerced(self):
        self.assertEqual(normalize_nfs({"local_port": "13000"})["local_port"],
                         13000)
        self.assertEqual(normalize_nfs({"local_port": "x"})["local_port"],
                         12049)
        self.assertEqual(normalize_nfs({"local_port": 70000})["local_port"],
                         12049)

    def test_mount_rows_normalized_unknown_keys_stripped(self):
        # auto_mount 严格布尔（is True）——与 services.ssh.autostart 同款口径
        n = normalize_nfs({"mounts": [
            {"name": " d ", "remote_path": "/d", "local_dir": " /V/d",
             "auto_mount": 1, "bogus": True},
            "not-a-dict",
        ]})
        self.assertEqual(len(n["mounts"]), 1)
        self.assertEqual(n["mounts"][0], {
            "name": "d", "remote_path": "/d", "local_dir": "/V/d",
            "auto_mount": False})

    def test_merge_fresh_nfs_dict_per_server_no_shared_default(self):
        cfg = merge_config({"servers": [_server(), _server()],
                            "proxy_server_id": "t-abc"})
        a, b = (server_nfs(s) for s in cfg["servers"])
        self.assertIsNot(a, b)
        self.assertIsNot(a["mounts"], b["mounts"])
        self.assertFalse(a["enabled"])
        self.assertEqual(a["local_port"], 12049)

    def test_merge_preserves_valid_nfs(self):
        cfg = merge_config({"servers": [
            _server(enabled=True, local_port=13000, squash_to_ssh_user=True,
                    mounts=[_mount(auto_mount=True)])]})
        n = server_nfs(cfg["servers"][0])
        self.assertTrue(n["enabled"])
        self.assertEqual(n["local_port"], 13000)
        self.assertTrue(n["squash_to_ssh_user"])
        self.assertTrue(n["mounts"][0]["auto_mount"])


class TestResolveMountDir(unittest.TestCase):
    def test_explicit_dir_wins(self):
        self.assertEqual(resolve_mount_dir(_mount(local_dir="/Users/x/m")),
                         "/Users/x/m")

    def test_blank_defaults_to_volumes_with_name(self):
        self.assertEqual(resolve_mount_dir(_mount(name="data")),
                         "/Volumes/data")

    def test_name_separators_sanitized(self):
        self.assertEqual(resolve_mount_dir(_mount(name="a/b\\c")),
                         "/Volumes/a-b-c")

    def test_blank_name_falls_back_to_generic(self):
        self.assertEqual(resolve_mount_dir(_mount(name="")),
                         "/Volumes/nfs")

    def test_none_row_safe(self):
        self.assertEqual(resolve_mount_dir(None), "/Volumes/nfs")


class TestPrepareNfsValidation(unittest.TestCase):
    def setUp(self):
        self.store = ConfigStateStore()

    def _prepare(self, servers):
        return self.store.prepare(mp={"servers": servers})

    def test_valid_nfs_passes(self):
        plan = self._prepare([_server(enabled=True, local_port=13000,
                                      mounts=[_mount()])])
        self.assertTrue(plan.ok, plan.errors)

    def test_port_out_of_range_rejected(self):
        plan = self._prepare([_server(local_port=70000, mounts=[_mount()])])
        self.assertFalse(plan.ok)
        self.assertTrue(any("NFS 本地端口无效" in e for e in plan.errors))

    def test_empty_name_rejected(self):
        plan = self._prepare([_server(mounts=[_mount(name=" ")])])
        self.assertFalse(plan.ok)
        self.assertTrue(any("挂载名不能为空" in e for e in plan.errors))

    def test_duplicate_name_rejected(self):
        plan = self._prepare([_server(mounts=[_mount(name="d"),
                                              _mount(name="d")])])
        self.assertFalse(plan.ok)
        self.assertTrue(any("重复" in e for e in plan.errors))

    def test_relative_remote_path_rejected(self):
        plan = self._prepare([_server(mounts=[_mount(remote_path="data")])])
        self.assertFalse(plan.ok)
        self.assertTrue(any("绝对路径" in e for e in plan.errors))

    def test_relative_local_dir_rejected(self):
        plan = self._prepare([_server(mounts=[_mount(local_dir="rel/x")])])
        self.assertFalse(plan.ok)
        self.assertTrue(any("本地目录" in e for e in plan.errors))

    def test_nfs_not_dict_treated_as_absent(self):
        # v2 语义（与 JS 第一道闸同口径）：非 dict 的 services.nfs 视为
        # 未配置不校验——merge 归一回默认节点，绝不落盘原形状。
        # v1 的「nfs 必须是对象」报错随换轴退役（见 PR 报告）。
        plan = self._prepare([{**_server(), "services": {
            "ssh": {"forwards": []}, "nfs": ["x"]}}])
        self.assertTrue(plan.ok, plan.errors)
        merged = merge_config(plan.mp_candidate)
        self.assertEqual(server_nfs(merged["servers"][0]),
                         {"enabled": False, "local_port": 12049,
                          "squash_to_ssh_user": False, "mounts": []})

    def test_port_conflict_with_forward_rejected(self):
        srv = _server()
        srv["services"] = {
            "ssh": {"forwards": [
                {"local_port": 9000, "remote_host": "127.0.0.1",
                 "remote_port": 80}]},
            "nfs": {"enabled": True, "local_port": 9000}}
        plan = self._prepare([srv])
        self.assertFalse(plan.ok)
        self.assertTrue(any("端口冲突" in e for e in plan.errors))

    def test_port_conflict_with_socks5_rejected(self):
        plan = self.store.prepare(mp={
            "servers": [_server(local_port=1080, mounts=[_mount()])],
            "socks5_port": 1080})
        self.assertFalse(plan.ok)
        self.assertTrue(any("端口冲突" in e for e in plan.errors))

    def test_mount_dir_conflict_across_servers_rejected(self):
        a = _server(mounts=[_mount(name="d1", local_dir="/Volumes/dup")])
        b = {**_server(mounts=[_mount(name="d2", local_dir="/Volumes/dup")]),
             "id": "t-xyz", "name": "srv2"}
        plan = self._prepare([a, b])
        self.assertFalse(plan.ok)
        self.assertTrue(any("挂载点冲突" in e for e in plan.errors))

    def test_default_dir_conflict_resolved_same_rule(self):
        # 两台服务器各有一个同名挂载且都不填 local_dir → 默认目录相同 → 冲突
        a = _server(mounts=[_mount(name="data")])
        b = {**_server(mounts=[_mount(name="data")]),
             "id": "t-xyz", "name": "srv2"}
        plan = self._prepare([a, b])
        self.assertFalse(plan.ok)
        self.assertTrue(any("/Volumes/data" in e for e in plan.errors))

    def test_disabled_nfs_still_port_checked(self):
        # enabled=False 也查端口——用户随时会打开，冲突要提前暴露。
        # 基线：prepare 只校验传入原样（socks5_port 缺省不参与），单独放行；
        # 显式给 socks5_port=1080 后即冲突——禁用不豁免。
        plan = self._prepare([_server(enabled=False, local_port=1080,
                                      mounts=[_mount()])])
        self.assertTrue(plan.ok, plan.errors)
        plan2 = self.store.prepare(mp={
            "servers": [_server(enabled=False, local_port=1080,
                                mounts=[_mount()])],
            "socks5_port": 1080})
        self.assertFalse(plan2.ok)

    def test_nfs_states_decorated_field_stripped(self):
        plan = self._prepare([_server(enabled=True,
                                      mounts=[_mount()])])
        self.assertTrue(plan.ok, plan.errors)
        # prepare 输入剥除只读装饰字段：nfs_states 不落盘
        s_in = _server(enabled=True, mounts=[_mount()])
        s_in["nfs_states"] = {"data": "mounted"}
        plan2 = self.store.prepare(mp={"servers": [s_in]})
        self.assertTrue(plan2.ok, plan2.errors)
        self.assertNotIn("nfs_states", plan2.mp_candidate["servers"][0])


if __name__ == "__main__":
    unittest.main()
