"""frozen 扁平世界契约——addon 子进程的 import 链在无包层级下必须可活。

frozen 形态里 mitmdump 子进程按 Resources/ 平铺布局（--add-data dest="."）
加载 addon：没有任何包层级，包限定 import 只能靠 try/except 扁平兜底。
v0.7.3 构建冒烟曾抓到 #91 迁移引入的真实破坏——capture_store 改引
shared.defaults 后扁平世界启动即死（1631 项 dev 测试全绿也没拦住：
dev 世界永远有包层级）。本测试用真实 subprocess + 平铺目录钉住这条链。
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# addon 扁平链的全体成员：入口 + 其扁平兜底会拉入的每个仓库模块
_FLAT_CHAIN = (
    "capture/ai_capture_addon.py",
    "capture/capture_store.py",
    "shared/defaults.py",
)


class TestFlatWorldAddonChain(unittest.TestCase):
    def test_addon_chain_imports_in_flat_layout(self):
        with tempfile.TemporaryDirectory() as flat:
            for rel in _FLAT_CHAIN:
                shutil.copy(ROOT / rel, os.path.join(flat, os.path.basename(rel)))
            # 无包层级的环境里导入 addon：包限定首选注定 ImportError，
            # 扁平兜底须撑起整条链（任何新成员缺兜底在这里红）
            cp = subprocess.run(
                [sys.executable, "-c",
                 "import sys, ai_capture_addon; "
                 "assert ai_capture_addon.__file__.endswith('ai_capture_addon.py')"],
                cwd=flat, capture_output=True, text=True, timeout=30,
                env={**os.environ, "PYTHONPATH": flat})
        self.assertEqual(
            cp.returncode, 0,
            f"flat-world import failed (frozen addon chain broken):\n{cp.stderr}")


if __name__ == "__main__":
    unittest.main()
