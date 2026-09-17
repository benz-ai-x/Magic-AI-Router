"""Tests for mount/remote_setup — 远程 NFS 一键安装的纯逻辑面。

ssh 层全部 mock（run_remote 契约已由 test_ssh_launch_run_remote 钉死）；
这里钉：发行版解析、导出表内容（insecure 必须）、root 脚本组装
（shlex.quote 用户路径）、编排阶段（detect/install/apply）与幂等语义。
"""
import unittest
from unittest.mock import patch

from mount import remote_setup


def _ok(stdout="", stderr=""):
    return {"ok": True, "stdout": stdout, "stderr": stderr}


def _fail(error, stage_hint=""):
    return {"ok": False, "error": error, "stdout": "", "stderr": stage_hint}


def _os_release(id_line, id_like=""):
    return f'NAME="Test"\n{id_line}\nID_LIKE="{id_like}"\n'


class TestDetectDistro(unittest.TestCase):
    def test_debian_family(self):
        for ident in ("debian", "ubuntu", "linuxmint"):
            self.assertEqual(
                remote_setup.detect_distro(_os_release(f'ID="{ident}"')),
                "apt-get")

    def test_rhel_family(self):
        for ident in ("rhel", "rocky", "almalinux", "centos", "fedora", "ol"):
            self.assertEqual(
                remote_setup.detect_distro(_os_release(f'ID="{ident}"')),
                "dnf")

    def test_id_like_fallback(self):
        self.assertEqual(
            remote_setup.detect_distro(_os_release('ID="unknown"',
                                                   id_like="rhel fedora")),
            "dnf")

    def test_id_takes_priority_over_id_like(self):
        # ID=ubuntu + ID_LIKE=rhel 不可能出现在真实系统，但优先级契约要钉
        self.assertEqual(
            remote_setup.detect_distro(
                _os_release('ID="ubuntu"', id_like="rhel")),
            "apt-get")

    def test_unrecognized_returns_none(self):
        self.assertIsNone(
            remote_setup.detect_distro(_os_release('ID="arch"')))
        self.assertIsNone(remote_setup.detect_distro(""))
        self.assertIsNone(remote_setup.detect_distro(None))

    def test_unquoted_values_accepted(self):
        self.assertEqual(
            remote_setup.detect_distro("ID=rocky\n"), "dnf")


class TestExportsContent(unittest.TestCase):
    def test_insecure_is_mandatory(self):
        # sshd 端口转发源端口非特权——没有 insecure 服务器必拒
        content = remote_setup.exports_content(["/data"])
        self.assertIn("insecure", content)

    def test_loopback_only_client(self):
        content = remote_setup.exports_content(["/data"])
        self.assertIn(" 127.0.0.1(", content)
        self.assertNotIn("0.0.0.0", content)
        self.assertNotIn("*", content)

    def test_base_options(self):
        content = remote_setup.exports_content(["/data"])
        self.assertIn("rw,sync,no_subtree_check,insecure", content)
        self.assertNotIn("all_squash", content)
        self.assertEqual(content, "/data 127.0.0.1(rw,sync,no_subtree_check,insecure)\n")

    def test_squash_appends_anon_mapping(self):
        content = remote_setup.exports_content(["/data"], uid_gid=(1000, 1000))
        self.assertIn("all_squash,anonuid=1000,anongid=1000", content)

    def test_multiple_paths_one_line_each(self):
        content = remote_setup.exports_content(["/data", "/home/u/ws"])
        self.assertEqual(len(content.strip().splitlines()), 2)


class TestScripts(unittest.TestCase):
    def test_install_script_quotes_nothing_hostile(self):
        # 包名是白名单常量，但脚本形状要钉：set -e + install + enable + 标记
        script = remote_setup.install_script("apt-get")
        self.assertIn("set -e", script)
        self.assertIn("nfs-kernel-server", script)
        self.assertIn("MR_NFS_INSTALLED", script)
        script = remote_setup.install_script("dnf")
        self.assertIn("nfs-utils", script)

    def test_apply_script_quotes_user_paths(self):
        # 用户输入的远程路径必须 shlex.quote——路径含空格/元字符不炸脚本
        script = remote_setup.apply_script(
            ["/data with space; rm -rf /"], "content\n")
        self.assertIn(shlex_quote_ref("/data with space; rm -rf /"), script)
        self.assertNotIn("rm -rf", script.replace(
            shlex_quote_ref("/data with space; rm -rf /"), "<P>"))

    def test_apply_script_writes_app_owned_file(self):
        script = remote_setup.apply_script(["/data"], "x\n")
        self.assertIn(remote_setup.EXPORTS_FILE, script)
        self.assertIn("exportfs -ra", script)
        self.assertIn("MR_NFS_READY", script)
        self.assertIn("MR_NFS_NOT_LISTENING", script)

    def test_probe_body_needs_no_root(self):
        body = remote_setup._probe_body()
        self.assertNotIn("sudo", body)
        self.assertIn("2049", body)


