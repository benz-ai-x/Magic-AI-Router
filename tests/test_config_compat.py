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
