"""server_check 单元测试：服务卡注册表形状 + 三卡探测归一 + check_server 编排。

探针 argv / ssh 失败分类 / check_remote 解析的测试各在
test_ssh_launch.py / test_remote_setup.py——这里只测服务卡层的输入守卫
透传、结果归一与编排隔离（底层原语 mock 掉，绝不起真 ssh）。
"""
import unittest
from unittest.mock import patch

from services import server_check


class _FakeKeychain:
    """测试替身：密码认证槽固定回值，密钥认证路径永不触达。"""

    def __init__(self, password=""):
        self._password = password
        self.calls = []

    def get_password(self, tunnel):
        self.calls.append(tunnel)
        return self._password


_KEY_SERVER = {
    "ssh": {"host": "example.com", "user": "u", "port": 2222,
            "auth_type": "key", "ssh_key": "~/.ssh/id_ed25519"},
}
_PW_SERVER = {
    "ssh": {"host": "example.com", "user": "u", "port": 22,
            "auth_type": "password"},
}


class TestServiceCards(unittest.TestCase):
    def test_registry_covers_three_service_types(self):
        self.assertEqual(set(server_check.SERVICE_CARDS),
                         {"ssh", "nfs", "openvpn"})

    def test_every_card_has_label_and_probe(self):
        for key, card in server_check.SERVICE_CARDS.items():
            with self.subTest(key=key):
                self.assertIsInstance(card["label"], str)
                self.assertTrue(card["label"])
                self.assertTrue(callable(card["probe"]))


class TestProbeInputs(unittest.TestCase):
    """自 config_server 迁入的输入守卫语义（原 TestTunnelProbeLogic 覆盖
    端到端路径，这里直打新归宿）。"""

    def test_missing_host_rejected(self):
        normalized, password, error = server_check.probe_inputs(
            {"ssh": {"host": "  ", "port": 22}}, _FakeKeychain())
        self.assertIsNone(normalized)
        self.assertIn("地址", error)

    def test_invalid_port_rejected(self):
        for port in (99999, "x", None):
            with self.subTest(port=port):
                normalized, _, error = server_check.probe_inputs(
                    {"ssh": {"host": "h", "port": port}}, _FakeKeychain())
                self.assertIsNone(normalized)
                self.assertTrue(error)

    def test_option_like_destination_rejected(self):
        _, _, error = server_check.probe_inputs(
            {"ssh": {"host": "-oProxyCommand=evil"}}, _FakeKeychain())
        self.assertIn("无效", error)

    def test_password_auth_without_saved_password(self):
        _, password, error = server_check.probe_inputs(
            _PW_SERVER, _FakeKeychain(password=""))
        self.assertEqual(password, "")
        self.assertIn("密码", error)

    def test_fields_normalized_for_probe(self):
        raw = {"ssh": {"host": "  example.com ", "user": " u ",
                       "port": "2222", "auth_type": "key", "ssh_key": "k"}}
        normalized, password, error = server_check.probe_inputs(
            raw, _FakeKeychain())
        self.assertFalse(error)
        self.assertEqual(password, "")
        self.assertEqual(normalized["ssh"]["host"], "example.com")
        self.assertEqual(normalized["ssh"]["user"], "u")
        self.assertEqual(normalized["ssh"]["port"], 2222)

    def test_password_auth_returns_saved_password(self):
        normalized, password, error = server_check.probe_inputs(
            _PW_SERVER, _FakeKeychain(password="sekrit"))
        self.assertFalse(error)
        self.assertEqual(password, "sekrit")


