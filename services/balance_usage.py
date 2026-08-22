"""Provider balance + usage aggregation for the config UI.

Extracted from config_server.py to isolate the upstream balance-API calls and
usage-log aggregation behind one seam. ``fetch_balance`` / ``fetch_usage`` take
the raw Suanpan config dict (no file I/O) so they unit-test directly — the
caller owns reading ``~/.suanpan.yaml``.
"""
import json
import logging
import math
import os
import urllib.error
import urllib.parse
import urllib.request

from services.authenticated_http import (
    AuthRedirectError,
    AuthenticatedHttpClient,
)

from datetime import datetime, timedelta, timezone

from mpconf.provider_auth import build_outbound_headers, resolve_api_key

_BALANCE_CLIENT = AuthenticatedHttpClient(timeout=10)

logger = logging.getLogger("magic-proxy.balance_usage")

# Provider → balance-API table: (host fragment, [(url, auth-style, label), ...]).
# auth-style "bearer" → `Authorization: Bearer <key>`; "raw" → bare key.
PROVIDER_BALANCE_APIS = [
    ("api.deepseek.com", [
        ("https://api.deepseek.com/user/balance", "bearer", "余额"),
    ]),
    ("bigmodel.cn", [
        ("https://open.bigmodel.cn/api/monitor/usage/quota/limit", "raw", "Coding Plan"),
        ("https://www.bigmodel.cn/api/biz/account/query-customer-account-report", "raw", "账户余额"),
    ]),
    ("api.kimi.com", [
        ("https://api.kimi.com/coding/v1/usages", "bearer", "Coding Plan"),
    ]),
]

_UNIT_NAMES = {3: "5小时", 5: "每月", 6: "每周"}
# Duration in hours for each GLM unit — used for ascending sort of quota windows.
_UNIT_DURATION_HOURS = {3: 5, 6: 24 * 7, 5: 24 * 30}
_LEVEL_MAP = {"LEVEL_ADVANCED": "Advanced", "LEVEL_PRO": "Pro", "LEVEL_ALLEGRO": "Allegro"}
CST = timezone(timedelta(hours=8))
USAGE_RANGES = frozenset({"today", "7d", "month", "all"})
DEFAULT_USAGE_LOG_PATH = "~/.suanpan/logs/usage.jsonl"
_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_creation_tokens",
)
_USAGE_NUMERIC_FIELDS = (*_TOKEN_FIELDS, "latency_ms", "status")


def _fmt_reset(iso_ts):
    """Format an ISO timestamp as a short Chinese date, converted to CST
    (provider reset times are UTC; the UI's convention is CST)."""
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        dt = dt.astimezone(CST)
        return f"{dt.month}月{dt.day}日 {dt:%H:%M}"
    except Exception:
        return iso_ts[:16]


def _fmt_reset_ms(epoch_ms):
    """Format an epoch-millis timestamp like _fmt_reset (GLM nextResetTime)."""
    try:
        dt = datetime.fromtimestamp(int(epoch_ms) / 1000, tz=CST)
        return f"{dt.month}月{dt.day}日 {dt:%H:%M}"
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _window_label(window):
    """Label a Kimi limits[] window like {duration: 300, timeUnit: TIME_UNIT_MINUTE}."""
    dur = window.get("duration")
    unit = window.get("timeUnit", "")
    if not isinstance(dur, int):
        return None
    if unit == "TIME_UNIT_MINUTE":
        if dur % 60 == 0:
            return f"{dur // 60}小时"
        return f"{dur}分钟"
    if unit == "TIME_UNIT_HOUR":
        return f"{dur}小时"
    if unit == "TIME_UNIT_DAY":
        return f"{dur}天"
    return None


def _window_duration_hours(window):
    """Approximate duration in hours for a Kimi window (for sort ordering)."""
    dur = window.get("duration")
    unit = window.get("timeUnit", "")
    if not isinstance(dur, (int, float)):
        return 0
    if unit == "TIME_UNIT_MINUTE":
        return dur / 60
    if unit == "TIME_UNIT_HOUR":
        return dur
    if unit == "TIME_UNIT_DAY":
        return dur * 24
    return 0


