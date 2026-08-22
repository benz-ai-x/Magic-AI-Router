"""Routing decision: which backend handles this request.

镖头：每单货走哪条山路、哪条水路，由这里拍板。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import structlog

from suanpan.compat import extract_system_text
from suanpan.config import AppConfig

_log = structlog.get_logger()


SUBAGENT_RE = re.compile(r"<SUBAGENT-MODEL>(.*?)</SUBAGENT-MODEL>", re.DOTALL)


@dataclass
class RouteDecision:
    provider: str
    target_model: str
    scenario: str
    strip_marker: bool = False  # if True, caller must strip the SUBAGENT marker from system text
    # #50：显式路由意图（内联覆盖/SUBAGENT 标签）因 provider 未知或停用
    # 而 fall-through 时携带原意图（如 "ghost/model-x"）——调用方据此在
    # 响应头/日志宣告 fallback，绝不静默误投。正常路由恒 None。
    fallback_from: str | None = None


class NoRouteMatched(Exception):
    def __init__(self, source_model: str) -> None:
        super().__init__(f"No route matched for model {source_model!r}")
        self.source_model = source_model


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_intent(raw: str) -> str:
    """#50：显式意图串进入 fallback_from 前消毒——剥离 CR/LF/控制
    字符并收敛到 latin-1 可编码（model 来自客户端请求体：CRLF 会被
    h11 拒、非 latin-1（如中文 SUBAGENT 标签）会 UnicodeEncodeError，
    两者都在误投之后 500，可感知性反被摧毁）。"""
    cleaned = _CONTROL_CHARS.sub(" ", raw).strip()
    return cleaned.encode("latin-1", "replace").decode("latin-1")


def _parse_target(target: str) -> tuple[str, str]:
    sep = "/" if "/" in target else ","
    provider, _, model = target.partition(sep)
    return provider, model


def _make_decision(
    slot_value: str | None,
    scenario: str,
    config: AppConfig,
    strip_marker: bool = False,
) -> RouteDecision | None:
    """Parse a router slot (single str) into a RouteDecision.
    Skip providers marked enabled=False."""
    if not slot_value:
        return None
    provider, target = _parse_target(slot_value)
    if provider not in config.providers or not config.providers[provider].enabled:
        return None
    return RouteDecision(provider, target, scenario, strip_marker=strip_marker)


def decide_route(
    body: dict[str, Any],
    *,
    system_text: str | None = None,
    config: AppConfig,
) -> RouteDecision:
    source_model: str = body.get("model", "") or ""
    sys_text = system_text if system_text is not None else extract_system_text(body)
    fallback_from: str | None = None

    # Priority 1: inline override (model contains '/' or ',')
    if "/" in source_model or "," in source_model:
        provider, target = _parse_target(source_model)
        if provider in config.providers and config.providers[provider].enabled:
            return RouteDecision(provider, target, "inline")
        # unknown or disabled provider → fall through（容错刻意，#50 起
        # 携带原意图供调用方宣告——绝不静默误投）
        fallback_from = _sanitize_intent(source_model)

    # Priority 2: <SUBAGENT-MODEL>x</> escape hatch
    m = SUBAGENT_RE.search(sys_text)
    if m:
        intent = m.group(1).strip()
        provider, target = _parse_target(intent)
        if provider in config.providers and config.providers[provider].enabled:
            return RouteDecision(provider, target, "subagent", strip_marker=True)
        # unknown or disabled provider → fall through（同上）
        fallback_from = fallback_from or _sanitize_intent(intent)

    # Priority 3: prefix rules
    for rule in config.rules:
        if source_model.startswith(rule.match_prefix):
            d = _make_decision(rule.route_to, "rule", config)
            if d:
                d.fallback_from = fallback_from
                return d

    # Priority 4: default
    d = _make_decision(config.router.default, "default", config)
    if d:
        d.fallback_from = fallback_from
        return d

    raise NoRouteMatched(source_model)


def strip_marker(body: dict[str, Any]) -> None:
    """Remove <SUBAGENT-MODEL> tags from the system field, in-place."""
    system = body.get("system")
    if isinstance(system, str):
        body["system"] = SUBAGENT_RE.sub("", system)
    elif isinstance(system, list):
        for item in system:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                item["text"] = SUBAGENT_RE.sub("", item["text"])
