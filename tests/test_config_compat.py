"""Backward-compat tests for config schema changes (batch 1).

Covers requirement 1 (delete "启动终端"): old configs that still carry the
``terminal_envs`` key must load without error and the field must be silently
dropped on the next save — no destructive migration, no crash.
"""
import json
import os
import tempfile
import unittest

from mpconf import config
class TestTerminalEnvsDropped(unittest.TestCase):
    def _old_cfg(self):
        return {
            "socks5_port": 1080,
            "http_listen_port": 8888,
            "current_tunnel": 0,
            "tunnels": [
                {"name": "demo", "ssh_user": "u", "ssh_host": "h",
                 "ssh_port": 22, "auth_type": "key", "ssh_key": "",
                 "ssh_compression": True}
            ],
            "terminal_envs": [
                {"name": "old profile", "env": "FOO=bar\nBAZ=qux"}
            ],
        }

    def test_merge_config_drops_terminal_envs(self):
        merged = config.merge_config(self._old_cfg())
        self.assertNotIn("terminal_envs", merged)
        self.assertNotIn("terminal_envs", config.DEFAULT_CONFIG)

    def test_load_then_save_drops_terminal_envs(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, ".magic-proxy.json")
            with open(cfg_path, "w") as f:
                json.dump(self._old_cfg(), f)

            cfg = config.load_config(cfg_path)
            self.assertIsNotNone(cfg, "load should not crash on old schema")
            merged = config.merge_config(cfg)
            self.assertNotIn("terminal_envs", merged)
            config.save_config(merged, cfg_path)
            with open(cfg_path) as f:
                on_disk = json.load(f)
            self.assertNotIn("terminal_envs", on_disk)


class TestConfigValidation(unittest.TestCase):
    def test_invalid_values_fall_back_safely(self):
        merged = config.merge_config({
            "http_listen_port": 99999,
            "socks5_port": "bad",
            "capture_port": -1,
            "retention_days": "bad",
            "current_tunnel": "bad",
            "capture_dir": "~/captures",
            "tunnels": [{"ssh_host": " example.com ", "ssh_port": 70000}],
        })
        self.assertEqual(merged["http_listen_port"], 8888)
        self.assertEqual(merged["socks5_port"], 1080)
        self.assertEqual(merged["capture_port"], 8080)
        self.assertEqual(merged["retention_days"], 7)
        self.assertEqual(merged["tunnels"][0]["ssh_port"], 22)
        self.assertTrue(os.path.isabs(merged["capture_dir"]))

    def test_non_object_config_is_backed_up_instead_of_crashing(self):
        with tempfile.TemporaryDirectory() as d:
            cfg_path = os.path.join(d, ".magic-proxy.json")
            with open(cfg_path, "w") as f:
                json.dump([], f)
            self.assertIsNone(config.load_config(cfg_path))
            self.assertTrue(os.path.exists(cfg_path + ".bak"))


class TestHttpListenPortBackcompat(unittest.TestCase):
    """Old configs stored ``http_listen`` as a "host:port" string. The new
    schema reads ``http_listen_port`` (int). Legacy values must convert."""

    def test_old_http_listen_string_converted_to_port(self):
        merged = config.merge_config({"http_listen": "127.0.0.1:8888"})
        self.assertEqual(merged["http_listen_port"], 8888)
        self.assertNotIn("http_listen", merged)

    def test_old_http_listen_non_loopback_silently_drops(self):
        # 0.0.0.0 host: legacy value parses for the port, but merge_config's
        # int range check (1-65535) is the only gate left. Host is always
        # treated as loopback now.
        merged = config.merge_config({"http_listen": "0.0.0.0:8888"})
        self.assertEqual(merged["http_listen_port"], 8888)

    def test_explicit_port_wins_over_legacy_string(self):
        merged = config.merge_config({
            "http_listen": "127.0.0.1:7777",
            "http_listen_port": 9999,
        })
        self.assertEqual(merged["http_listen_port"], 9999)