def normalize_balance(raw, label):
    """Shape a raw balance/usage API response into a structured dict.

    For providers with quota windows (GLM Coding Plan, Kimi):
    ``{label, primary, pct, quotas: [{period, pct, used, limit, reset}, ...]}``

    For simple balance providers (DeepSeek, GLM account):
    ``{label, primary, secondary}``  — no ``quotas`` key.

    Called only by ``fetch_balance`` in this module.
    """
    # DeepSeek: {"balance_infos": [{"total_balance", "topped_up_balance", "currency"}]}
    if isinstance(raw.get("balance_infos"), list) and raw["balance_infos"]:
        info = raw["balance_infos"][0]
        sym = "¥" if info.get("currency") == "CNY" else ""
        return {"label": label, "primary": sym + str(info.get("total_balance", "—")),
                "secondary": f"充值 {sym}{info.get('topped_up_balance', '—')}"}
    d = raw.get("data", {}) if isinstance(raw.get("data"), dict) else {}
    # GLM Coding Plan: {"data": {"limits": [...], "level": "..."}}
    if isinstance(d.get("limits"), list):
        level = (d.get("level") or "").upper() or "套餐"
        quotas = []
        pcts = []
        for lim in d["limits"]:
            period = _UNIT_NAMES.get(lim.get("unit"), f"unit{lim.get('unit')}")
            p = lim.get("percentage")
            if isinstance(p, (int, float)):
                pcts.append(int(p))
            used = lim["currentValue"] if "currentValue" in lim else None
            qlimit = lim["usage"] if "usage" in lim else None
            quotas.append({
                "period": period,
                "pct": int(p) if isinstance(p, (int, float)) else None,
                "used": used,
                "limit": qlimit,
                "reset": _fmt_reset_ms(lim.get("nextResetTime")),
                "_sort": _UNIT_DURATION_HOURS.get(lim.get("unit"), 0),
            })
        quotas.sort(key=lambda q: q["_sort"])
        for q in quotas:
            del q["_sort"]
        return {"label": label, "primary": level,
                "pct": max(pcts) if pcts else None,
                "quotas": quotas}
    # Kimi Coding Plan: {"usage": {周额度}, "limits": [{window: 5小时窗口}],
    # "totalQuota": {月度会员池, 按套餐填充, 可能为 {}}, "user": {...}}
    if isinstance(raw.get("usage"), dict) and "limit" in raw["usage"]:
        u = raw["usage"]
        used, lim = int(u.get("used", 0)), int(u.get("limit", 0))
        pct_num = round(used / lim * 100) if lim > 0 else None
        level_raw = raw.get("user", {}).get("membership", {}).get("level", "")
        level = _LEVEL_MAP.get(level_raw, level_raw or "套餐")
        quotas = []
        # limits[] holds windowed quotas (e.g. 300-minute = 5小时)
        for entry in raw.get("limits") or []:
            w = entry.get("window") or {}
            det = entry.get("detail") or {}
            wlabel = _window_label(w)
            if wlabel and det.get("limit"):
                wused, wlim = int(det.get("used", 0)), int(det["limit"])
                wpct = round(wused / wlim * 100) if wlim > 0 else None
                quotas.append({
                    "period": wlabel,
                    "pct": wpct,
                    "used": wused,
                    "limit": wlim,
                    "reset": _fmt_reset(det["resetTime"]) if det.get("resetTime") else None,
                    "_sort": _window_duration_hours(w),
                })
        quotas.append({
            "period": "每周",
            "pct": pct_num,
            "used": used,
            "limit": lim,
            "reset": _fmt_reset(u["resetTime"]) if u.get("resetTime") else None,
            "_sort": 24 * 7,
        })
        # totalQuota = 月度会员池，按套餐填充（我们 Advanced 账号返回 {}）
        tq = raw.get("totalQuota") or {}
        if tq.get("limit"):
            tused, tlim = int(tq.get("used", 0)), int(tq["limit"])
            quotas.append({
                "period": "每月",
                "pct": round(tused / tlim * 100) if tlim > 0 else None,
                "used": tused,
                "limit": tlim,
                "reset": _fmt_reset(tq["resetTime"]) if tq.get("resetTime") else None,
                "_sort": 24 * 30,
            })
        quotas.sort(key=lambda q: q["_sort"])
        for q in quotas:
            del q["_sort"]
        all_pcts = [q["pct"] for q in quotas if q["pct"] is not None]
        return {"label": label, "primary": level,
                "pct": max(all_pcts) if all_pcts else None,
                "quotas": quotas}
    # GLM account: {"data": {"balance", "totalSpendAmount"}}
    if "balance" in d:
        return {"label": label, "primary": f"¥{d['balance']:.2f}",
                "secondary": f"已消费 ¥{d.get('totalSpendAmount', 0):.2f}"}
    # Unrecognized structure: show only top-level key names, never values —
    # a provider response could reflect the API key back and would otherwise
    # leak into the settings UI.
    keys = [str(k) for k in raw if isinstance(k, str)][:8]
    return {"label": label, "primary": "—",
            "secondary": ("响应字段: " + ", ".join(keys)) if keys else "未识别的响应结构"}


def fetch_models(sp_raw, name):
    """Query one provider's model list API. ``sp_raw`` = raw Suanpan config dict.

    Returns ``{"models": [id, ...]}`` or ``{"error": <message>}`` — business
    failures are data, not exceptions (same convention as ``fetch_balance``).
    Tries ``{base_url}/v1/models`` first, falls back to ``{base_url}/models``
    on 404 (mirrors the ``/v1/messages`` URL convention in suanpan/proxy.py).
    """
    p = sp_raw.get("providers", {}).get(name)
    if p is None:
        return {"error": f"供应商 {name!r} 不存在"}
    base = p.get("base_url", "").rstrip("/")
    if not base:
        return {"error": "未配置 base_url"}
    key = resolve_api_key(p)
    if not key:
        return {"error": "未配置 API Key"}
    headers = build_outbound_headers({}, key, auth_header=p.get("auth_header"))
    headers["anthropic-version"] = "2023-06-01"

    def _get(url):
        return _BALANCE_CLIENT.open_json(url, headers=headers)

    # Candidates: base_url paths first, then origin root — some providers mount
    # the Messages API under a path prefix (e.g. /anthropic) but only serve
    # /models at the root.
    candidates = [f"{base}/v1/models", f"{base}/models"]
    parts = urllib.parse.urlsplit(base)
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin != base:
        candidates += [f"{origin}/v1/models", f"{origin}/models"]

    try:
        data = None
        for url in candidates:
            try:
                data = _get(url)
                break
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
        if data is None:
            return {"error": "供应商未提供模型列表接口（均 404）"}
        ids = [m["id"] for m in data["data"]]
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return {"models": list(dict.fromkeys(ids))}


