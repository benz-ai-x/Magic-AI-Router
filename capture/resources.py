"""抓包模式的资源契约（issue #2）.

控制器消费**已验证**的 ``CaptureResources``，不再自行拼接文件名——
"capture.ai_capture_addon.py" 点连名事故（dev/frozen 双态解析失败、
抓包模式无法启动）的根治。
"""
from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass

from util import resource_path as _resource_path
from capture.capture_store import DEFAULT_CAPTURE_DIR, prepare as prepare_capture_dir

ADDON_RESOURCE_NAME = "ai_capture_addon.py"


def resolve_mitmdump_bin():
    """Locate the mitmdump binary (env override → bundled → PATH). None if not found.

    Deliberate deviation from SSHMonitor's hardcoded "ssh" literal: mitmdump is
    sometimes PATH-resolved (dev venv) and sometimes a bundled path inside the
    packaged .app. MAGIC_PROXY_MITMDUMP_BIN lets dev/test override explicitly.
    """
    override = os.environ.get("MAGIC_PROXY_MITMDUMP_BIN")
    if override:
        return override
    if hasattr(sys, "_MEIPASS"):
        bundled = _resource_path(os.path.join("mitmdump", "mitmdump"))
        return bundled if os.path.exists(bundled) else None
    return shutil.which("mitmdump")


class CaptureResourcesError(Exception):
    """Preflight 失败；msg 为可直接展示的可行动中文错误。"""

    def __init__(self, msg: str):
        self.msg = msg
        super().__init__(msg)


@dataclass(frozen=True)
class CaptureResources:
    """已通过 preflight 的抓包资源三元组。"""

    mitmdump_bin: str
    addon_path: str
    capture_dir: str


def resolve_capture_resources(cfg: dict) -> CaptureResources:
    """解析并验证 mitmdump / addon / 抓包目录。失败抛 CaptureResourcesError。"""
    mitmdump_bin = resolve_mitmdump_bin()
    if not mitmdump_bin or not os.path.exists(mitmdump_bin) \
            or not os.access(mitmdump_bin, os.X_OK):
        raise CaptureResourcesError(
            "未找到 mitmdump 可执行文件（可设置 MAGIC_PROXY_MITMDUMP_BIN 指定路径）")
    addon = _resource_path(ADDON_RESOURCE_NAME)
    if not os.path.isfile(addon) or not os.access(addon, os.R_OK):
        raise CaptureResourcesError(f"抓包组件缺失或不可读：{addon}")
    try:
        capture_dir = prepare_capture_dir(
            (cfg or {}).get("capture_dir") or DEFAULT_CAPTURE_DIR)
    except OSError as exc:
        raise CaptureResourcesError(f"抓包目录不可用：{exc}") from exc
    return CaptureResources(mitmdump_bin, addon, capture_dir)


# ── 启动冒烟判据：单一归宿（此前正则/宽限秒散落 build.sh、SIT、app.py
#    三处且已漂移 5≠4；统一收敛，彻底方案归 issue #14）───────────────
SMOKE_GRACE_SECONDS = 5  # 进程须带着 addon 活过此时长
SMOKE_ERROR_MARKERS = ("Error loading script", "Traceback")


def smoke_capture_boot(res=None, *, grace_seconds=SMOKE_GRACE_SECONDS):
    """启动期冒烟：spawn mitmdump 加载 addon，活过宽限期且无加载报错。

    返回 (ok, detail)。dev（SIT）与 frozen（app smoke 钩子，经 build.sh
    调用）共用此判据。
    """
    import subprocess
    import tempfile
    import time
    if res is None:
        res = resolve_capture_resources({})
    with tempfile.TemporaryDirectory() as confdir:
        proc = subprocess.Popen(
            [res.mitmdump_bin, "-q", "--no-server",
             "--set", f"confdir={confdir}", "-s", res.addon_path],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        stderr = b""
        try:
            time.sleep(grace_seconds)
            if proc.poll() is not None:
                _, stderr = proc.communicate()
                return False, (
                    f"mitmdump rc={proc.returncode} died during "
                    f"{grace_seconds}s grace: "
                    + stderr.decode("utf-8", "replace")[:300])
        finally:
            proc.terminate()
            try:
                _, stderr = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                _, stderr = proc.communicate()
    err_text = stderr.decode("utf-8", "replace")
    for marker in SMOKE_ERROR_MARKERS:
        if marker in err_text:
            return False, f"addon load reported: {marker}"
    return True, "mitmdump carried the addon past the grace window"