class TestPreventSleepLaunchLoginDefaults(unittest.TestCase):
    def test_new_fields_default_false_when_absent(self):
        old = {
            "socks5_port": 1080,
            "http_listen_port": 8888,
            "current_tunnel": 0,
            "tunnels": [],
        }
        merged = config.merge_config(old)
        self.assertFalse(merged["prevent_sleep"])
        self.assertFalse(merged["launch_at_login"])

    def test_non_bool_values_reset_to_false(self):
        merged = config.merge_config({"prevent_sleep": "yes", "launch_at_login": 1})
        self.assertIs(merged["prevent_sleep"], False)
        self.assertIs(merged["launch_at_login"], False)

    def test_bool_true_round_trips(self):
        merged = config.merge_config({"prevent_sleep": True, "launch_at_login": True})
        self.assertIs(merged["prevent_sleep"], True)
        self.assertIs(merged["launch_at_login"], True)


class TestMergeConfigTunnels(unittest.TestCase):
    def test_tunnel_missing_fields_get_defaults(self):
        merged = config.merge_config({"tunnels": [{"ssh_host": "srv"}]})
        t = merged["tunnels"][0]
        self.assertEqual(t["ssh_port"], 22)
        self.assertEqual(t["auth_type"], "key")
        self.assertTrue(t["ssh_compression"])

    def test_tunnel_port_out_of_range_falls_back(self):
        merged = config.merge_config({"tunnels": [{"ssh_host": "s", "ssh_port": 99999}]})
        self.assertEqual(merged["tunnels"][0]["ssh_port"], 22)

    def test_tunnel_auth_type_validated(self):
        merged = config.merge_config({"tunnels": [{"ssh_host": "s", "auth_type": "bogus"}]})
        self.assertEqual(merged["tunnels"][0]["auth_type"], "key")

    def test_tunnel_missing_fields_get_forwards_default(self):
        merged = config.merge_config({"tunnels": [{"ssh_host": "srv"}]})
        # forwards 缺省得 []（旧配置无感升级，无需迁移脚本）
        self.assertEqual(merged["tunnels"][0]["forwards"], [])


class TestMergeConfigForwards(unittest.TestCase):
    """端口转发读路径归一：剥未知键、字符串端口兼容、缺省回填、浅拷贝防护。"""

    def test_string_ports_read_compat(self):
        merged = config.merge_config({"tunnels": [{"ssh_host": "s", "forwards": [
            {"local_port": "9000", "remote_host": "db", "remote_port": "5432"},
        ]}]})
        self.assertEqual(merged["tunnels"][0]["forwards"], [
            {"local_port": 9000, "remote_host": "db", "remote_port": 5432,
             "enabled": True}])

    def test_unknown_keys_stripped_and_remote_host_defaults(self):
        merged = config.merge_config({"tunnels": [{"ssh_host": "s", "forwards": [
            {"local_port": 9000, "remote_port": 8000, "note": "dropped"},
        ]}]})
        self.assertEqual(merged["tunnels"][0]["forwards"], [
            {"local_port": 9000, "remote_host": "127.0.0.1", "remote_port": 8000,
             "enabled": True}])

    def test_invalid_port_falls_to_zero_not_dropped(self):
        """非法端口落 0（下次保存被 prepare 拦），绝不静默丢行。"""
        merged = config.merge_config({"tunnels": [{"ssh_host": "s", "forwards": [
            {"local_port": "abc", "remote_host": "h", "remote_port": 70000},
        ]}]})
        self.assertEqual(merged["tunnels"][0]["forwards"], [
            {"local_port": 0, "remote_host": "h", "remote_port": 0,
             "enabled": True}])

    def test_non_dict_rows_and_non_list_dropped(self):
        merged = config.merge_config({"tunnels": [
            {"ssh_host": "s", "forwards": ["bad", 42,
             {"local_port": 9000, "remote_port": 80}]},
            {"ssh_host": "s2", "forwards": "not-a-list"},
        ]})
        self.assertEqual(merged["tunnels"][0]["forwards"], [
            {"local_port": 9000, "remote_host": "127.0.0.1", "remote_port": 80,
             "enabled": True}])
        self.assertEqual(merged["tunnels"][1]["forwards"], [])

    def test_forward_autostart_defaults_and_normalization(self):
        merged = config.merge_config({"tunnels": [
            {"ssh_host": "a"},                         # 缺省 False
            {"ssh_host": "b", "forward_autostart": True},   # 显式开
            {"ssh_host": "c", "forward_autostart": "yes"},  # 非 bool 归 False
        ]})
        self.assertIs(merged["tunnels"][0]["forward_autostart"], False)
        self.assertIs(merged["tunnels"][1]["forward_autostart"], True)
        self.assertIs(merged["tunnels"][2]["forward_autostart"], False)

    def test_default_list_not_shared_across_tunnels(self):
        """DEFAULT_TUNNEL.copy() 是浅拷贝——forwards 默认 [] 绝不能跨隧道
        共享同一 list 对象（后续 append 会串隧道）。"""
        merged = config.merge_config({"tunnels": [
            {"ssh_host": "a"}, {"ssh_host": "b"},
        ]})
        fa, fb = merged["tunnels"][0]["forwards"], merged["tunnels"][1]["forwards"]
        self.assertIsNot(fa, fb)
        self.assertIsNot(fa, config.DEFAULT_TUNNEL["forwards"])
        fa.append({"local_port": 1, "remote_host": "h", "remote_port": 2})
        self.assertEqual(fb, [])
        self.assertEqual(config.DEFAULT_TUNNEL["forwards"], [])


