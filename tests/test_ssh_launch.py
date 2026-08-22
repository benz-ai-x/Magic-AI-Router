"""Tests for tunnel/ssh_launch.py — SSH 调用策略单一归宿。

策略面（argv / 失败分类）直接钉在本模块；两个调用方
（SSHMonitor.start / config_server.test_tunnel）只留各自职责的测试。
"""
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tunnel import host_key, ssh_launch


class TestDescribeFailure(unittest.TestCase):
    """stderr → 中文短语的分类表：顺序敏感，变更须先于未信任。"""

    def test_host_key_changed_maps_before_verification_failed(self):
        stderr = ("@@@@ REMOTE HOST IDENTIFICATION HAS CHANGED! @@@@\n"
                  "Host key verification failed.")
        self.assertIn("主机密钥已变更", ssh_launch.describe_failure(stderr))

    def test_host_key_untrusted_mapping(self):
        self.assertIn("主机密钥未信任",
                      ssh_launch.describe_failure("Host key verification failed."))

    def test_permission_denied_mapping(self):
        self.assertIn("认证失败", ssh_launch.describe_failure(
            "u@example.com: Permission denied (publickey)."))

    def test_connection_refused_mapping(self):
        self.assertIn("连接被服务器拒绝",
                      ssh_launch.describe_failure("Connection refused"))

    def test_resolve_failure_mapping(self):
        self.assertIn("无法解析服务器地址",
                      ssh_launch.describe_failure("Could not resolve hostname x"))

    def test_timeout_mapping(self):
        self.assertIn("连接超时",
                      ssh_launch.describe_failure("Connection timed out"))

    def test_no_route_mapping(self):
        self.assertIn("无法路由到服务器",
                      ssh_launch.describe_failure("No route to host"))

    def test_network_unreachable_mapping(self):
        self.assertIn("网络不可达",
                      ssh_launch.describe_failure("Network is unreachable"))

    def test_unknown_failure_includes_first_stderr_line(self):
        result = ssh_launch.describe_failure("some exotic failure\nsecond line")
        self.assertIn("some exotic failure", result)
        self.assertNotIn("second line", result)

    def test_long_first_line_is_truncated(self):
        result = ssh_launch.describe_failure("x" * 200)
        self.assertLess(len(result), 200)

    def test_none_stderr_degrades_gracefully(self):
        self.assertIn("未知错误", ssh_launch.describe_failure(None))

    def test_empty_stderr_degrades_gracefully(self):
        self.assertIn("未知错误", ssh_launch.describe_failure("  "))


class TestHostKeyChanged(unittest.TestCase):
    """is_host_key_changed 的分类谓词与失败文案共用同一张表。"""

    def test_changed_phrase_matches(self):
        self.assertTrue(ssh_launch.host_key_changed(
            "REMOTE HOST IDENTIFICATION HAS CHANGED"))

    def test_plain_verification_failure_is_not_changed(self):
        self.assertFalse(ssh_launch.host_key_changed(
            "Host key verification failed."))

    def test_empty_is_not_changed(self):
        self.assertFalse(ssh_launch.host_key_changed(""))


