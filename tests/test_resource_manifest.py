"""资源清单一致性（issue #14）：manifest 是 dev/build/smoke 的单一真源.

守卫三件套：
- 所有 manifest src 真实存在；
- 所有 resource_path 消费项被 manifest 覆盖（无漏项无幽灵）；
- bundle 校验函数能逐条核验 dest 文件存在。
"""
import unittest
from pathlib import Path

from tools.resource_manifest import RESOURCE_MANIFEST, RESOURCE_NAMES

ROOT = Path(__file__).resolve().parents[1]


class TestManifestCoversResources(unittest.TestCase):
    def test_all_manifest_sources_exist(self):
        missing = [s for s, _ in RESOURCE_MANIFEST
                   if not (ROOT / s).is_file()]
        self.assertEqual(missing, [])

    def test_every_resource_path_consumer_covered(self):
        import re
        consumed = set()
        for py in list(ROOT.glob("*.py")) + list(ROOT.glob("capture/*.py")) \
                + list(ROOT.glob("services/*.py")) + list(ROOT.glob("shellui/*.py")) \
                + list(ROOT.glob("tunnel/*.py")) + list(ROOT.glob("sysctl/*.py")) \
                + list(ROOT.glob("app.py", )):
            text = py.read_text(encoding="utf-8", errors="ignore")
            for m in re.finditer(r'resource_path\("([a-zA-Z0-9_/. -]+\.(?:py|html|md|png|yaml))"',
                                 text):
                consumed.add(m.group(1))
        uncovered = consumed - set(RESOURCE_NAMES)
        self.assertEqual(uncovered, set(),
                         f"resource_path 消费了未列入 manifest 的资源: {uncovered}")

    def test_suanpan_example_in_manifest(self):
        self.assertIn("suanpan.example.yaml", RESOURCE_NAMES)

    def test_bundle_verifier(self):
        from tools.resource_manifest import verify_bundle
        import tempfile
        with tempfile.TemporaryDirectory() as bundle:
            for src, dest in RESOURCE_MANIFEST:
                name = src.rsplit("/", 1)[-1]
                target = Path(bundle) / dest / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("x")
            ok, missing = verify_bundle(bundle)
        self.assertTrue(ok)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()


class TestBuildScriptCoverage(unittest.TestCase):
    """build.sh 的 --add-data 必须覆盖 manifest（单一真源驱动构建）。"""

    def test_build_script_covers_manifest(self):
        from tools.resource_manifest import RESOURCE_MANIFEST, RUNTIME_MODULES
        build = (ROOT / "build.sh").read_text()
        missing = []
        for src, _ in RESOURCE_MANIFEST + [(m, ".") for m in RUNTIME_MODULES]:
            if src == "app.py":
                continue  # app.py 是 PyInstaller 入口参数，不走 add-data
            if f'--add-data "{src}:."' not in build:
                missing.append(src)
        self.assertEqual(missing, [],
                         f"build.sh 未按 manifest 打包: {missing}")

    def test_runtime_modules_exist(self):
        from tools.resource_manifest import RUNTIME_MODULES
        missing = [m for m in RUNTIME_MODULES if not (ROOT / m).is_file()]
        self.assertEqual(missing, [])
