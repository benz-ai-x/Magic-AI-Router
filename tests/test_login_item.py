"""Tests for login_item.set_launch_at_login (LaunchAgent implementation).

Exercises the real plistlib output via a temp dir; only launchctl is mocked.
"""
import os
import plistlib
import tempfile
import unittest
from unittest.mock import patch

from sysctl import login_item
class TestLaunchAtLoginLaunchAgent(unittest.TestCase):
    def setUp(self):
        self._orig_frozen = login_item.FROZEN

    def tearDown(self):
        login_item.FROZEN = self._orig_frozen

    def test_dev_mode_rejected_up_front(self):
        login_item.FROZEN = False
        ok, err = login_item.set_launch_at_login(True)
        self.assertFalse(ok)
        self.assertIn("开发模式", err)

    def test_missing_executable_rejected(self):
        login_item.FROZEN = True
        with tempfile.TemporaryDirectory():
            with patch.object(login_item.sys, "executable", "/nonexistent/exe"), \
                 patch.object(login_item.os.path, "exists", return_value=False):
                ok, err = login_item.set_launch_at_login(True)
        self.assertFalse(ok)
        self.assertIn("无法定位", err)

    def test_enable_writes_valid_plist_without_loading(self):
        login_item.FROZEN = True
        exe = "/Applications/Magic AI Router.app/Contents/MacOS/Magic AI Router"
        with tempfile.TemporaryDirectory() as d:
            plist_path = os.path.join(d, "login.plist")
            with patch.object(login_item.sys, "executable", exe), \
                 patch.object(login_item.os.path, "exists", return_value=True), \
                 patch.object(login_item, "_plist_path", return_value=plist_path), \
                 patch.object(login_item.subprocess, "run") as run:
                ok, err = login_item.set_launch_at_login(True)
            self.assertTrue(ok)
            self.assertEqual(err, "")
            # Real plistlib output on disk:
            with open(plist_path, "rb") as f:
                pl = plistlib.load(f)
            self.assertEqual(pl["Label"], login_item.LABEL)
            self.assertEqual(pl["ProgramArguments"], [exe])
            self.assertIs(pl["RunAtLoad"], True)
            self.assertIs(pl["KeepAlive"], False)
            # Enable must NOT launchctl load immediately (the app is already
            # running; launchd handles it at next login).
            self.assertEqual(run.call_count, 0)

    def test_enable_writes_via_atomic_write(self):
        """#40: the plist must be staged atomically — a crash mid-write
        must not leave a corrupt plist behind."""
        login_item.FROZEN = True
        with patch.object(login_item.sys, "executable", "/x"), \
             patch.object(login_item.os.path, "exists", return_value=True), \
             patch.object(login_item, "_plist_path", return_value="/tmp/x.plist"), \
             patch.object(login_item.config_store, "atomic_write",
                          return_value=True) as aw:
            ok, err = login_item.set_launch_at_login(True)
        self.assertTrue(ok)
        self.assertEqual(err, "")
        aw.assert_called_once()
        path, xml = aw.call_args[0]
        self.assertEqual(path, "/tmp/x.plist")
        pl = plistlib.loads(xml.encode("utf-8"))
        self.assertEqual(pl["Label"], login_item.LABEL)
        self.assertEqual(pl["ProgramArguments"], ["/x"])
        self.assertIs(pl["RunAtLoad"], True)

    def test_atomic_write_failure_reported(self):
        login_item.FROZEN = True
        with patch.object(login_item.sys, "executable", "/x"), \
             patch.object(login_item.os.path, "exists", return_value=True), \
             patch.object(login_item, "_plist_path", return_value="/tmp/x.plist"), \
             patch.object(login_item.config_store, "atomic_write",
                          return_value=False):
            ok, err = login_item.set_launch_at_login(True)
        self.assertFalse(ok)
        self.assertIn("写入登录项失败", err)

    def test_disable_unloads_and_removes_plist(self):
        login_item.FROZEN = True
        with tempfile.TemporaryDirectory() as d:
            plist_path = os.path.join(d, "login.plist")
            with open(plist_path, "wb") as f:
                plistlib.dump({"Label": login_item.LABEL}, f)
            with patch.object(login_item.sys, "executable", "/x"), \
                 patch.object(login_item.os.path, "exists", return_value=True), \
                 patch.object(login_item, "_plist_path", return_value=plist_path), \
                 patch.object(login_item.subprocess, "run") as run:
                ok, err = login_item.set_launch_at_login(False)
            self.assertTrue(ok)
            self.assertEqual(err, "")
            self.assertFalse(os.path.exists(plist_path))  # file removed
            run.assert_called_once_with(
                ["launchctl", "unload", plist_path], capture_output=True,
            )


if __name__ == "__main__":
    unittest.main()
