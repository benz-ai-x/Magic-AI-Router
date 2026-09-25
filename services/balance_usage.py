"""供应商余额/配额查询与归一（R5 三刀切后的单一职责半边）。

Extracted from config_server.py to isolate the upstream balance-API calls
behind one seam. ``fetch_balance`` takes the raw Suanpan config dict (no
file I/O) so it unit-tests directly — the caller owns reading
``~/.suanpan.yaml``.

姊妹模块（同期拆出，一个变更原因一个模块）：
- services/provider_probe.py — 供应商验证与端点探测（models/test/probe）；
- services/usage_stats.py — 本地 usage.jsonl 的 CST 聚合。
时区口径（CST）与出站失败塑形（shape_outbound_error）归 shared/defaults
与 services/authenticated_http。
"""
import json
import logging
import urllib.parse

from services.authenticated_http import (
    AuthRedirectError,
    AuthenticatedHttpClient,
    shape_outbound_error,
)
from datetime import datetime

from shared.defaults import CST
from shared.provider_auth import (
    PROVIDER_REGISTRY as _REGISTRY,
    resolve_api_key,
)

_BALANCE_CLIENT = AuthenticatedHttpClient(timeout=10)

logger = logging.getLogger("magic-proxy.balance_usage")

# 供应商 → 余额 API 的单一真源是 shared.provider_auth.PROVIDER_REGISTRY
# （#51：与 UI 模板共消费——新增供应商只改注册表一处）。
# (host 片段, [(url, auth-style, label), ...]) ——注册表视图
PROVIDER_BALANCE_APIS = [
    (frag, entry["balance_apis"])
    for entry in _REGISTRY.values()
    for frag in entry["hosts"]
    if entry["balance_apis"]
]

_MONTHLY_PERIOD = "每月"  # canonical label（GLM model-usage / Kimi totalQuota 共用）
_UNIT_NAMES = {3: "5小时", 5: _MONTHLY_PERIOD, 6: "每周"}
_WEEK_HOURS = 24 * 7
_MONTH_HOURS = 24 * 30
# Duration in hours for each GLM unit — used for ascending sort of quota windows.
_UNIT_DURATION_HOURS = {3: 5, 6: _WEEK_HOURS, 5: _MONTH_HOURS}
_LEVEL_MAP = {"LEVEL_ADVANCED": "Advanced", "LEVEL_PRO": "Pro", "LEVEL_ALLEGRO": "Allegro"}


def _fmt_dt(dt):
    """Short Chinese datetime label — the quota reset-time display format."""
    return f"{dt.month}月{dt.day}日 {dt:%H:%M}"


def _fmt_reset(iso_ts):
    """Format an ISO timestamp as a short Chinese date, converted to CST
    (provider reset times are UTC; the UI's convention is CST)."""
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
        return _fmt_dt(dt.astimezone(CST))
    except Exception:
        return iso_ts[:16]


def _fmt_reset_ms(epoch_ms):
    """Format an epoch-millis timestamp like _fmt_reset (GLM nextResetTime).
    Returns None on malformed input (display simply omits the reset note)."""
    try:
        return _fmt_dt(datetime.fromtimestamp(int(epoch_ms) / 1000, tz=CST))
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _quota_row(period, pct, used, limit, reset, sort_hours):
    """One quota-window row; ``_sort`` is stripped after the ascending sort."""
    return {"period": period, "pct": pct, "used": used, "limit": limit,
            "reset": reset, "_sort": sort_hours}


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


def _parse_deepseek_balance(raw, label):
    """DeepSeek：{"balance_infos": [{"total_balance", "topped_up_balance",
    "currency"}]}——形状不符返回 None（交还路由方）。"""
    if not (isinstance(raw.get("balance_infos"), list)
            and raw["balance_infos"]):
        return None
    info = raw["balance_infos"][0]
    sym = "¥" if info.get("currency") == "CNY" else ""
    return {"label": label, "primary": sym + str(info.get("total_balance", "—")),
            "secondary": f"充值 {sym}{info.get('topped_up_balance', '—')}"}


