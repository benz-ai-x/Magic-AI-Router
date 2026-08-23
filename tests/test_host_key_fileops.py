"""Tests for host_key.py — accept + replace with real temp files."""
import os
import tempfile
import unittest
from unittest.mock import patch

from tunnel import host_key
class TestHostKeyAcceptWithFiles(unittest.TestCase):
    def test_accept_writes_keys_to_known_hosts(self):
        with tempfile.TemporaryDirectory() as d:
            sec_dir = os.path.join(d, "security")
            os.makedirs(sec_dir)
            kh_path = os.path.join(sec_dir, "known_hosts")
            with patch("tunnel.host_key.APP_SECURITY_DIR", sec_dir), \
                 patch("tunnel.host_key.KNOWN_HOSTS_PATH", kh_path):
                result = host_key.accept("[test]:22 ssh-rsa AAAATEST\n")
            self.assertTrue(result)
            with open(kh_path) as f:
                content = f.read()
            self.assertIn("AAAATEST", content)

    def test_accept_appends_to_existing(self):
        with tempfile.TemporaryDirectory() as d:
            sec_dir = os.path.join(d, "security")
            os.makedirs(sec_dir)
            kh_path = os.path.join(sec_dir, "known_hosts")
            with open(kh_path, "w") as f:
                f.write("existing line\n")
            with patch("tunnel.host_key.APP_SECURITY_DIR", sec_dir), \
                 patch("tunnel.host_key.KNOWN_HOSTS_PATH", kh_path):
                host_key.accept("[test]:22 ssh-rsa NEWKEY\n")
            with open(kh_path) as f:
                lines = f.readlines()
            self.assertEqual(len(lines), 2)
            self.assertIn("existing", lines[0])
            self.assertIn("NEWKEY", lines[1])


class TestHostKeyReplaceWithFiles(unittest.TestCase):
    def test_replace_adds_new_keys(self):
        with tempfile.TemporaryDirectory() as d:
            sec_dir = os.path.join(d, "security")
            os.makedirs(sec_dir)
            kh_path = os.path.join(sec_dir, "known_hosts")
            with open(kh_path, "w") as f:
                f.write("[other]:22 ssh-rsa OTHERKEY\n")
            with patch("tunnel.host_key.APP_SECURITY_DIR", sec_dir), \
                 patch("tunnel.host_key.KNOWN_HOSTS_PATH", kh_path):
                result = host_key.replace(
                    {"ssh_host": "srv", "ssh_port": 22},
                    "[srv]:22 ssh-rsa NEWKEY\n")
            self.assertTrue(result)
            with open(kh_path) as f:
                content = f.read()
            self.assertIn("NEWKEY", content)
            self.assertIn("OTHERKEY", content)

    def test_replace_creates_file_if_missing(self):
        with tempfile.TemporaryDirectory() as d:
            sec_dir = os.path.join(d, "security")
            os.makedirs(sec_dir)
            kh_path = os.path.join(sec_dir, "known_hosts")
            with patch("tunnel.host_key.APP_SECURITY_DIR", sec_dir), \
                 patch("tunnel.host_key.KNOWN_HOSTS_PATH", kh_path):
                result = host_key.replace(
                    {"ssh_host": "newhost", "ssh_port": 22},
                    "[newhost]:22 ssh-rsa NEWKEY\n")
            self.assertTrue(result)
            with open(kh_path) as f:
                content = f.read()
            self.assertIn("NEWKEY", content)


class TestHostKeyEnsureStorage(unittest.TestCase):
    def test_ensure_storage_creates_dir(self):
        with tempfile.TemporaryDirectory() as d:
            sec_dir = os.path.join(d, "newsec")
            with patch("tunnel.host_key.APP_SECURITY_DIR", sec_dir), \
                 patch("tunnel.host_key.KNOWN_HOSTS_PATH", os.path.join(sec_dir, "known_hosts")):
                host_key._ensure_storage()
            self.assertTrue(os.path.isdir(sec_dir))


class TestHostKeyInspectDetailed(unittest.TestCase):
    def setUp(self):
        # inspect 的分支取决于 known_hosts 是否存在——固定到临时文件，
        # 不能依赖开发机真实的 ~/.magic-proxy/known_hosts（CI 上不存在）。
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        kh = os.path.join(self._tmp.name, "known_hosts")
        open(kh, "w").close()
        self._dir_patch = patch("tunnel.host_key.APP_SECURITY_DIR", self._tmp.name)
        self._kh_patch = patch("tunnel.host_key.KNOWN_HOSTS_PATH", kh)
        self._dir_patch.start()
        self._kh_patch.start()
        self.addCleanup(self._kh_patch.stop)
        self.addCleanup(self._dir_patch.stop)

    @patch("tunnel.host_key.subprocess.run")
    def test_inspect_known_host(self, mock_run):
        mock_run.return_value = type("R", (), {
            "returncode": 0, "stdout": "[srv]:22 ssh-rsa AAAA\n", "stderr": ""
        })()
        known, keys, fps, err = host_key.inspect({"ssh_host": "srv", "ssh_port": 22})
        self.assertTrue(known)

    @patch("tunnel.host_key.subprocess.run")
    def test_inspect_scan_error(self, mock_run):
        mock_run.side_effect = [
            type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})(),  # not found
            OSError("ssh-keyscan not found"),
        ]
        known, keys, fps, err = host_key.inspect({"ssh_host": "srv", "ssh_port": 22})
        self.assertFalse(known)
        self.assertIn("扫描", err)

    @patch("tunnel.host_key.subprocess.run")
    def test_inspect_fingerprint_error(self, mock_run):
        mock_run.side_effect = [
            type("R", (), {"returncode": 1, "stdout": "", "stderr": ""})(),  # not found
            type("R", (), {"returncode": 0, "stdout": "srv ssh-rsa AAAA\n", "stderr": ""})(),  # keyscan
            type("R", (), {"returncode": 1, "stdout": "", "stderr": "fingerprint error"})(),  # fp fail
        ]
        known, keys, fps, err = host_key.inspect({"ssh_host": "srv", "ssh_port": 22})
        self.assertFalse(known)
        self.assertIn("fingerprint", err.lower())


class TestReplaceNewlineGuard(unittest.TestCase):
    """#69 R7：手编 known_hosts 末行缺 \n 时，新 key 不得胶接到旧行
    （否则刚验证过指纹的新 key 不可达）。"""

    def test_missing_trailing_newline_no_glue(self):
        import tempfile
        import os
        from unittest.mock import patch
        from tunnel import host_key
        with tempfile.TemporaryDirectory() as d:
            kh = os.path.join(d, "known_hosts")
            with open(kh, "w") as f:
                f.write("oldhost ssh-ed25519 AAAA_OLD")  # 无末行换行
            # replace 用 APP_SECURITY_DIR 锁 + KNOWN_HOSTS_PATH 常量——
            # 两者都 patch 到临时目录
            with patch.object(host_key, "KNOWN_HOSTS_PATH", kh), \
                 patch.object(host_key, "APP_SECURITY_DIR", d):
                ok = host_key.replace(
                    {"ssh_host": "newhost", "ssh_port": 22},
                    "newhost ssh-ed25519 AAAA_NEW")
            self.assertTrue(ok)
            lines = open(kh).read().splitlines()
            # 新 key 独占一行（不胶接到 oldhost 行尾）
            self.assertTrue(any(ln.startswith("newhost ssh") for ln in lines),
                            lines)