class TestProbeSsh(unittest.TestCase):
    def test_ok_carries_latency(self):
        with patch.object(server_check.ssh_launch, "probe",
                          return_value={"ok": True}) as probe:
            result = server_check.probe_ssh(_KEY_SERVER, _FakeKeychain())
        self.assertTrue(result["ok"])
        self.assertEqual(result["error"], "")
        self.assertIsInstance(result["latency_ms"], int)
        self.assertGreaterEqual(result["latency_ms"], 0)
        probe.assert_called_once()

    def test_failure_passes_error_through_without_latency(self):
        with patch.object(server_check.ssh_launch, "probe",
                          return_value={"ok": False, "error": "连接超时"}):
            result = server_check.probe_ssh(_KEY_SERVER, _FakeKeychain())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "连接超时")
        self.assertIsNone(result["latency_ms"])

    def test_input_guard_blocks_before_probe(self):
        with patch.object(server_check.ssh_launch, "probe") as probe:
            result = server_check.probe_ssh(
                {"ssh": {"host": "", "port": 22}}, _FakeKeychain())
        probe.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIsNone(result["latency_ms"])

    def test_password_flows_to_probe(self):
        with patch.object(server_check.ssh_launch, "probe",
                          return_value={"ok": True}) as probe:
            server_check.probe_ssh(_PW_SERVER, _FakeKeychain(password="sekrit"))
        self.assertEqual(probe.call_args.kwargs.get("password"), "sekrit")


class TestProbeNfs(unittest.TestCase):
    def test_ok_maps_check_remote_shape(self):
        raw = {"ok": True, "family": "apt-get", "installed": True,
               "listening": True, "exports": "/data 127.0.0.1(...)\n"}
        with patch.object(server_check.remote_setup, "check_remote",
                          return_value=raw) as check:
            result = server_check.probe_nfs(_KEY_SERVER, _FakeKeychain())
        self.assertTrue(result["ok"])
        self.assertEqual(result["family"], "apt-get")
        self.assertTrue(result["installed"])
        self.assertTrue(result["listening_2049"])
        self.assertTrue(result["exports_configured"])
        self.assertEqual(check.call_args.kwargs.get("password"), "")

    def test_empty_exports_reported_unconfigured(self):
        raw = {"ok": True, "family": "dnf", "installed": False,
               "listening": False, "exports": ""}
        with patch.object(server_check.remote_setup, "check_remote",
                          return_value=raw):
            result = server_check.probe_nfs(_KEY_SERVER, _FakeKeychain())
        self.assertTrue(result["ok"])
        self.assertFalse(result["installed"])
        self.assertFalse(result["listening_2049"])
        self.assertFalse(result["exports_configured"])

    def test_failure_defaults_fields(self):
        raw = {"ok": False, "error": "连接超时"}
        with patch.object(server_check.remote_setup, "check_remote",
                          return_value=raw):
            result = server_check.probe_nfs(_KEY_SERVER, _FakeKeychain())
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "连接超时")
        self.assertEqual(
            result,
            {"ok": False, "error": "连接超时", "family": "",
             "installed": False, "listening_2049": False,
             "exports_configured": False})

    def test_input_guard_blocks_before_check(self):
        with patch.object(server_check.remote_setup, "check_remote") as check:
            result = server_check.probe_nfs(
                {"ssh": {"host": "-evil", "port": 22}}, _FakeKeychain())
        check.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("无效", result["error"])


