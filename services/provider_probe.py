"""供应商验证与端点探测（自 balance_usage 三刀切，R5）。

一个变更原因一个模块：这里聚「供应商是否可用」的全部探查——
- ``fetch_models``：模型清单查询（GET /v1/models 候选链回退）；
- ``test_provider``：最小真实消息（ADR-010 协议分叉 anthropic/openai）；
- ``probe_provider``：端点三级探测（存在性 / 认证 / 模型清单）。

业务失败是数据不是异常（``{"error": ...}``，与 balance_usage.fetch_balance
同约定）。出站纪律复用 services.authenticated_http（跨 origin 拒 / 降级
必拒 / 1MB 上限 + 失败塑形）。
"""
import json
import logging
import time
import urllib.error
import urllib.parse

from services.authenticated_http import (
    AuthenticatedHttpClient,
    shape_outbound_error,
)
from shared.provider_auth import (
    build_outbound_headers,
    openai_max_tokens_field,
    resolve_api_key,
)

_PROBE_CLIENT = AuthenticatedHttpClient(timeout=10)

logger = logging.getLogger("magic-proxy.provider_probe")


def models_url_candidates(base):
    """GET /v1/models 探测候选的单一归宿（fetch_models 与 _probe_endpoint
    共用）：base_url 路径优先，后补 origin 根——部分厂商把 Messages API
    挂在路径前缀（如 /anthropic）下而 /models 只在根上服务。404 依次回退。
    """
    base = base.rstrip("/")
    candidates = [f"{base}/v1/models", f"{base}/models"]
    parts = urllib.parse.urlsplit(base)
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin != base:
        candidates += [f"{origin}/v1/models", f"{origin}/models"]
    return candidates


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
        return _PROBE_CLIENT.open_json(url, headers=headers)

    try:
        data = None
        for url in models_url_candidates(base):
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


def _http_error_message(e) -> str:
    """HTTPError → 供应商错误消息（≤120 字符；解析失败回退 HTTP <code>）。"""
    try:
        err_body = json.loads(e.read())
        if isinstance(err_body.get("error"), dict):
            msg = err_body["error"].get("message", "")
        else:
            msg = err_body.get("message", str(e)[:120])
    except Exception:
        msg = f"HTTP {e.code}"
    return msg[:120]


def _drain_http_error(e) -> None:
    """读完并丢弃 HTTPError 响应体（连接卫生）。"""
    try:
        e.read()
    except Exception:  # noqa: BLE001 — 清理失败无关紧要
        pass


