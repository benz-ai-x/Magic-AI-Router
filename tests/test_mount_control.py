"""Tests for mount/mount_control — 本地挂载控制的纯逻辑面。

subprocess/socket 全部 mock：mount 表解析、mount_nfs argv、卸载升级链、
sudoers 引导（base64 传输 + visudo 校验）在这里钉死；真机行为属 E2E。
"""
import os
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mount import mount_control


def _proc(returncode=0, stdout=b"", stderr=b""):
    def enc(v):
        return v.encode() if isinstance(v, str) else v
    return SimpleNamespace(returncode=returncode, stdout=enc(stdout),
                           stderr=enc(stderr))


_MOUNT_SAMPLE = "\n".join([
    "/dev/disk1s1 on / (apfs, local, journaled)",
    "map auto_home on /home (autofs, automounted, nobrowse)",
    "127.0.0.1:/data on /Volumes/magic-data (nfs, async, hard, mounted by pc)",
    "fileserver.local:/export on /Volumes/other (nfs)",
    "garbage line without parens",
    "/dev/disk2s1 on /Volumes/External (apfs, local)]",  # 括号在尾——fstype 非 nfs
])


class TestParseMounts(unittest.TestCase):
    def test_only_nfs_entries_kept(self):
        table = mount_control.parse_mounts(_MOUNT_SAMPLE)
        self.assertEqual(table, {
            "/Volumes/magic-data": "127.0.0.1:/data",
            "/Volumes/other": "fileserver.local:/export",
        })

    def test_relative_mountpoint_rejected(self):
        out = "weird spec on not-absolute (nfs)"
        self.assertEqual(mount_control.parse_mounts(out), {})

    def test_empty_and_none(self):
        self.assertEqual(mount_control.parse_mounts(""), {})
        self.assertEqual(mount_control.parse_mounts(None), {})

    def test_mountpoint_with_spaces(self):
        out = "127.0.0.1:/a on /Volumes/My Data (nfs)"
        table = mount_control.parse_mounts(out)
        self.assertEqual(table, {"/Volumes/My Data": "127.0.0.1:/a"})


class TestNfsMountsAndIsMounted(unittest.TestCase):
    def test_table_from_subprocess(self):
        with patch.object(mount_control.subprocess, "run",
                          return_value=_proc(0, _MOUNT_SAMPLE)):
            table = mount_control.nfs_mounts()
        self.assertIn("/Volumes/magic-data", table)

    def test_failure_folds_to_empty(self):
        for label, kwargs in (
                ("timeout", {"side_effect":
                             subprocess.TimeoutExpired("mount", 5)}),
                ("oserror", {"side_effect": OSError("nope")}),
                ("returncode", {"return_value": _proc(1)})):
            with self.subTest(label), \
                 patch.object(mount_control.subprocess, "run", **kwargs):
                self.assertEqual(mount_control.nfs_mounts(), {})

    def test_is_mounted_absolute_normalized(self):
        with patch.object(mount_control, "nfs_mounts",
                          return_value={"/Volumes/x": "s:/p"}):
            self.assertTrue(mount_control.is_mounted("/Volumes/x/"))
            self.assertFalse(mount_control.is_mounted("/Volumes/y"))


class TestProbeLocalPort(unittest.TestCase):
    def test_connect_success(self):
        import socket as _socket
        with patch.object(mount_control.socket, "socket") as factory:
            inst = factory.return_value
            self.assertTrue(mount_control.probe_local_port(12049))
            inst.connect.assert_called_once_with(("127.0.0.1", 12049))
            inst.close.assert_called_once()

    def test_connect_refused(self):
        with patch.object(mount_control.socket, "socket") as factory:
            factory.return_value.connect.side_effect = ConnectionRefusedError
            self.assertFalse(mount_control.probe_local_port(1))


class TestEnsureMountpoint(unittest.TestCase):
    def test_existing_dir_ok(self):
        ok, err = mount_control.ensure_mountpoint("/tmp")
        self.assertTrue(ok)

    def test_create_failure_returns_guidance(self):
        with patch.object(mount_control.os, "makedirs",
                          side_effect=PermissionError(13, "Permission denied")):
            ok, err = mount_control.ensure_mountpoint("/Volumes/x")
        self.assertFalse(ok)
        self.assertIn("无法创建挂载点", err)


