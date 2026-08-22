"""PyInstaller entry point for the bundled mitmdump helper (ADR-001).

frozen 构建的 mitmdump 入口：build.sh 先以 onedir 打出独立 mitmdump，
再作为 Resources/mitmdump/ 嵌入主 .app——capture/resources.py 的三级
解析链（env → frozen bundled → PATH）在 frozen 态落到它。仅委托
mitmproxy 自身 CLI；addon 由 resources 契约以 --scripts 挂载。
"""
import sys

from mitmproxy.tools.main import mitmdump

if __name__ == "__main__":
    sys.exit(mitmdump())
