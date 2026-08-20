"""Capture resources contract (issue #2): 抓包模式的资源契约.

Seam S1 —— resolve_capture_resources 是控制器唯一的资源入口：
mitmdump 二进制、addon 脚本、抓包目录在此解析并验证，控制器不再
自行拼接文件名（"capture.ai_capture_addon.py" 事故的根治）。
"""
import os
import types
import unittest
from unittest.mock import patch

from capture.resources import (
    ADDON_RESOURCE_NAME as ADDON_FLAT,
    CaptureResources,
    CaptureResourcesError,
    resolve_capture_resources,
)


class TestDevAddonResolution(unittest.TestCase):
    def test_dev_mode_resolves_real_flat_addon(self):
        with patch.dict(os.environ, {"MAGIC_PROXY_MITMDUMP_BIN": "/usr/bin/true"}):
            res = resolve_capture_resources({})
        self.assertIsInstance(res, CaptureResources)
        self.assertEqual(os.path.basename(res.addon_path), "ai_capture_addon.py")
        self.assertTrue(os.path.isfile(res.addon_path), res.addon_path)
        self.assertTrue(os.access(res.addon_path, os.R_OK))
        self.assertEqual(res.mitmdump_bin, "/usr/bin/true")


if __name__ == "__main__":
    unittest.main()


class TestMitmdumpResolutionChain(unittest.TestCase):
    """env 覆盖 → frozen bundled → PATH 三级链（继承原 resolve_mitmdump_bin）。"""

    def test_path_resolved_when_no_env_no_bundle(self):
        with patch.dict(os.environ, {}, clear=False), \
             patch("capture.resources.shutil.which", return_value="/usr/bin/true") as which:
            os.environ.pop("MAGIC_PROXY_MITMDUMP_BIN", None)
            res = resolve_capture_resources({})
        which.assert_called_once_with("mitmdump")
        self.assertEqual(res.mitmdump_bin, "/usr/bin/true")

    def test_frozen_bundled_bin_preferred_over_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as meip:
            bundled_dir = os.path.join(meip, "mitmdump")
            os.makedirs(bundled_dir)
            bin_path = os.path.join(bundled_dir, "mitmdump")
            open(bin_path, "w").close()
            addon = os.path.join(meip, ADDON_FLAT)
            open(addon, "w").close()
            fake = types.SimpleNamespace(_MEIPASS=meip)
            with patch.dict(os.environ, {}, clear=False), \
                 patch("capture.resources.sys", fake), \
                 patch("util.sys", fake), \
                 patch("capture.resources.shutil.which") as which:
                os.environ.pop("MAGIC_PROXY_MITMDUMP_BIN", None)
                res = resolve_capture_resources({})
            which.assert_not_called()
            self.assertEqual(res.mitmdump_bin, bin_path)
            self.assertEqual(res.addon_path, addon)

    def test_none_when_frozen_bundle_missing_and_no_env(self):
        import tempfile
        with tempfile.TemporaryDirectory() as meip:
            addon = os.path.join(meip, ADDON_FLAT)
            open(addon, "w").close()
            fake = types.SimpleNamespace(_MEIPASS=meip)
            with patch.dict(os.environ, {}, clear=False), \
                 patch("capture.resources.sys", fake), \
                 patch("util.sys", fake), \
                 patch("capture.resources.shutil.which", return_value=None):
                os.environ.pop("MAGIC_PROXY_MITMDUMP_BIN", None)
                with self.assertRaises(CaptureResourcesError) as ctx:
                    resolve_capture_resources({})
        self.assertIn("mitmdump", ctx.exception.msg)


class TestMissingAddon(unittest.TestCase):
    def test_frozen_without_addon_fails_with_actionable_error(self):
        import tempfile
        with tempfile.TemporaryDirectory() as meip:
            os.makedirs(os.path.join(meip, "mitmdump"))
            open(os.path.join(meip, "mitmdump", "mitmdump"), "w").close()
            fake = types.SimpleNamespace(_MEIPASS=meip)
            with patch.dict(os.environ, {}, clear=False), \
                 patch("capture.resources.sys", fake), \
                 patch("util.sys", fake):
                os.environ.pop("MAGIC_PROXY_MITMDUMP_BIN", None)
                with self.assertRaises(CaptureResourcesError) as ctx:
                    resolve_capture_resources({})
        self.assertIn("抓包组件", ctx.exception.msg)
        self.assertIn("ai_capture_addon.py", ctx.exception.msg)