class TestBuildTunnelCommand(unittest.TestCase):
    """长驻隧道 argv 策略：SSHMonitor.start 的完整策略面。"""

    _KEY = {"ssh_host": "srv", "ssh_user": "u", "ssh_port": 22,
            "auth_type": "key", "ssh_key": "~/.ssh/id_rsa"}

    def test_key_auth_full_argv(self):
        sc = ssh_launch.build_tunnel_command(self._KEY, 1080)
        try:
            self.assertEqual(sc.cmd, [
                "ssh", "-i", "~/.ssh/id_rsa",
                "-D", "1080", "-N",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "StrictHostKeyChecking=yes",
                "-o", f"UserKnownHostsFile={host_key.KNOWN_HOSTS_PATH}",
                "-o", "GlobalKnownHostsFile=/dev/null",
                "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=3",
                "-C",
                "-p", "22", "u@srv",
            ])
            self.assertEqual(sc.display_cmd, " ".join(sc.cmd))
            self.assertEqual(sc.pass_fds, ())
            self.assertIsNone(sc.password_fd)
        finally:
            sc.close_password_fd()

    def test_compression_off_omits_dash_c(self):
        t = dict(self._KEY, ssh_compression=False)
        sc = ssh_launch.build_tunnel_command(t, 1080)
        self.assertNotIn("-C", sc.cmd)

    def test_no_user_destination_is_bare_host(self):
        t = {k: v for k, v in self._KEY.items() if k != "ssh_user"}
        sc = ssh_launch.build_tunnel_command(t, 1080)
        self.assertEqual(sc.cmd[-1], "srv")

    def test_default_port_22(self):
        t = {k: v for k, v in self._KEY.items() if k != "ssh_port"}
        sc = ssh_launch.build_tunnel_command(t, 1080)
        self.assertEqual(sc.cmd[-2:], ["22", "u@srv"])

    def test_password_auth_uses_sshpass_fd(self):
        sc = ssh_launch.build_tunnel_command(
            {"ssh_host": "srv", "ssh_user": "u", "auth_type": "password"},
            1080, "sekrit")
        try:
            self.assertEqual(sc.cmd[0], "sshpass")
            self.assertEqual(sc.cmd[1], "-d")
            # 密码绝不出现在 argv；展示串的 -d 参数打码为 ***。
            self.assertNotIn("sekrit", sc.cmd)
            self.assertEqual(sc.display_cmd.split()[:3],
                             ["sshpass", "-d", "***"])
            self.assertEqual(sc.pass_fds, (sc.password_fd,))
            self.assertIsNotNone(sc.password_fd)
        finally:
            sc.close_password_fd()

    def test_close_password_fd_is_idempotent(self):
        sc = ssh_launch.build_tunnel_command(
            {"ssh_host": "srv", "auth_type": "password"}, 1080, "sekrit")
        fd = sc.password_fd
        sc.close_password_fd()
        with self.assertRaises(OSError):
            os.fstat(fd)  # 已真正关闭
        sc.close_password_fd()  # 第二次不抛

    def test_key_auth_close_is_noop(self):
        sc = ssh_launch.build_tunnel_command(self._KEY, 1080)
        sc.close_password_fd()  # 无 fd 也不抛