def _parse_glm_quota_limits(raw, label):
    """GLM Coding Plan：{"data": {"limits": [...], "level": "..."}}。"""
    d = raw.get("data", {}) if isinstance(raw.get("data"), dict) else {}
    if not isinstance(d.get("limits"), list):
        return None
    level = (d.get("level") or "").upper() or "套餐"
    quotas = []
    pcts = []
    for lim in d["limits"]:
        period = _UNIT_NAMES.get(lim.get("unit"), f"unit{lim.get('unit')}")
        if lim.get("type") == "TIME_LIMIT":
            # 工具时长配额（usageDetails: search-prime/web-reader/zread），
            # 不是 token 用量——period 标「·工具」与 model-usage 月度行区分
            period += "·工具"
        p = lim.get("percentage")
        if isinstance(p, (int, float)):
            pcts.append(int(p))
        quotas.append(_quota_row(
            period,
            int(p) if isinstance(p, (int, float)) else None,
            lim["currentValue"] if "currentValue" in lim else None,
            lim["usage"] if "usage" in lim else None,
            _fmt_reset_ms(lim.get("nextResetTime")),
            _UNIT_DURATION_HOURS.get(lim.get("unit"), 0),
        ))
    quotas.sort(key=lambda q: q["_sort"])
    for q in quotas:
        del q["_sort"]
    return {"label": label, "primary": level,
            "pct": max(pcts) if pcts else None,
            "quotas": quotas}


def _parse_glm_model_usage(raw, label):
    """GLM model-usage（月度官方统计，本月窗口）：{"data": {"totalUsage":
    {"totalModelCallCount", "totalTokensUsage"}}}。"""
    d = raw.get("data", {}) if isinstance(raw.get("data"), dict) else {}
    tu = d.get("totalUsage") if isinstance(d.get("totalUsage"), dict) else None
    if tu is None or "totalTokensUsage" not in tu:
        return None
    tokens = tu.get("totalTokensUsage")
    calls = tu.get("totalModelCallCount")
    row = _quota_row(_MONTHLY_PERIOD, None, tokens, None, None,
                     _MONTH_HOURS)
    del row["_sort"]
    row["source"] = "api"
    row["calls"] = calls
    return {"label": label, "primary": "本月用量",
            "pct": None, "quotas": [row]}


def _parse_kimi_usage(raw, label):
    """Kimi Coding Plan：{"usage": {周额度}, "limits": [{window: 5小时窗口}],
    "totalQuota": {月度会员池, 按套餐填充, 可能为 {}}, "user": {...}}。"""
    if not (isinstance(raw.get("usage"), dict) and "limit" in raw["usage"]):
        return None
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
            quotas.append(_quota_row(
                wlabel, wpct, wused, wlim,
                _fmt_reset(det["resetTime"]) if det.get("resetTime") else None,
                _window_duration_hours(w),
            ))
    quotas.append(_quota_row(
        "每周", pct_num, used, lim,
        _fmt_reset(u["resetTime"]) if u.get("resetTime") else None,
        _WEEK_HOURS,
    ))
    # totalQuota = 月度会员池，按套餐填充（我们 Advanced 账号返回 {}）
    tq = raw.get("totalQuota") or {}
    if tq.get("limit"):
        tused, tlim = int(tq.get("used", 0)), int(tq["limit"])
        quotas.append(_quota_row(
            _MONTHLY_PERIOD,
            round(tused / tlim * 100) if tlim > 0 else None,
            tused, tlim,
            _fmt_reset(tq["resetTime"]) if tq.get("resetTime") else None,
            _MONTH_HOURS,
        ))
    quotas.sort(key=lambda q: q["_sort"])
    for q in quotas:
        del q["_sort"]
    all_pcts = [q["pct"] for q in quotas if q["pct"] is not None]
    return {"label": label, "primary": level,
            "pct": max(all_pcts) if all_pcts else None,
            "quotas": quotas}


def _parse_glm_account(raw, label):
    """GLM 账户：{"data": {"balance", "totalSpendAmount"}}。"""
    d = raw.get("data", {}) if isinstance(raw.get("data"), dict) else {}
    if "balance" not in d:
        return None
    return {"label": label, "primary": f"¥{d['balance']:.2f}",
            "secondary": f"已消费 ¥{d.get('totalSpendAmount', 0):.2f}"}