def shlex_quote_ref(s):
    import shlex
    return shlex.quote(s)


class TestCheckRemote(unittest.TestCase):
    _T = {"ssh_host": "srv", "ssh_user": "u", "auth_type": "key"}

    def test_full_success_parse(self):
        stdout = ("MR_EXPORTFS\nMR_LISTENING\n"
                  "/data 127.0.0.1(rw,sync,no_subtree_check,insecure)\n")
        with patch.object(remote_setup.ssh_launch, "run_remote",
                          side_effect=[_ok(_os_release('ID="rocky"')),
                                       _ok(stdout)]):
            r = remote_setup.check_remote(self._T)
        self.assertTrue(r["ok"])
        self.assertEqual(r["family"], "dnf")
        self.assertTrue(r["installed"])
        self.assertTrue(r["listening"])
        self.assertIn("/data", r["exports"])

    def test_exports_after_exportfs_only_marker(self):
        # listening 缺席（服务装了没起）——导出表仍要读得到
        stdout = "MR_EXPORTFS\n/data 127.0.0.1(...)\n"
        with patch.object(remote_setup.ssh_launch, "run_remote",
                          side_effect=[_ok(_os_release('ID="ubuntu"')),
                                       _ok(stdout)]):
            r = remote_setup.check_remote(self._T)
        self.assertTrue(r["ok"])
        self.assertFalse(r["listening"])
        self.assertIn("/data", r["exports"])

    def test_ssh_failure_propagates_chinese_phrase(self):
        with patch.object(remote_setup.ssh_launch, "run_remote",
                          return_value=_fail("连接超时")):
            r = remote_setup.check_remote(self._T)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "连接超时")

    def test_unsupported_distro(self):
        with patch.object(remote_setup.ssh_launch, "run_remote",
                          return_value=_ok(_os_release('ID="arch"'))):
            r = remote_setup.check_remote(self._T)
        self.assertFalse(r["ok"])
        self.assertIn("暂不支持", r["error"])