class TestProbe(unittest.TestCase):
    """一次性探针：与隧道同策略，连一次即退（remote command `true`）。"""

    _KEY = {"ssh_host": "example.com", "ssh_user": "u", "ssh_port": 2222,
            "auth_type": "key", "ssh_key": "~/.ssh/id_ed25519"}
    _PW = {"ssh_host": "example.com", "ssh_user": "u", "ssh_port": 22,
           "auth_type": "password"}

    @staticmethod
    def _proc(returncode=0, stderr=b""):
        if isinstance(stderr, str):
            stderr = stderr.encode("utf-8")
        return SimpleNamespace(returncode=returncode, stderr=stderr)

    def test_key_auth_success_full_argv(self):
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(0)) as run:
            result = ssh_launch.probe(self._KEY)
        self.assertEqual(result, {"ok": True})
        cmd = run.call_args[0][0]
        self.assertEqual(cmd, [
            "ssh", "-o", "BatchMode=yes", "-i", "~/.ssh/id_ed25519",
            "-o", "ConnectTimeout=5",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={host_key.KNOWN_HOSTS_PATH}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-p", "2222", "u@example.com", "true",
        ])
        kwargs = run.call_args[1]
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(kwargs["timeout"], ssh_launch.PROBE_TIMEOUT)
        self.assertEqual(kwargs["pass_fds"], ())

    def test_password_auth_success_uses_sshpass_fd(self):
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(0)) as run:
            result = ssh_launch.probe(self._PW, password="sekrit")
        self.assertEqual(result, {"ok": True})
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "sshpass")
        self.assertEqual(cmd[1], "-d")
        self.assertNotIn("sekrit", cmd)
        self.assertIn("NumberOfPasswordPrompts=1", cmd)
        self.assertNotIn("BatchMode=yes", cmd)
        self.assertTrue(run.call_args[1]["pass_fds"])

    def test_timeout_returns_chinese_phrase(self):
        with patch.object(ssh_launch.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("ssh", 15)):
            result = ssh_launch.probe(self._KEY)
        self.assertEqual(result, {"ok": False, "error": "连接超时"})

    def test_missing_binary_returns_oserror_phrase(self):
        with patch.object(ssh_launch.subprocess, "run",
                          side_effect=FileNotFoundError("ssh")):
            result = ssh_launch.probe(self._KEY)
        self.assertFalse(result["ok"])
        self.assertIn("无法启动 ssh", result["error"])
        self.assertNotIn("sshpass", result["error"])

    def test_missing_sshpass_mentions_password_hint(self):
        with patch.object(ssh_launch.subprocess, "run",
                          side_effect=FileNotFoundError("sshpass")):
            result = ssh_launch.probe(self._PW, password="sekrit")
        self.assertFalse(result["ok"])
        self.assertIn("sshpass", result["error"])

    def test_failure_stderr_is_classified(self):
        stderr = ("@@@@ REMOTE HOST IDENTIFICATION HAS CHANGED! @@@@\n"
                  "Host key verification failed.")
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(255, stderr)):
            result = ssh_launch.probe(self._KEY)
        self.assertFalse(result["ok"])
        self.assertIn("主机密钥已变更", result["error"])

    def test_none_stderr_degrades_gracefully(self):
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=SimpleNamespace(returncode=255,
                                                       stderr=None)):
            result = ssh_launch.probe(self._KEY)
        self.assertFalse(result["ok"])
        self.assertIn("未知错误", result["error"])

    def test_non_utf8_stderr_does_not_crash(self):
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(255, b"\xff\xfe broken")):
            result = ssh_launch.probe(self._KEY)
        self.assertFalse(result["ok"])

    def test_null_ssh_key_coerced_to_empty_string(self):
        """显式 ssh_key: null 不得让 None 进入 argv（probe 绝不抛异常）。"""
        t = dict(self._KEY, ssh_key=None)
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(0)) as run:
            result = ssh_launch.probe(t)
        self.assertEqual(result, {"ok": True})
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("-i") + 1], "")

    def test_null_ssh_key_coerced_in_tunnel_command(self):
        t = dict(self._KEY, ssh_key=None)
        sc = ssh_launch.build_tunnel_command(t, 1080)
        self.assertEqual(sc.cmd[sc.cmd.index("-i") + 1], "")

    def test_pipe_exhaustion_returns_oserror_phrase(self):
        """fd 耗尽（os.pipe OSError）不破「绝不抛异常」契约。"""
        with patch.object(ssh_launch.os, "pipe",
                          side_effect=OSError("too many open files")):
            result = ssh_launch.probe(self._PW, password="sekrit")
        self.assertFalse(result["ok"])
        self.assertIn("无法启动 ssh", result["error"])

    def test_write_failure_closes_read_fd_before_raise(self):
        """os.write 失败时 r_fd 不泄漏（异常仍按 OSError 分类）。"""
        real_pipe = os.pipe
        created = []

        def tracking_pipe():
            fds = real_pipe()
            created.extend(fds)
            return fds

        with patch.object(ssh_launch.os, "pipe", side_effect=tracking_pipe), \
             patch.object(ssh_launch.os, "write",
                          side_effect=OSError("disk full")):
            result = ssh_launch.probe(self._PW, password="sekrit")
        self.assertFalse(result["ok"])
        self.assertIn("无法启动 ssh", result["error"])
        r_fd, w_fd = created
        for fd in (r_fd, w_fd):
            with self.assertRaises(OSError):
                os.fstat(fd)  # 两端都已关闭


if __name__ == "__main__":
    unittest.main()