# 响应语法名 → 解析器（注册表 API 卡按名引用；本表是名字的单一归宿）
_BALANCE_PARSERS = {
    "deepseek_balance": _parse_deepseek_balance,
    "glm_quota_limits": _parse_glm_quota_limits,
    "glm_model_usage": _parse_glm_model_usage,
    "kimi_usage": _parse_kimi_usage,
    "glm_account": _parse_glm_account,
}

# 嗅探兜底链（原行为保序）：卡上无 parser 名或形状不符时逐个试形状
_SNIFF_CHAIN = (
    _parse_deepseek_balance,
    _parse_glm_quota_limits,
    _parse_glm_model_usage,
    _parse_kimi_usage,
    _parse_glm_account,
)


def normalize_balance(raw, label, parser=None):
    """Shape a raw balance/usage API response into a structured dict.

    路由：卡上声明 parser 名（注册表 API 卡第 4 元）→ 按名精确路由；
    无名/未知名/形状不符 → 形状嗅探兜底（接口变更期不比旧版差）。

    For providers with quota windows (GLM Coding Plan, Kimi):
    ``{label, primary, pct, quotas: [{period, pct, used, limit, reset}, ...]}``

    For simple balance providers (DeepSeek, GLM account):
    ``{label, primary, secondary}``  — no ``quotas`` key.

    Called only by ``fetch_balance`` in this module.
    """
    fn = _BALANCE_PARSERS.get(parser)
    if fn is not None:
        parsed = fn(raw, label)
        if parsed is not None:
            return parsed
    for fallback in _SNIFF_CHAIN:
        parsed = fallback(raw, label)
        if parsed is not None:
            return parsed
    # Unrecognized structure: show only top-level key names, never values —
    # a provider response could reflect the API key back and would otherwise
    # leak into the settings UI.
    keys = [str(k) for k in raw if isinstance(k, str)][:8]
    return {"label": label, "primary": "—",
            "secondary": ("响应字段: " + ", ".join(keys)) if keys else "未识别的响应结构"}


def _month_window():
    """本月起止（GLM model-usage 查询窗口）——CST 日历，与 usage 聚合
    的时区口径一致。"""
    now = datetime.now(CST)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    fmt = "%Y-%m-%d %H:%M:%S"
    return start.strftime(fmt), now.strftime(fmt)


def fetch_balance(sp_raw):
    """Query each enabled provider's balance API. ``sp_raw`` = raw Suanpan config dict.

    「全读 API，没有不显示」（#调研落地）：GLM 月度经 model-usage 本月
    窗口官方统计；Kimi 月度仅 totalQuota 非空（高阶套餐）才显示；
    API 无月度维度的供应商该行不出现——本地聚合回退已删除。
    """
    results = []

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
        frag, apis = matched
        api_res = []
        for url, style, label, parser in apis:
            try:
                auth = f"Bearer {key}" if style == "bearer" else key
                data = _BALANCE_CLIENT.open_json(
                    url, headers={"Authorization": auth})
                api_res.append(normalize_balance(data, label, parser))
            except AuthRedirectError as e:
                api_res.append({"label": label, "error": e.msg[:120]})
            except Exception as e:
                api_res.append({"label": label,
                                "error": shape_outbound_error(e)})
        # GLM 月度：model-usage 本月窗口官方统计（注册表
        # model_usage_url = (url, parser)）
        entry = next((e for e in _REGISTRY.values()
                      if frag in e["hosts"]), None)
        mu = (entry or {}).get("model_usage_url")
        if mu:
            mu_url, mu_parser = mu
            try:
                start, end = _month_window()
                url = (mu_url + "?startTime=" + urllib.parse.quote(start)
                       + "&endTime=" + urllib.parse.quote(end))
                data = _BALANCE_CLIENT.open_json(
                    url, headers={"Authorization": key})
                api_res.append(normalize_balance(data, "本月用量", mu_parser))
            except AuthRedirectError as e:
                api_res.append({"label": "本月用量", "error": e.msg[:120]})
            except Exception as e:
                api_res.append({"label": "本月用量",
                                "error": shape_outbound_error(e)})
        results.append({"provider": name, "supported": True, "apis": api_res})
    return results
