"""i18n — 跨语言文案单一归宿（ADR-012）。

叶子层（零域知识；**不得 import util**——同层不同域，见
test_arch_imports）。语义键 → 双语 catalog（``shared/locales/*.json``；
dev 按包内子目录查找，frozen 经 ``--add-data`` 平铺进 ``_MEIPASS``，
目录查找自包含）。``t()`` 是唯一取词口。

- 语言是模块级只读快照：GIL 下引用替换原子，菜单线程与配置服务
  线程各读各的，无需锁；
- 键缺席：记一次 warning、回显键名——可见但不炸；
- 占位符 ``{name}`` 经 ``str.format``；模板含裸花括号时回退原文；
- ``resolve()`` 把偏好值（含 ``auto``）收敛为支持语言：``auto`` 读
  AppleLanguages（plistlib 直读 .GlobalPreferences.plist，无子进程）；
- **语言键不做校验**（validate/JS 镜像零新增规则）：取词侧全兜底，
  坏值落缺省 zh-CN。

不翻译的（ADR-012 D6）：日志、suanpan 网关 wire 错误、内部标识符、
代码注释、docs——中文直写，不进 catalog（tests/test_i18n 的汉字
字面量守卫配套豁免 logging 子树）。
"""
from __future__ import annotations

import json
import logging
import os
import plistlib
import sys

logger = logging.getLogger("magic-proxy.i18n")

AUTO = "auto"
SUPPORTED = ("zh-CN", "en")
DEFAULT_LANGUAGE = "zh-CN"
# 语言子菜单的选项全集（偏好值域；resolved 值域是 SUPPORTED）
PREFERENCES = (AUTO,) + SUPPORTED

_current = DEFAULT_LANGUAGE
_catalogs: dict = {}
_missing_logged: set = set()


def _catalog_path(name: str) -> str:
    here = os.path.dirname(os.path.abspath(__file__))
    dev = os.path.join(here, "locales", name)
    if os.path.exists(dev):
        return dev
    base = getattr(sys, "_MEIPASS", here)  # frozen: --add-data 平铺
    return os.path.join(base, name)


def _catalog(lang: str) -> dict:
    cat = _catalogs.get(lang)
    if cat is None:
        try:
            with open(_catalog_path(f"{lang}.json"), encoding="utf-8") as f:
                cat = json.load(f)
        except (OSError, ValueError):
            logger.warning("i18n catalog missing/corrupt: %s.json", lang)
            cat = {}
        _catalogs[lang] = cat
    return cat


def set_language(lang: str) -> None:
    """切换当前语言（未知值静默保持现值——语言键不校验，取词全兜底）。"""
    global _current
    if lang in SUPPORTED and lang != _current:
        _current = lang


def language() -> str:
    return _current


def t(key: str, **params) -> str:
    """取词：当前语言 → zh-CN 兜底 → 键名回显（缺席记一次 warning）。"""
    template = _catalog(_current).get(key)
    if template is None and _current != DEFAULT_LANGUAGE:
        template = _catalog(DEFAULT_LANGUAGE).get(key)
    if template is None:
        if key not in _missing_logged:
            _missing_logged.add(key)
            logger.warning("i18n key missing: %s", key)
        return key
    if params:
        try:
            return template.format(**params)
        except (KeyError, IndexError, ValueError):
            return template
    return template


def catalog(lang: str) -> dict:
    """整本 catalog 的副本（M2：config_server 注入设置窗用）。"""
    return dict(_catalog(lang))


def _system_language() -> str:
    plist = os.path.expanduser(
        "~/Library/Preferences/.GlobalPreferences.plist")
    try:
        with open(plist, "rb") as f:
            prefs = plistlib.load(f)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return DEFAULT_LANGUAGE
    for tag in prefs.get("AppleLanguages") or []:
        tag = str(tag)
        if tag.startswith("zh"):
            return "zh-CN"
        if tag.startswith("en"):
            return "en"
    return DEFAULT_LANGUAGE


def resolve(pref) -> str:
    """偏好值 → 支持语言：合法值直通，auto/未知值跟随系统（系统语也
    不在支持列表时落缺省）。"""
    if pref in SUPPORTED:
        return pref
    return _system_language()