class TestSudoers(unittest.TestCase):
    def test_rule_scoped_to_two_binaries(self):
        rule = mount_control.sudoers_rule(user="alice")
        self.assertEqual(
            rule,
            "alice ALL=(root) NOPASSWD: /sbin/mount_nfs, /sbin/umount\n")

    def test_check_ok_when_both_binaries_listed(self):
        listing = ("User alice may run the following commands:\n"
                   "(root) NOPASSWD: /sbin/mount_nfs, /sbin/umount\n")
        with patch.object(mount_control.subprocess, "run",
                          return_value=_proc(0, listing)):
            self.assertTrue(mount_control.check_sudoers())

    def test_check_fails_when_password_required(self):
        with patch.object(mount_control.subprocess, "run",
                          return_value=_proc(1, "", "sudo: a password is required")):
            self.assertFalse(mount_control.check_sudoers())

    def test_check_fails_when_rule_partial(self):
        listing = "(root) NOPASSWD: /sbin/mount_nfs\n"
        with patch.object(mount_control.subprocess, "run",
                          return_value=_proc(0, listing)):
            self.assertFalse(mount_control.check_sudoers())

    def test_install_skipped_when_rule_and_dirs_exist(self):
        with patch.object(mount_control, "check_sudoers", return_value=True), \
             patch.object(mount_control.subprocess, "run") as run:
            ok, err = mount_control.install_sudoers(["/tmp"])
        self.assertTrue(ok)
        run.assert_not_called()  # 规则在 + 目录在 → 绝不多弹授权

    def test_missing_dir_forces_admin_script_even_with_rule(self):
        """macOS 26 的 /Volumes root:wheel 0755——目录缺失必须走管理员
        脚本（规则重装幂等 + mkdir）；路径含空格时经 shlex.quote。"""
        with patch.object(mount_control, "check_sudoers",
                          side_effect=[True, True]), \
             patch.object(mount_control.subprocess, "run",
                          return_value=_proc(0)) as run:
            ok, err = mount_control.install_sudoers(["/Volumes/brand new"])
        self.assertTrue(ok)
        run.assert_called_once()
        script = run.call_args[0][0][2]
        self.assertIn("mkdir -p '/Volumes/brand new'", script)

    def test_admin_script_shape(self):
        import base64
        rule = "alice ALL=(root) NOPASSWD: /sbin/mount_nfs, /sbin/umount\n"
        script = mount_control._admin_script(rule, ["/Volumes/a b"])
        b64 = base64.b64encode(rule.encode()).decode()
        self.assertIn(f"echo {b64} | base64 -D", script)
        self.assertIn("visudo -cf", script)
        self.assertIn("chmod 0440", script)
        self.assertIn("mkdir -p '/Volumes/a b'", script)
        self.assertIn("with administrator privileges", script)
        inner = script.partition('shell script "')[2].partition('"')[0]
        self.assertNotIn('"', inner)  # 内层零双引号——AppleScript 字面量安全

    def test_install_user_cancel(self):
        with patch.object(mount_control, "check_sudoers",
                          side_effect=[False, False]), \
             patch.object(mount_control.subprocess, "run",
                          return_value=_proc(1, "", "33:84: execution error: "
                                          "User canceled. (-128)")):
            ok, err = mount_control.install_sudoers()
        self.assertFalse(ok)
        self.assertIn("取消", err)

    def test_install_success_verified(self):
        with patch.object(mount_control, "check_sudoers",
                          side_effect=[False, True]), \
             patch.object(mount_control.subprocess, "run",
                          return_value=_proc(0)):
            ok, err = mount_control.install_sudoers()
        self.assertTrue(ok)

    def test_install_write_but_verify_fails(self):
        with patch.object(mount_control, "check_sudoers",
                          side_effect=[False, False]), \
             patch.object(mount_control.subprocess, "run",
                          return_value=_proc(0)):
            ok, err = mount_control.install_sudoers()
        self.assertFalse(ok)
        self.assertIn("校验未通过", err)