class TestMergeConfigPorts(unittest.TestCase):
    def test_port_out_of_range_falls_back(self):
        merged = config.merge_config({"socks5_port": 0, "capture_port": 99999, "config_port": 70000})
        self.assertEqual(merged["socks5_port"], 1080)
        self.assertEqual(merged["capture_port"], config.DEFAULT_CAPTURE_PORT)
        self.assertEqual(merged["config_port"], 9528)

    def test_http_listen_port_out_of_range_falls_back(self):
        merged = config.merge_config({"http_listen_port": 70000})
        self.assertEqual(merged["http_listen_port"], config.DEFAULT_CONFIG["http_listen_port"])

    def test_current_tunnel_out_of_range_resets(self):
        merged = config.merge_config({"current_tunnel": 5, "tunnels": [{"ssh_host": "s"}]})
        self.assertEqual(merged["current_tunnel"], 0)

    def test_capture_dir_expands_home(self):
        merged = config.merge_config({"capture_dir": "~/captures"})
        self.assertTrue(merged["capture_dir"].startswith(os.path.expanduser("~")))
        self.assertNotIn("~", merged["capture_dir"])


if __name__ == "__main__":
    unittest.main()


class TestProxyRoleResolution(unittest.TestCase):
    """v0.9.2 代理角色双表示：current_tunnel_id（稳定 id）是唯一真相，
    current_tunnel 下标退为兼容读入口 + merge 派生投影。"""

    def _two(self):
        return [
            {"id": "t-a", "ssh_host": "a", "ssh_port": 22, "auth_type": "key"},
            {"id": "t-b", "ssh_host": "b", "ssh_port": 22, "auth_type": "key"},
        ]

    def test_id_wins_over_index_and_backfills_projection(self):
        merged = config.merge_config({
            "current_tunnel": 0, "current_tunnel_id": "t-b",
            "tunnels": self._two()})
        self.assertEqual(merged["current_tunnel_id"], "t-b")
        self.assertEqual(merged["current_tunnel"], 1)

    def test_dangling_id_falls_back_to_index(self):
        merged = config.merge_config({
            "current_tunnel": 1, "current_tunnel_id": "t-gone",
            "tunnels": self._two()})
        self.assertEqual(merged["current_tunnel_id"], "t-b")
        self.assertEqual(merged["current_tunnel"], 1)

    def test_legacy_index_only_backfills_id(self):
        merged = config.merge_config({
            "current_tunnel": 1, "tunnels": self._two()})
        self.assertEqual(merged["current_tunnel_id"], "t-b")

    def test_role_survives_reorder(self):
        reordered = config.merge_config({
            "current_tunnel": 0, "current_tunnel_id": "t-b",
            "tunnels": [self._two()[1], self._two()[0]]})
        self.assertEqual(reordered["current_tunnel_id"], "t-b")
        self.assertEqual(reordered["current_tunnel"], 0,
                         "投影下标随位置重算，真相 id 纹丝不动")

    def test_empty_tunnels_resets_role(self):
        merged = config.merge_config({"current_tunnel": 3, "tunnels": []})
        self.assertEqual(merged["current_tunnel"], 0)
        self.assertEqual(merged["current_tunnel_id"], "")

    def test_idless_tunnel_role_stays_indexless_id(self):
        # 新隧道保存时尚未赋 id（下次 load 才赋）——id 真相保持空，
        # 角色经下标解析不丢
        merged = config.merge_config({
            "current_tunnel": 0, "tunnels": [{"ssh_host": "x", "ssh_port": 22}]})
        self.assertEqual(merged["current_tunnel_id"], "")
        self.assertEqual(merged["current_tunnel"], 0)
