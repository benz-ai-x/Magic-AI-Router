"""抓包冒烟（issue #2 Seam S3）：真实子进程加载真实 addon.

不 mock、不查存在性——spawn 由资源契约解析出的 mitmdump，加载解析出的
addon 脚本，断言进程带着脚本活过启动期。本地未装 mitmproxy 时整文件
跳过；CI（requirements-dev 含 mitmproxy）必跑。
"""
import subprocess
import tempfile
import time

import pytest

pytest.importorskip("mitmproxy")

from capture.resources import CaptureResourcesError, resolve_capture_resources  # noqa: E402

_STARTUP_GRACE_SECONDS = 4


def test_dev_smoke_resolves_and_boots_mitmdump_with_addon():
    try:
        res = resolve_capture_resources({})
    except CaptureResourcesError as exc:
        pytest.fail(f"资源契约 dev 态解析失败：{exc.msg}")
    with tempfile.TemporaryDirectory() as confdir:
        proc = subprocess.Popen(
            [res.mitmdump_bin, "-q", "--no-server",
             "--set", f"confdir={confdir}", "-s", res.addon_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            time.sleep(_STARTUP_GRACE_SECONDS)
            assert proc.poll() is None, (
                "mitmdump 加载 addon 后启动期内退出（加载崩溃或路径错误）")
        finally:
            proc.terminate()
            try:
                _, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                _, stderr = proc.communicate()
    err_text = (stderr or b"").decode("utf-8", "replace")
    assert "Error loading script" not in err_text, err_text[:500]
    assert "Traceback" not in err_text, err_text[:500]
