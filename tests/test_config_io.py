"""Tests for config.py — load_config, save_config, _migrate."""
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from mpconf import config
class TestLoadConfig(unittest.TestCase):
    def test_nonexistent_returns_none(self):
        self.assertIsNone(config.load_config("/nonexistent/path.json"))


class TestSaveConfig(unittest.TestCase):
    def test_save_and_read_back(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "test.json")
            cfg = {"servers": [{"ssh": {"host": "srv", "port": 22}}],
                   "socks5_port": 1080}
            self.assertTrue(config.save_config(cfg, path))
            with open(path) as f:
                saved = json.load(f)
            self.assertEqual(saved["socks5_port"], 1080)

    def test_save_failure_returns_false(self):
        with patch("tempfile.mkstemp", side_effect=OSError("disk full")):
            self.assertFalse(config.save_config({}))


class TestMigrate(unittest.TestCase):
    def test_single_tunnel_format_migrated(self):
        old = {"ssh_host": "srv", "ssh_user": "u", "ssh_port": 22, "auth_type": "key"}
        result = config._migrate(old)
        self.assertIn("servers", result)
        self.assertEqual(len(result["servers"]), 1)
        self.assertEqual(result["servers"][0]["ssh"]["host"], "srv")
        self.assertEqual(result["schema_version"], config.SCHEMA_VERSION)

    def test_invalid_tunnels_raises(self):
        with self.assertRaises(ValueError):
            config._migrate({"tunnels": "not a list"})

    def test_v1_tunnels_migrated_to_servers(self):
        cfg = {"tunnels": [
            {"id": "t-x", "ssh_host": "s", "forwards": [
                {"local_port": 9000, "remote_port": 80}]}],
            "current_tunnel_id": "t-x", "current_tunnel": 0}
        result = config._migrate(cfg)
        srv = result["servers"][0]
        self.assertEqual(srv["ssh"]["host"], "s")
        self.assertEqual(srv["services"]["ssh"]["forwards"][0]["local_port"],
                         9000)
        self.assertEqual(result["proxy_server_id"], "t-x")
        self.assertNotIn("tunnels", result)
        self.assertNotIn("current_tunnel", result)
        self.assertNotIn("current_tunnel_id", result)

    def test_v1_role_collapse_falls_back_to_first_id(self):
        # current_tunnel_id 悬空 → 旧下标（这里指向首条）→ 首条兜底，
        # 解析序与 v1 相同；id 空时塌缩为空串（load 期 assign 再补）
        cfg = {"tunnels": [{"id": "t-a"}, {"id": "t-b"}],
               "current_tunnel_id": "gone", "current_tunnel": 5}
        result = config._migrate(cfg)
        self.assertEqual(result["proxy_server_id"], "t-a")

    def test_already_migrated_passes_through(self):
        cfg = {"servers": [{"ssh": {"host": "s"}}], "socks5_port": 1080,
               "schema_version": 2}
        result = config._migrate(cfg)
        self.assertEqual(len(result["servers"]), 1)
        self.assertEqual(result["socks5_port"], 1080)


class TestMergeConfigEdgeCases(unittest.TestCase):
    def test_none_input_uses_defaults(self):
        merged = config.merge_config(None)
        self.assertEqual(merged["socks5_port"], 1080)
        self.assertEqual(merged["servers"], [])

    def test_non_dict_server_entry_normalized_not_dropped(self):
        # v2 归一语义：非 dict 行折叠为空白默认服务器（保位不丢行），
        # 不再像 v1 那样静默剔除——手编配置的坏行在 UI 里可见可修
        merged = config.merge_config({"servers": ["not a dict",
                                                  {"ssh": {"host": "ok"}}]})
        self.assertEqual(len(merged["servers"]), 2)
        self.assertEqual(merged["servers"][0]["ssh"]["host"], "")
        self.assertEqual(merged["servers"][1]["ssh"]["host"], "ok")

    def test_non_list_servers_handled(self):
        merged = config.merge_config({"servers": "bad"})
        self.assertEqual(merged["servers"], [])

    def test_invalid_ssh_port_string_falls_back(self):
        merged = config.merge_config(
            {"servers": [{"ssh": {"host": "s", "port": "abc"}}]})
        self.assertEqual(merged["servers"][0]["ssh"]["port"], 22)

    def test_empty_capture_dir_falls_back_to_default(self):
        from capture.capture_store import DEFAULT_CAPTURE_DIR
        merged = config.merge_config({"capture_dir": "   "})
        self.assertEqual(merged["capture_dir"],
                         os.path.abspath(os.path.expanduser(DEFAULT_CAPTURE_DIR)))


