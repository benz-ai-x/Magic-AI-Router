"""抓包冒烟（issue #2 Seam S3）：真实子进程加载真实 addon.

判据单一归宿在 ``capture/resources.py``（SMOKE_GRACE_SECONDS /
SMOKE_ERROR_MARKERS + smoke_capture_boot）——dev SIT 与 frozen 打包冒烟
（build.sh → app 二进制 smoke 钩子）共用同一实现，不再跨语言双写。

本地未装 mitmproxy 时整文件跳过；CI（requirements-dev 含
mitmproxy）必跑。
"""
import pytest

pytest.importorskip("mitmproxy")

from capture.resources import (  # noqa: E402
    CaptureResourcesError,
    resolve_capture_resources,
    smoke_capture_boot,
)


def test_dev_smoke_boots_mitmdump_with_addon():
    try:
        res = resolve_capture_resources({})
    except CaptureResourcesError as exc:
        pytest.fail(f"资源契约 dev 态解析失败：{exc.msg}")
    ok, detail = smoke_capture_boot(res)
    assert ok, detail
