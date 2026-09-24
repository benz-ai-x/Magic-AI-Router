"""Tests for NFS config schema — merge 归一（mpconf.config）与 prepare
校验（mpconf.config_state）的 nfs 节面。
"""
import unittest

from mpconf.config import (merge_config, normalize_nfs, resolve_mount_dir)
from mpconf.config_state import ConfigStateStore


def _tunnel(**nfs):
    t = {"id": "t-abc", "name": "srv", "ssh_host": "srv", "ssh_user": "u",
         "auth_type": "key", "forwards": []}
    if nfs:
        t["nfs"] = nfs
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
        # auto_mount 严格布尔（is True）——与 forward_autostart 同款口径
        n = normalize_nfs({"mounts": [
            {"name": " d ", "remote_path": "/d", "local_dir": " /V/d",
             "auto_mount": 1, "bogus": True},
            "not-a-dict",
        ]})
        self.assertEqual(len(n["mounts"]), 1)
        self.assertEqual(n["mounts"][0], {
            "name": "d", "remote_path": "/d", "local_dir": "/V/d",
            "auto_mount": False})

    def test_merge_fresh_nfs_dict_per_tunnel_no_shared_default(self):
        cfg = merge_config({"tunnels": [_tunnel(), _tunnel()],
                            "current_tunnel_id": "t-abc"})
        a, b = cfg["tunnels"][0]["nfs"], cfg["tunnels"][1]["nfs"]
        self.assertIsNot(a, b)
        self.assertIsNot(a["mounts"], b["mounts"])
        self.assertFalse(a["enabled"])
        self.assertEqual(a["local_port"], 12049)

    def test_merge_preserves_valid_nfs(self):
        cfg = merge_config({"tunnels": [
            _tunnel(enabled=True, local_port=13000, squash_to_ssh_user=True,
                    mounts=[_mount(auto_mount=True)])]})
        n = cfg["tunnels"][0]["nfs"]
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

    def _prepare(self, tunnels):
        return self.store.prepare(mp={"tunnels": tunnels,
                                      "current_tunnel_id":
                                          tunnels[0].get("id", "")})

    def test_valid_nfs_passes(self):
        plan = self._prepare([_tunnel(enabled=True, local_port=13000,
                                      mounts=[_mount()])])
        self.assertTrue(plan.ok, plan.errors)

    def test_port_out_of_range_rejected(self):
        plan = self._prepare([_tunnel(local_port=70000, mounts=[_mount()])])
        self.assertFalse(plan.ok)
        self.assertTrue(any("NFS 本地端口无效" in e for e in plan.errors))

    def test_empty_name_rejected(self):
        plan = self._prepare([_tunnel(mounts=[_mount(name=" ")])])
        self.assertFalse(plan.ok)
        self.assertTrue(any("挂载名不能为空" in e for e in plan.errors))

    def test_duplicate_name_rejected(self):
        plan = self._prepare([_tunnel(mounts=[_mount(name="d"),
                                              _mount(name="d")])])
        self.assertFalse(plan.ok)
        self.assertTrue(any("重复" in e for e in plan.errors))

    def test_relative_remote_path_rejected(self):
        plan = self._prepare([_tunnel(mounts=[_mount(remote_path="data")])])
        self.assertFalse(plan.ok)
        self.assertTrue(any("绝对路径" in e for e in plan.errors))

    def test_relative_local_dir_rejected(self):
        plan = self._prepare([_tunnel(mounts=[_mount(local_dir="rel/x")])])
        self.assertFalse(plan.ok)
        self.assertTrue(any("本地目录" in e for e in plan.errors))

    def test_nfs_not_dict_rejected(self):
        plan = self._prepare([{**_tunnel(), "nfs": ["x"]}])
        self.assertFalse(plan.ok)
        self.assertTrue(any("nfs 必须是对象" in e for e in plan.errors))

    def test_port_conflict_with_forward_rejected(self):
        t = {**_tunnel(), "nfs": {"enabled": True, "local_port": 9000},
             "forwards": [{"local_port": 9000, "remote_host": "127.0.0.1",
                           "remote_port": 80}]}
        plan = self._prepare([t])
        self.assertFalse(plan.ok)
        self.assertTrue(any("端口冲突" in e for e in plan.errors))

    def test_port_conflict_with_socks5_rejected(self):
        plan = self.store.prepare(mp={
            "tunnels": [_tunnel(local_port=1080, mounts=[_mount()])],
            "current_tunnel_id": "t-abc", "socks5_port": 1080})
        self.assertFalse(plan.ok)
        self.assertTrue(any("端口冲突" in e for e in plan.errors))

    def test_mount_dir_conflict_across_tunnels_rejected(self):
        a = _tunnel(mounts=[_mount(name="d1", local_dir="/Volumes/dup")])
        b = {**_tunnel(mounts=[_mount(name="d2", local_dir="/Volumes/dup")]),
             "id": "t-xyz", "name": "srv2"}
        plan = self._prepare([a, b])
        self.assertFalse(plan.ok)
        self.assertTrue(any("挂载点冲突" in e for e in plan.errors))

    def test_default_dir_conflict_resolved_same_rule(self):
        # 两隧道各有一个同名挂载且都不填 local_dir → 默认目录相同 → 冲突
        a = _tunnel(mounts=[_mount(name="data")])
        b = {**_tunnel(mounts=[_mount(name="data")]),
             "id": "t-xyz", "name": "srv2"}
        plan = self._prepare([a, b])
        self.assertFalse(plan.ok)
        self.assertTrue(any("/Volumes/data" in e for e in plan.errors))

    def test_disabled_nfs_still_port_checked(self):
        # enabled=False 也查端口——用户随时会打开，冲突要提前暴露。
        # 基线：prepare 只校验传入原样（socks5_port 缺省不参与），单独放行；
        # 显式给 socks5_port=1080 后即冲突——禁用不豁免。
        plan = self._prepare([_tunnel(enabled=False, local_port=1080,
                                      mounts=[_mount()])])
        self.assertTrue(plan.ok, plan.errors)
        plan2 = self.store.prepare(mp={
            "tunnels": [_tunnel(enabled=False, local_port=1080,
                                mounts=[_mount()])],
            "current_tunnel_id": "t-abc", "socks5_port": 1080})
        self.assertFalse(plan2.ok)

    def test_nfs_states_decorated_field_stripped(self):
        plan = self._prepare([_tunnel(enabled=True,
                                      mounts=[_mount()])])
        self.assertTrue(plan.ok, plan.errors)
        # prepare 输入剥除只读装饰字段：nfs_states 不落盘
        t_in = _tunnel(enabled=True, mounts=[_mount()])
        t_in["nfs_states"] = {"data": "mounted"}
        plan2 = self.store.prepare(mp={"tunnels": [t_in],
                                       "current_tunnel_id": "t-abc"})
        self.assertTrue(plan2.ok, plan2.errors)
        self.assertNotIn("nfs_states", plan2.mp_candidate["tunnels"][0])


if __name__ == "__main__":
    unittest.main()
