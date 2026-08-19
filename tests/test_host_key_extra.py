"""Tests for host_key.py — inspect, accept, replace with mocked subprocess."""
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from tunnel import host_key
_TUNNEL = {"ssh_host": "srv", "ssh_port": 22, "ssh_user": "u"}


class TestInspect(unittest.TestCase):
    @patch("tunnel.host_key.subprocess.run")
    def test_known_host_returns_known_true(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="srv ssh-rsa AAAA...\n")
        # inspect 的分支取决于 known_hosts 是否存在——固定到临时文件，
        # 不能依赖开发机真实的 ~/.magic-proxy/known_hosts（CI 上不存在）。
        with tempfile.TemporaryDirectory() as d:
            kh = os.path.join(d, "known_hosts")
            open(kh, "w").close()
            with patch("tunnel.host_key.APP_SECURITY_DIR", d), \
                 patch("tunnel.host_key.KNOWN_HOSTS_PATH", kh):
                known, keys, fps, err = host_key.inspect(_TUNNEL)
        self.assertTrue(known)
        self.assertEqual(err, "")

    @patch("tunnel.host_key.subprocess.run")
    def test_scan_unknown_host_returns_keys_and_fingerprints(self, mock_run):
        # known_hosts 存在但 keygen -F 未命中 → 依次 keyscan / fingerprint
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr=""),  # not found
            MagicMock(returncode=0, stdout="srv ssh-rsa AAAA...\n", stderr=""),  # keyscan
            MagicMock(returncode=0, stdout="256 SHA256:abc... srv (ECDSA)\n", stderr=""),  # fingerprint
        ]
        with tempfile.TemporaryDirectory() as d:
            kh = os.path.join(d, "known_hosts")
            open(kh, "w").close()
            with patch("tunnel.host_key.APP_SECURITY_DIR", d), \
                 patch("tunnel.host_key.KNOWN_HOSTS_PATH", kh):
                known, keys, fps, err = host_key.inspect(_TUNNEL)
        self.assertFalse(known)
        self.assertEqual(err, "")
        self.assertIn("AAAA", keys)
        self.assertIn("SHA256", fps)

    def test_no_host_returns_error(self):
        known, keys, fps, err = host_key.inspect({"ssh_host": ""})
        self.assertFalse(known)
        self.assertIn("主机", err)


class TestAccept(unittest.TestCase):
    def test_empty_keys_returns_false(self):
        self.assertFalse(host_key.accept(""))


class TestReplace(unittest.TestCase):
    def test_no_host_returns_false(self):
        result = host_key.replace({"ssh_host": ""}, "keys")
        self.assertFalse(result)


class TestValidateHost(unittest.TestCase):
    def test_valid_host_passes(self):
        host_key._validate_host("example.com")  # should not raise

    def test_invalid_chars_rejected(self):
        with self.assertRaises(ValueError):
            host_key._validate_host("host;rm -rf")