class TestLoadConfigMigration(unittest.TestCase):
    def test_old_format_migrated_and_saved(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cfg.json")
            old = {"ssh_host": "srv", "ssh_user": "u", "ssh_port": 22}
            with open(path, "w") as f:
                json.dump(old, f)
            result = config.load_config(path)
            self.assertIn("servers", result)
            # File was rewritten in the new format
            with open(path) as f:
                saved = json.load(f)
            self.assertIn("servers", saved)

    def test_corrupt_config_backup_failure_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cfg.json")
            with open(path, "w") as f:
                f.write("{not valid json")
            with patch("os.replace", side_effect=OSError("cannot backup")):
                self.assertIsNone(config.load_config(path))

    def test_corrupt_config_backed_up_returns_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cfg.json")
            with open(path, "w") as f:
                f.write("{not valid json")
            self.assertIsNone(config.load_config(path))
            self.assertTrue(os.path.exists(path + ".bak"))

    def test_save_failure_isolates_plaintext_file(self):
        """When the migrated config cannot be written back, the pre-migration
        file — which may still hold plaintext ssh_password — must not stay on
        disk. It is isolated to .bak and load_config still returns the
        in-memory (cleaned) config."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cfg.json")
            old = {"tunnels": [{"ssh_host": "srv", "ssh_port": 22,
                                "auth_type": "password", "ssh_password": "secret"}]}
            with open(path, "w") as f:
                json.dump(old, f)
            with patch("mpconf.config.keychain.set_password", return_value=True), \
                 patch("mpconf.config.save_config", return_value=False):
                result = config.load_config(path)
            self.assertIsNotNone(result)
            self.assertNotIn("ssh_password", result["servers"][0])
            self.assertFalse(os.path.exists(path))
            with open(path + ".bak") as f:
                self.assertEqual(json.load(f), old)

    def test_save_failure_isolation_oserror_returns_migrated(self):
        """Isolation itself failing must not crash load_config."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cfg.json")
            old = {"tunnels": [{"ssh_host": "srv", "ssh_port": 22,
                                "auth_type": "password", "ssh_password": "secret"}]}
            with open(path, "w") as f:
                json.dump(old, f)
            with patch("mpconf.config.keychain.set_password", return_value=True), \
                 patch("mpconf.config.save_config", return_value=False), \
                 patch("os.replace", side_effect=OSError("cannot isolate")):
                result = config.load_config(path)
            self.assertIsNotNone(result)

    def test_migrate_non_dict_root_raises(self):
        with self.assertRaisesRegex(ValueError, "JSON object"):
            config._migrate(["not", "a", "dict"])


class TestSaveConfigUnlinkError(unittest.TestCase):
    def test_unlink_error_swallowed(self):
        with patch("tempfile.mkstemp", return_value=(99, "/tmp/x.tmp")), \
             patch("os.fdopen", side_effect=OSError("cannot open fd")), \
             patch("os.unlink", side_effect=OSError("cannot unlink")):
            self.assertFalse(config.save_config({}))


class TestMigratePasswordSweep(unittest.TestCase):
    def test_old_format_password_sets_auth_type(self):
        old = {"ssh_host": "srv", "ssh_password": "secret"}
        with patch("mpconf.config.keychain.set_password", return_value=True):
            result = config._migrate(old)
        server = result["servers"][0]
        self.assertEqual(server["ssh"]["auth_type"], "password")

    def test_plaintext_password_moved_to_keychain(self):
        cfg = {"tunnels": [{"ssh_host": "s", "ssh_password": "secret"}]}
        with patch("mpconf.config.keychain.set_password", return_value=True):
            result = config._migrate(cfg)
        self.assertNotIn("ssh_password", result["servers"][0])

    def test_password_removed_even_when_keychain_fails(self):
        """HARD-1: plaintext must leave the dict regardless of Keychain result."""
        cfg = {"tunnels": [{"ssh_host": "s", "ssh_password": "secret"}]}
        with patch("mpconf.config.keychain.set_password", return_value=False):
            result = config._migrate(cfg)
        self.assertNotIn("ssh_password", result["servers"][0])

    def test_password_not_persisted_on_disk_when_keychain_fails(self):
        """HARD-1 end-to-end: load_config must not write plaintext back to disk."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "cfg.json")
            with open(path, "w") as f:
                json.dump({"tunnels": [{"ssh_host": "s", "ssh_password": "secret"}]}, f)
            with patch("mpconf.config.keychain.set_password", return_value=False):
                config.load_config(path)
            with open(path) as f:
                on_disk = json.load(f)
        self.assertNotIn("ssh_password", on_disk["servers"][0])


class TestSaveConfigWriteError(unittest.TestCase):
    def test_write_error_returns_false_and_cleans_tmp(self):
        with patch("tempfile.mkstemp", return_value=(99, "/tmp/x.tmp")), \
             patch("os.fdopen", side_effect=OSError("cannot open fd")), \
             patch("os.unlink") as unlink:
            self.assertFalse(config.save_config({}))
        unlink.assert_called_once_with("/tmp/x.tmp")


class TestConfigApiEnabledKey(unittest.TestCase):
    """ADR-009：config_api_enabled 默认关 + 真值保留。"""

    def test_default_off_when_absent(self):
        self.assertIs(config.merge_config({})["config_api_enabled"], False)

    def test_explicit_true_preserved(self):
        self.assertIs(
            config.merge_config({"config_api_enabled": True})["config_api_enabled"],
            True)