class TestMountNfs(unittest.TestCase):
    def test_argv_v4_hard_local_port(self):
        with patch.object(mount_control, "is_mounted",
                          side_effect=[False, True]), \
             patch.object(mount_control.subprocess, "run",
                          return_value=_proc(0)) as run:
            r = mount_control.mount_nfs(12049, "/data", "/Volumes/magic-data")
        self.assertTrue(r["ok"])
        argv = run.call_args[0][0]
        self.assertEqual(argv[:3], ["sudo", "-n", "/sbin/mount_nfs"])
        opts = argv[argv.index("-o") + 1]
        self.assertEqual(opts, "vers=4,port=12049,tcp,hard")
        self.assertEqual(argv[-2:], ["127.0.0.1:/data",
                                     os.path.abspath("/Volumes/magic-data")])

    def test_idempotent_when_already_mounted(self):
        with patch.object(mount_control, "is_mounted", return_value=True), \
             patch.object(mount_control.subprocess, "run") as run:
            r = mount_control.mount_nfs(1, "/data", "/Volumes/x")
        self.assertTrue(r["ok"])
        run.assert_not_called()

    def test_returncode_zero_but_not_in_table_is_failure(self):
        with patch.object(mount_control, "is_mounted", return_value=False), \
             patch.object(mount_control.subprocess, "run",
                          return_value=_proc(0, "", "")):
            r = mount_control.mount_nfs(1, "/data", "/Volumes/x")
        self.assertFalse(r["ok"])

    def test_timeout_classified(self):
        with patch.object(mount_control, "is_mounted", return_value=False), \
             patch.object(mount_control.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("sudo", 20)):
            r = mount_control.mount_nfs(1, "/data", "/Volumes/x")
        self.assertFalse(r["ok"])
        self.assertIn("超时", r["error"])

    def test_password_required_classified(self):
        with patch.object(mount_control, "is_mounted", return_value=False), \
             patch.object(mount_control.subprocess, "run",
                          return_value=_proc(1, "",
                                             "sudo: a password is required")):
            r = mount_control.mount_nfs(1, "/data", "/Volumes/x")
        self.assertFalse(r["ok"])
        self.assertIn("管理员授权", r["error"])

    def test_generic_stderr_first_line(self):
        with patch.object(mount_control, "is_mounted", return_value=False), \
             patch.object(mount_control.subprocess, "run",
                          return_value=_proc(1, "",
                                             "mount_nfs: /data: Permission "
                                             "denied\nsecond line")):
            r = mount_control.mount_nfs(1, "/data", "/Volumes/x")
        self.assertIn("Permission denied", r["error"])
        self.assertNotIn("second line", r["error"])


class TestUnmount(unittest.TestCase):
    def test_idempotent_when_not_mounted(self):
        with patch.object(mount_control, "is_mounted", return_value=False), \
             patch.object(mount_control.subprocess, "run") as run:
            r = mount_control.unmount("/Volumes/x")
        self.assertTrue(r["ok"])
        run.assert_not_called()

    def test_unmount_verified_by_table(self):
        states = iter([True, False])
        with patch.object(mount_control, "is_mounted",
                          side_effect=lambda d: next(states)), \
             patch.object(mount_control.subprocess, "run",
                          return_value=_proc(0)) as run:
            r = mount_control.unmount("/Volumes/x")
        self.assertTrue(r["ok"])
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["sudo", "-n", "/sbin/umount",
                                os.path.abspath("/Volumes/x")])

    def test_force_unmount_escalation_chain(self):
        # 普通卸载失败 → -f 失败 → diskutil 成功（is_mounted 共 5 次回读）
        states = iter([True, True, True, True, False])
        calls = []
        with patch.object(mount_control, "is_mounted",
                          side_effect=lambda d: next(states)), \
             patch.object(mount_control.subprocess, "run",
                          side_effect=lambda argv, **kw:
                          calls.append(argv) or _proc(0)):
            r = mount_control.force_unmount("/Volumes/x")
        self.assertTrue(r["ok"])
        self.assertEqual(calls[0][:3], ["sudo", "-n", "/sbin/umount"])
        self.assertEqual(calls[1], ["sudo", "-n", "/sbin/umount", "-f",
                                    os.path.abspath("/Volumes/x")])
        self.assertEqual(calls[2][:2], ["diskutil", "unmount"])

    def test_force_unmount_all_fail_reports_manual_command(self):
        with patch.object(mount_control, "is_mounted", return_value=True), \
             patch.object(mount_control.subprocess, "run",
                          return_value=_proc(1)):
            r = mount_control.force_unmount("/Volumes/x")
        self.assertFalse(r["ok"])
        self.assertIn("sudo umount -f", r["error"])


if __name__ == "__main__":
    unittest.main()
