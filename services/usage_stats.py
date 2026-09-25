"""本地用量统计（自 balance_usage 三刀切，R5）。

一个变更原因一个模块：这里只做**纯本地**的 usage.jsonl 聚合——
CST 日历范围（today/7d/month/all）+ 供应商/来源/Agent/逐日多维桶。
不发出任何网络请求（余额在 balance_usage、探测在 provider_probe）。

``fetch_usage`` 取原始 Suanpan config dict（不读盘以外无 I/O 副作用），
调用方拥有读 ``~/.suanpan.yaml`` 的职责。时区口径 shared.defaults.CST。
"""
import json
import math
import os
from datetime import datetime, timedelta

from shared.defaults import CST

USAGE_RANGES = frozenset({"today", "7d", "month", "all"})
DEFAULT_USAGE_LOG_PATH = "~/.suanpan/logs/usage.jsonl"
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)
_USAGE_NUMERIC_FIELDS = (*_TOKEN_FIELDS, "latency_ms", "status")


def _usage_bucket(*, latency=False):
    bucket = {
        "calls": 0,
        **{field: 0 for field in _TOKEN_FIELDS},
        "errors": 0,
    }
    if latency:
        bucket["latency_sum"] = 0
    return bucket


def _add_usage(bucket, entry):
    bucket["calls"] += 1
    for field in _TOKEN_FIELDS:
        bucket[field] += entry.get(field, 0)
    if entry.get("status", 0) >= 400:
        bucket["errors"] += 1
    if "latency_sum" in bucket:
        bucket["latency_sum"] += entry.get("latency_ms", 0)


def _finish_usage(bucket):
    billed_input = (
        bucket["input_tokens"]
        + bucket["cache_read_tokens"]
        + bucket["cache_creation_tokens"]
    )
    bucket["cache_hit_rate"] = (
        bucket["cache_read_tokens"] / billed_input if billed_input else None
    )
    if bucket.get("calls") and "latency_sum" in bucket:
        latency_sum = bucket["latency_sum"]
        calls = bucket["calls"]
        if isinstance(latency_sum, int):
            quotient, remainder = divmod(latency_sum, calls)
            twice_remainder = remainder * 2
            bucket["avg_latency_ms"] = quotient + int(
                twice_remainder > calls
                or (twice_remainder == calls and quotient % 2 == 1)
            )
        else:
            bucket["avg_latency_ms"] = round(latency_sum / calls)
    return bucket


def _entry_cst_date(entry):
    ts = entry.get("ts")
    if not isinstance(ts, str):
        return None
    try:
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CST)
    return parsed.astimezone(CST).date()


def _valid_usage_entry(entry):
    if not isinstance(entry, dict):
        return False
    for field in ("provider", "scenario", "ts"):
        if not isinstance(entry.get(field), str) or not entry[field]:
            return False
    for field in _USAGE_NUMERIC_FIELDS:
        if field not in entry:
            return False
        value = entry[field]
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or (isinstance(value, float) and not math.isfinite(value))
                or value < 0):
            return False
    return _entry_cst_date(entry) is not None


def fetch_usage(sp_raw, usage_range="all"):
    """Aggregate the Suanpan usage log for a CST calendar range.

    ``sp_raw`` is the raw Suanpan config dict. ``usage_range`` is one of
    ``today`` / ``7d`` / ``month`` / ``all``; seven days includes today and
    the preceding six CST calendar dates, month is the current CST calendar
    month from the 1st through today.
    """
    if usage_range not in USAGE_RANGES:
        raise ValueError(f"invalid usage range: {usage_range!r}")
    today = datetime.now(CST).date() if usage_range != "all" else None
    first_day = (
        today if usage_range == "today"
        else today - timedelta(days=6) if usage_range == "7d"
        else today.replace(day=1) if usage_range == "month"
        else None
    )
    path = os.path.expanduser(
        sp_raw.get("usage_log", {}).get("path", DEFAULT_USAGE_LOG_PATH))
    total = _usage_bucket(latency=True)
    if not os.path.exists(path):
        return {"total": _finish_usage(total), "providers": {},
                "daily": [], "scenarios": {}, "agents": {}}
    by_provider = {}
    by_day = {}
    by_route_source = {}
    by_agent = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not _valid_usage_entry(entry):
                    continue
                day = _entry_cst_date(entry)
                if usage_range != "all" and (
                        day < first_day or day > today):
                    continue
                provider = entry["provider"]
                route_source = entry["scenario"]
                agent = entry.get("agent") or ""
                _add_usage(by_provider.setdefault(
                    provider, _usage_bucket()), entry)
                _add_usage(by_route_source.setdefault(
                    route_source, _usage_bucket()), entry)
                _add_usage(by_agent.setdefault(agent, _usage_bucket()), entry)
                _add_usage(by_day.setdefault(day, _usage_bucket()), entry)
                _add_usage(total, entry)
    except OSError:
        pass
    daily = [
        {"date": day.isoformat(), **_finish_usage(bucket)}
        for day, bucket in sorted(by_day.items())
    ]
    return {
        "total": _finish_usage(total),
        "providers": {
            name: _finish_usage(bucket) for name, bucket in by_provider.items()
        },
        "daily": daily,
        # Public name follows the persisted RouteDecision.scenario field and
        # Issue #1 API contract; internally these values are route sources.
        "scenarios": {
            name: _finish_usage(bucket)
            for name, bucket in by_route_source.items()
        },
        # ADR-010 M5：来源 Agent 维度（User-Agent 判别；空串桶 = 未识别）
        "agents": {
            name: _finish_usage(bucket) for name, bucket in by_agent.items()
        },
    }
