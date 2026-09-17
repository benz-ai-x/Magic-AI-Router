"""Tests for tunnel/ssh_launch.run_remote — 一次性远程命令执行。

策略面钉死：argv 与探针同源（host-key 三件套 + 认证注入）、sudo 密码
只走 stdin 管道（argv 永不出现）、sshpass pty 回显在返回前 scrub、
失败走中文分类表——契约「绝不抛异常」无条件成立。
"""
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tunnel import host_key, ssh_launch


class TestRunRemote(unittest.TestCase):
    _KEY = {"ssh_host": "example.com", "ssh_user": "u", "ssh_port": 2222,
            "auth_type": "key", "ssh_key": "~/.ssh/id_ed25519"}
    _PW = {"ssh_host": "example.com", "ssh_user": "u", "ssh_port": 22,
           "auth_type": "password"}

    @staticmethod
    def _proc(returncode=0, stderr=b"", stdout=b""):
        def enc(v):
            return v.encode("utf-8") if isinstance(v, str) else v
        return SimpleNamespace(returncode=returncode, stderr=enc(stderr),
                               stdout=enc(stdout))

    def test_key_auth_success_full_argv(self):
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(0, stdout="ok")) as run:
            result = ssh_launch.run_remote(self._KEY, "cat /etc/os-release")
        self.assertTrue(result["ok"])
        self.assertEqual(result["stdout"], "ok")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd, [
            "ssh", "-o", "BatchMode=yes", "-i", "~/.ssh/id_ed25519",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=yes",
            "-o", f"UserKnownHostsFile={host_key.KNOWN_HOSTS_PATH}",
            "-o", "GlobalKnownHostsFile=/dev/null",
            "-p", "2222", "u@example.com", "cat /etc/os-release",
        ])
        kwargs = run.call_args[1]
        self.assertTrue(kwargs["capture_output"])
        self.assertEqual(kwargs["timeout"], ssh_launch.REMOTE_TIMEOUT)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertNotIn("input", kwargs)

    def test_sudo_password_flows_via_stdin_only(self):
        """sudo 密码经 input= 管道传递；argv/本地命令行永不出现。"""
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(0)) as run:
            result = ssh_launch.run_remote(
                self._KEY, "sudo -S -p '' id", sudo_password="sudopw")
        self.assertTrue(result["ok"])
        cmd = run.call_args[0][0]
        self.assertNotIn("sudopw", cmd)
        self.assertNotIn("sudopw", " ".join(cmd))
        kwargs = run.call_args[1]
        self.assertEqual(kwargs["input"], b"sudopw\n")
        self.assertNotIn("stdin", kwargs)

    def test_password_auth_uses_sshpass_fd(self):
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(0)) as run:
            result = ssh_launch.run_remote(self._PW, "true", password="sekrit",
                                           sudo_password="sudopw")
        self.assertTrue(result["ok"])
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], "sshpass")
        self.assertNotIn("sekrit", cmd)
        self.assertNotIn("sudopw", cmd)
        self.assertIn("NumberOfPasswordPrompts=1", cmd)
        self.assertNotIn("BatchMode=yes", cmd)

    def test_pty_echo_scrubbed_from_output(self):
        """sshpass 的 pty 会回显 stdin——返回前 scrub，密码绝不回流。"""
        echoed = "sudopw\nuid=0(root) gid=0(root)\n"
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(0, stdout=echoed)):
            result = ssh_launch.run_remote(
                self._PW, "sudo -S -p '' id", password="sekrit",
                sudo_password="sudopw")
        self.assertTrue(result["ok"])
        self.assertNotIn("sudopw", result["stdout"])
        self.assertIn("***", result["stdout"])

    def test_scrub_applies_to_stderr_too(self):
        stderr = "sudo: sudopw: 1 incorrect password attempt"
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(1, stderr=stderr)):
            result = ssh_launch.run_remote(
                self._KEY, "sudo -S -p '' id", sudo_password="sudopw")
        self.assertFalse(result["ok"])
        self.assertNotIn("sudopw", result["stderr"])

    def test_wrong_sudo_password_classified(self):
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(
                              1, stderr="sudo: 1 incorrect password attempt")):
            result = ssh_launch.run_remote(
                self._KEY, "sudo -S -p '' id", sudo_password="bad")
        self.assertFalse(result["ok"])
        self.assertIn("密码错误", result["error"])

    def test_sudo_password_required_classified(self):
        """`sudo -n`（无密码管道）失败须与「密码错误」区分。"""
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(
                              1, stderr="sudo: a password is required")):
            result = ssh_launch.run_remote(self._KEY, "sudo -n id")
        self.assertFalse(result["ok"])
        self.assertIn("需要密码", result["error"])

    def test_not_in_sudoers_classified(self):
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(
                              1, stderr="u is not in the sudoers file.")):
            result = ssh_launch.run_remote(self._KEY, "sudo -n id")
        self.assertFalse(result["ok"])
        self.assertIn("sudo 权限", result["error"])

    def test_timeout_returns_chinese_phrase(self):
        with patch.object(ssh_launch.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("ssh", 60)):
            result = ssh_launch.run_remote(self._KEY, "sleep 300",
                                           timeout=60)
        self.assertFalse(result["ok"])
        self.assertIn("超时", result["error"])
        self.assertIn("60", result["error"])

    def test_oserror_returns_phrase_with_sshpass_hint(self):
        with patch.object(ssh_launch.subprocess, "run",
                          side_effect=FileNotFoundError("sshpass")):
            result = ssh_launch.run_remote(self._PW, "true", password="x")
        self.assertFalse(result["ok"])
        self.assertIn("无法启动 ssh", result["error"])
        self.assertIn("sshpass", result["error"])

    def test_ssh_layer_failure_uses_failure_table(self):
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(
                              255, "Permission denied (publickey).")):
            result = ssh_launch.run_remote(self._KEY, "true")
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "认证失败：密钥或密码被拒绝")

    def test_custom_timeout_forwarded(self):
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(0)) as run:
            ssh_launch.run_remote(self._KEY, "apt-get install …", timeout=300)
        self.assertEqual(run.call_args[1]["timeout"], 300)

    def test_stdout_preserved_on_failure_for_debugging(self):
        with patch.object(ssh_launch.subprocess, "run",
                          return_value=self._proc(1, stdout="partial")):
            result = ssh_launch.run_remote(self._KEY, "false")
        self.assertFalse(result["ok"])
        self.assertEqual(result["stdout"], "partial")


if __name__ == "__main__":
    unittest.main()