def test_provider(sp_raw, name, model=None):
    """Send a minimal test message to a provider's chat/messages endpoint.

    ADR-010：按 provider.protocol 分叉——anthropic（默认）打
    ``{base}/v1/messages`` 的最小 Anthropic 消息；openai 打
    ``{base}/chat/completions`` 的最小 chat 消息（参数名按模型族选
    max_tokens/max_completion_tokens）。这同时是端点探测的第三级
    「真实请求确证」，按需调用（有真实费用，虽然 ≈0）。

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

    if (p.get("protocol") or "anthropic") == "openai":
        # openai 线格式知识归注册表之家（shared.provider_auth）——
        # 网关依赖缺席时探测侧不再 lazy-import suanpan
        headers = build_outbound_headers({}, key)  # openai 车道恒 Bearer
        headers["Content-Type"] = "application/json"
        body = json.dumps({
            "model": target_model,
            openai_max_tokens_field(target_model): 32,
            "messages": [{"role": "user",
                          "content": "Say hello in one word."}],
        }).encode()
        url = f"{base}/chat/completions"
    else:
        headers = build_outbound_headers({}, key,
                                         auth_header=p.get("auth_header"))
        headers["Content-Type"] = "application/json"
        headers["anthropic-version"] = "2023-06-01"
        body = json.dumps({
            "model": target_model,
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "Say hello in one word."}],
        }).encode()
        url = f"{base}/v1/messages"

    try:
        data = AuthenticatedHttpClient(timeout=30).open_json(
            url, headers=headers, data=body, method="POST", timeout=30)
        reply = ""
        if (p.get("protocol") or "anthropic") == "openai":
            choices = data.get("choices") or []
            if choices and isinstance(choices[0], dict):
                reply = str((choices[0].get("message") or {}).get("content")
                            or "")[:80]
        else:
            if isinstance(data.get("content"), list) and data["content"]:
                reply = data["content"][0].get("text", "")[:80]
        return {"ok": True, "model": data.get("model", target_model), "reply": reply}
    except urllib.error.HTTPError as e:
        return {"error": _http_error_message(e)}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


# ── 端点连通性探测（ADR-010 决策二，三级探测）───────────────────────
# 探测是免费的（GET 语义）：存在性（GET POST-only 端点看 404/405/401）
# + 认证（401/403）+ 模型清单（GET /v1/models）。产生真实费用的「最小
# 请求确证」复用 test_provider，由 UI 按需触发。

def probe_provider(provider):
    """对单个 provider 形态 dict 的 base_url 按三协议标准路径探测。

    ``provider``：至少含 base_url（可选 api_key/api_key_env/auth_header）。
    返回 ``{"anthropic": {...}, "openai": {...}, "responses": {...}}``，每协议
    ``{"reachable", "auth_ok", "latency_ms", "models", "error"}``；业务失败
    是数据不是异常（fetch_models 同约定）。
    """
    base = (provider.get("base_url") or "").rstrip("/")
    if not base:
        return {"error": "未配置 base_url"}
    key = resolve_api_key(provider)
    headers = build_outbound_headers(
        {}, key, auth_header=provider.get("auth_header"))
    out = {}
    for proto, path in (("anthropic", "/v1/messages"),
                        ("openai", "/chat/completions"),
                        ("responses", "/responses")):
        out[proto] = _probe_endpoint(base, path, headers)
    return out


def _probe_endpoint(base, path, headers):
    result = {"reachable": False, "auth_ok": None, "latency_ms": None,
              "models": None, "error": None}
    client = AuthenticatedHttpClient(timeout=10)
    started = time.monotonic()

    # 1. 存在性：GET POST-only 端点——404=无；401/403=在但 Key 问题；
    #    其余（405/400/4xx/5xx）= 服务器路由了该路径，视为存在
    exists = False
    try:
        client.open(f"{base}{path}", headers=headers, method="GET")
        exists = True  # GET 竟 200：路由在（非严格 POST-only）
    except urllib.error.HTTPError as e:
        _drain_http_error(e)
        if e.code == 404:
            exists = False
        else:
            exists = True
            if e.code in (401, 403):
                result["auth_ok"] = False
                result["error"] = f"Key 无效或无权限（HTTP {e.code}）"
    except Exception as e:
        result["error"] = shape_outbound_error(e)
        return result
    result["reachable"] = exists
    result["latency_ms"] = int((time.monotonic() - started) * 1000)
    if result["auth_ok"] is False:
        return result

    # 2/3. 认证确证 + 模型清单（GET /v1/models → /models 回退，候选链
    # 同 fetch_models 单一归宿）。reachable 只由 POST 端点存在性决定——
    # models 接口在不能证明 /v1/messages 在（OpenAI 官方即反例：有
    # /v1/models 无 /v1/messages）
    for models_url in models_url_candidates(base):
        try:
            data = json.loads(client.open(models_url, headers=headers,
                                          method="GET"))
            ids = [m.get("id") for m in data.get("data", [])
                   if isinstance(m, dict) and m.get("id")]
            result["auth_ok"] = True
            result["models"] = list(dict.fromkeys(ids))
            return result
        except urllib.error.HTTPError as e:
            _drain_http_error(e)
            if e.code in (401, 403):
                result["auth_ok"] = False
                result["error"] = f"Key 无效或无权限（HTTP {e.code}）"
                return result
            continue
        except Exception:
            continue
    if not exists:
        result["error"] = result["error"] or "端点不存在（404 且无模型接口）"
    return result