class TestProbeOpenvpn(unittest.TestCase):
    def _run(self, stdout="", ok=True, error=""):
        result = {"ok": ok, "stdout": stdout, "stderr": ""}
        if not ok:
            result["error"] = error
        with patch.object(server_check.ssh_launch, "run_remote",
                          return_value=result) as remote:
            outcome = server_check.probe_openvpn(_KEY_SERVER, _FakeKeychain())
        return outcome, remote

    def test_installed_parses_version_line(self):
        outcome, remote = self._run(
            "OpenVPN 2.5.9 x86_64-pc-linux-gnu [SSL (OpenSSL)]\n")
        self.assertTrue(outcome["ok"])
        self.assertTrue(outcome["installed"])
        self.assertEqual(outcome["version"],
                         "OpenVPN 2.5.9 x86_64-pc-linux-gnu [SSL (OpenSSL)]")
        # 只读探测：无 sudo 密码管道（sudo_password 不传 = stdin DEVNULL）
        self.assertNotIn("sudo_password", remote.call_args.kwargs)

    def test_absent_marker_reports_not_installed(self):
        outcome, _ = self._run("__ABSENT__\n")
        self.assertTrue(outcome["ok"])
        self.assertFalse(outcome["installed"])
        self.assertEqual(outcome["version"], "")

    def test_version_line_wins_over_absent_marker(self):
        """老版 openvpn --version 退出码非 0 时两条都会出现——按版本行判已装。"""
        outcome, _ = self._run(
            "OpenVPN 2.4.0 x86_64-pc-linux-gnu\n__ABSENT__\n")
        self.assertTrue(outcome["installed"])
        self.assertEqual(outcome["version"],
                         "OpenVPN 2.4.0 x86_64-pc-linux-gnu")

    def test_connection_failure_reports_error(self):
        outcome, _ = self._run(ok=False, error="连接超时")
        self.assertFalse(outcome["ok"])
        self.assertEqual(outcome["error"], "连接超时")
        self.assertFalse(outcome["installed"])
        self.assertEqual(outcome["version"], "")

    def test_input_guard_blocks_before_remote(self):
        with patch.object(server_check.ssh_launch, "run_remote") as remote:
            result = server_check.probe_openvpn(
                {"ssh": {"port": 0}}, _FakeKeychain())
        remote.assert_not_called()
        self.assertFalse(result["ok"])
        self.assertIn("地址", result["error"])

    def test_password_flows_to_remote(self):
        result = {"ok": True, "stdout": "__ABSENT__\n", "stderr": ""}
        with patch.object(server_check.ssh_launch, "run_remote",
                          return_value=result) as remote:
            server_check.probe_openvpn(_PW_SERVER,
                                       _FakeKeychain(password="sekrit"))
        self.assertEqual(remote.call_args.kwargs.get("password"), "sekrit")
        self.assertIn("command -v openvpn", remote.call_args.args[1])


class TestCheckServer(unittest.TestCase):
    """编排器：全卡聚合 / only 过滤 / 单卡异常隔离（走注册表真接线，
    底层原语 mock——不patch probe 函数本身，注册表持有的是原引用）。"""

    def test_only_none_runs_all_cards(self):
        with patch.object(server_check.ssh_launch, "probe",
                          return_value={"ok": True}), \
             patch.object(server_check.remote_setup, "check_remote",
                          return_value={"ok": True, "family": "apt-get",
                                        "installed": True, "listening": True,
                                        "exports": "/data 127.0.0.1(x)\n"}), \
             patch.object(server_check.ssh_launch, "run_remote",
                          return_value={"ok": True,
                                        "stdout": "__ABSENT__\n",
                                        "stderr": ""}):
            results = server_check.check_server(_KEY_SERVER, _FakeKeychain())
        self.assertEqual(set(results), {"ssh", "nfs", "openvpn"})
        self.assertTrue(results["ssh"]["ok"])
        self.assertTrue(results["nfs"]["exports_configured"])
        self.assertFalse(results["openvpn"]["installed"])

    def test_only_filters_to_requested_card(self):
        with patch.object(server_check.ssh_launch, "probe") as probe, \
             patch.object(server_check.remote_setup, "check_remote",
                          return_value={"ok": True, "family": "apt-get",
                                        "installed": False, "listening": False,
                                        "exports": ""}):
            results = server_check.check_server(_KEY_SERVER, _FakeKeychain(),
                                                only="nfs")
        self.assertEqual(set(results), {"nfs"})
        probe.assert_not_called()

    def test_one_card_crash_does_not_kill_others(self):
        with patch.object(server_check.ssh_launch, "probe",
                          side_effect=RuntimeError("boom")), \
             patch.object(server_check.remote_setup, "check_remote",
                          return_value={"ok": False, "error": "连接超时"}), \
             patch.object(server_check.ssh_launch, "run_remote",
                          return_value={"ok": True, "stdout": "__ABSENT__\n",
                                        "stderr": ""}):
            results = server_check.check_server(_KEY_SERVER, _FakeKeychain())
        self.assertEqual(set(results), {"ssh", "nfs", "openvpn"})
        # 崩溃卡归一为中文错误，不连坐
        self.assertFalse(results["ssh"]["ok"])
        self.assertIn("错误", results["ssh"]["error"])
        self.assertFalse(results["nfs"]["ok"])
        self.assertTrue(results["openvpn"]["ok"])


if __name__ == "__main__":
    unittest.main()
