"""抓包模式的资源契约（issue #2）.

控制器消费**已验证**的 ``CaptureResources``，不再自行拼接文件名——
"capture.ai_capture_addon.py" 点连名事故（dev/frozen 双态解析失败、
抓包模式无法启动）的根治。
"""
from __future__ import annotations

import os
import shutil
import sys

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


class CaptureResources:
    """已通过 preflight 的抓包资源三元组（frozen dataclass 语义）。"""

    __slots__ = ("mitmdump_bin", "addon_path", "capture_dir")

    def __init__(self, mitmdump_bin: str, addon_path: str, capture_dir: str):
        self.mitmdump_bin = mitmdump_bin
        self.addon_path = addon_path
        self.capture_dir = capture_dir


def resolve_capture_resources(cfg: dict) -> CaptureResources:
    """解析并验证 mitmdump / addon / 抓包目录。失败抛 CaptureResourcesError。"""
    override = os.environ.get("MAGIC_PROXY_MITMDUMP_BIN")
    mitmdump_bin = resolve_mitmdump_bin()
    if not mitmdump_bin or not os.path.exists(mitmdump_bin):
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