class TestSetupRemote(unittest.TestCase):
    _T = {"ssh_host": "srv", "ssh_user": "u", "auth_type": "key"}
    _PROBE_OK = _ok(_os_release('ID="ubuntu"'))
    _STATUS_OK = _ok("MR_EXPORTFS\nMR_LISTENING\n")

    def test_no_mounts_rejected(self):
        r = remote_setup.setup_remote(self._T, [])
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "detect")

    def test_already_installed_skips_install(self):
        calls = []
        def runner(t, cmd, **kw):
            calls.append(cmd)
            return [self._PROBE_OK, self._STATUS_OK, _ok("MR_NFS_READY\n")][
                len(calls) - 1]
        with patch.object(remote_setup.ssh_launch, "run_remote",
                          side_effect=runner):
            r = remote_setup.setup_remote(self._T, ["/data"])
        self.assertTrue(r["ok"])
        self.assertFalse(r["installed"])  # 已装：本次未触发安装
        # 只有探测×2 + 应用（无安装调用）
        self.assertEqual(len(calls), 3)
        self.assertNotIn("apt-get install", calls[2])
        self.assertIn("exportfs -ra", calls[2])

    def test_fresh_install_runs_install_then_apply(self):
        not_installed = _ok("")  # 无 MR_EXPORTFS / MR_LISTENING
        calls = []
        def runner(t, cmd, **kw):
            calls.append(cmd)
            return [self._PROBE_OK, not_installed, _ok("MR_NFS_INSTALLED\n"),
                    _ok("MR_NFS_READY\n")][len(calls) - 1]
        with patch.object(remote_setup.ssh_launch, "run_remote",
                          side_effect=runner):
            r = remote_setup.setup_remote(self._T, ["/data"])
        self.assertTrue(r["ok"])
        self.assertTrue(r["installed"])  # 本次触发了安装
        self.assertEqual(len(calls), 4)
        self.assertIn("apt-get install", calls[2])

    def test_sudo_password_uses_stdin_form(self):
        calls = []
        def runner(t, cmd, **kw):
            calls.append((cmd, kw))
            return [self._PROBE_OK, self._STATUS_OK, _ok(), _ok()][
                len(calls) - 1]
        with patch.object(remote_setup.ssh_launch, "run_remote",
                          side_effect=runner):
            remote_setup.setup_remote(self._T, ["/data"],
                                      sudo_password="pw")
        # 安装/应用两次调用均带密码管道，且远程 sudo 形态为 -S（stdin 喂）
        for cmd, kw in calls[2:]:
            self.assertEqual(kw.get("sudo_password"), "pw")
            self.assertIn("sudo -S -p ''", cmd)

    def test_no_sudo_password_uses_nopasswd_form(self):
        calls = []
        def runner(t, cmd, **kw):
            calls.append((cmd, kw))
            return [self._PROBE_OK, self._STATUS_OK, _ok(), _ok()][
                len(calls) - 1]
        with patch.object(remote_setup.ssh_launch, "run_remote",
                          side_effect=runner):
            remote_setup.setup_remote(self._T, ["/data"])
        for cmd, kw in calls[2:]:
            self.assertFalse(kw.get("sudo_password"))  # 空 = 无密码管道
            self.assertIn("sudo -n", cmd)

    def test_install_failure_reports_stage(self):
        def runner(t, cmd, **kw):
            n = runner.n = getattr(runner, "n", 0) + 1
            return [self._PROBE_OK, _ok(""), _fail("远程 sudo 认证失败：密码错误")][n - 1]
        with patch.object(remote_setup.ssh_launch, "run_remote",
                          side_effect=runner):
            r = remote_setup.setup_remote(self._T, ["/data"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "install")
        self.assertIn("安装 NFS 服务失败", r["error"])

    def test_not_listening_failure_reported(self):
        def runner(t, cmd, **kw):
            n = runner.n = getattr(runner, "n", 0) + 1
            return [self._PROBE_OK, self._STATUS_OK,
                    _fail("远程命令超时（>300s）", "MR_NFS_NOT_LISTENING")][n - 1]
        with patch.object(remote_setup.ssh_launch, "run_remote",
                          side_effect=runner):
            r = remote_setup.setup_remote(self._T, ["/data"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["stage"], "apply")
        self.assertIn("未在 2049 监听", r["error"])

    def test_squash_fetches_uid_gid(self):
        calls = []
        def runner(t, cmd, **kw):
            calls.append(cmd)
            return [self._PROBE_OK, self._STATUS_OK, _ok("1000 1000\n"),
                    _ok("MR_NFS_READY\n")][len(calls) - 1]
        with patch.object(remote_setup.ssh_launch, "run_remote",
                          side_effect=runner):
            r = remote_setup.setup_remote(self._T, ["/data"],
                                          squash_to_ssh_user=True)
        self.assertTrue(r["ok"])
        self.assertTrue(r["squash"])
        self.assertIn("id -u; id -g", calls[2])

    def test_squash_degrades_gracefully_on_bad_output(self):
        def runner(t, cmd, **kw):
            n = runner.n = getattr(runner, "n", 0) + 1
            return [self._PROBE_OK, self._STATUS_OK, _ok("garbage"),
                    _ok("MR_NFS_READY\n")][n - 1]
        with patch.object(remote_setup.ssh_launch, "run_remote",
                          side_effect=runner):
            r = remote_setup.setup_remote(self._T, ["/data"],
                                          squash_to_ssh_user=True)
        self.assertTrue(r["ok"])
        self.assertFalse(r["squash"])  # uid 解析失败降级不 squash


if __name__ == "__main__":
    unittest.main()


class TestApplyScriptChown(unittest.TestCase):
    def test_squash_chowns_newly_created_dir(self):
        script = remote_setup.apply_script(
            ["/home/u/data"], "x\n", uid_gid=(1000, 1000))
        self.assertIn(
            "if [ ! -d /home/u/data ]; then mkdir -p /home/u/data "
            "&& chown 1000:1000 /home/u/data; fi", script)

    def test_no_squash_keeps_plain_mkdir(self):
        script = remote_setup.apply_script(["/data"], "x\n")
        self.assertIn("mkdir -p /data", script)
        self.assertNotIn("chown", script)

    def test_quoted_path_stays_safe_in_conditional(self):
        script = remote_setup.apply_script(
            ["/d s"], "x\n", uid_gid=(1000, 1000))
        self.assertIn(shlex_quote_ref("/d s"), script)
        self.assertNotIn("rm -rf", script)