def test_provider(sp_raw, name, model=None):
    """Send a minimal test message to a provider's /v1/messages endpoint.

    Returns {"ok": True, "model": ..., "reply": "..."} on success,
    or {"error": "<message>"} on failure.
    """
    p = sp_raw.get("providers", {}).get(name)
    if p is None:
        return {"error": f"供应商 {name!r} 不存在"}
    base = p.get("base_url", "").rstrip("/")
    if not base:
        return {"error": "未配置 base_url"}
    key = resolve_api_key(p)
    if not key:
        return {"error": "未配置 API Key"}
    models = p.get("models") or []
    target_model = model or (models[0] if models else "")
    if not target_model:
        return {"error": "未配置模型"}

    headers = build_outbound_headers({}, key, auth_header=p.get("auth_header"))
    headers["Content-Type"] = "application/json"
    headers["anthropic-version"] = "2023-06-01"

    body = json.dumps({
        "model": target_model,
        "max_tokens": 32,
        "messages": [{"role": "user", "content": "Say hello in one word."}],
    }).encode()

    try:
        data = AuthenticatedHttpClient(timeout=30).open_json(
            f"{base}/v1/messages", headers=headers, data=body, method="POST",
            timeout=30)
        reply = ""
        if isinstance(data.get("content"), list) and data["content"]:
            reply = data["content"][0].get("text", "")[:80]
        return {"ok": True, "model": data.get("model", target_model), "reply": reply}
    except urllib.error.HTTPError as e:
        try:
            err_body = json.loads(e.read())
            msg = err_body.get("error", {}).get("message", "") if isinstance(err_body.get("error"), dict) else err_body.get("message", str(err_body)[:120])
        except Exception:
            msg = f"HTTP {e.code}"
        return {"error": msg[:120]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def fetch_balance(sp_raw):
    """Query each enabled provider's balance API. ``sp_raw`` = raw Suanpan config dict.

    Plan providers (quota windows) whose API reports no 每月 row get a local
    fallback row aggregated from the gateway usage log (source="local",
    limit/pct None) — the local row never overrides an API-reported month.
    """
    results = []
    monthly_providers = None  # lazy: only read the log when a plan lacks API monthly

    def local_monthly_row(name):
        nonlocal monthly_providers
        if monthly_providers is None:
            monthly_providers = fetch_usage(sp_raw, "month")["providers"]
        bucket = monthly_providers.get(name)
        if bucket is None:
            return None
        return {
            "period": "每月",
            "pct": None,
            "used": sum(bucket[f] for f in _TOKEN_FIELDS),
            "limit": None,
            "reset": None,
            "source": "local",
            "calls": bucket["calls"],
        }

    for name, p in sp_raw.get("providers", {}).items():
        if p.get("enabled") is False:
            results.append({"provider": name, "enabled": False})
            continue
        base = p.get("base_url", "")
        key = resolve_api_key(p)
        matched = next((ap for ap in PROVIDER_BALANCE_APIS if ap[0] in base), None)
        if not matched:
            results.append({"provider": name, "supported": False, "note": "无余额 API"})
            continue
        if not key:
            results.append({"provider": name, "supported": True, "error": "未配置 API Key"})
            continue
        _, apis = matched
        api_res = []
        for url, style, label in apis:
            try:
                auth = f"Bearer {key}" if style == "bearer" else key
                data = _BALANCE_CLIENT.open_json(
                    url, headers={"Authorization": auth})
                api_res.append(normalize_balance(data, label))
            except AuthRedirectError as e:
                api_res.append({"label": label, "error": e.msg[:120]})
            except Exception as e:
                api_res.append({"label": label, "error": f"{type(e).__name__}"})
        for res in api_res:
            quotas = res.get("quotas")
            if not isinstance(quotas, list):
                continue
            if any(q.get("period") == "每月" for q in quotas):
                continue
            row = local_monthly_row(name)
            if row is not None:
                quotas.append(row)
        results.append({"provider": name, "supported": True, "apis": api_res})
    return results


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
                "daily": [], "scenarios": {}}
    by_provider = {}
    by_day = {}
    by_route_source = {}
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
                _add_usage(by_provider.setdefault(
                    provider, _usage_bucket()), entry)
                _add_usage(by_route_source.setdefault(
                    route_source, _usage_bucket()), entry)
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
    }
