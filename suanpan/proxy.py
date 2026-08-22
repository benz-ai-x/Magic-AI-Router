"""Streaming proxy: forwards bytes to the backend, tees them through a
read-only SSE parser to extract authoritative usage from message_start /
message_delta events.

镖师：押车上路，把客人交给目的地，沿途记一笔账。
"""

from __future__ import annotations

import time

import httpx
import structlog
from fastapi import Request
from fastapi.responses import JSONResponse, StreamingResponse

from suanpan.compat import normalize_body
from suanpan.config import AppConfig
from suanpan.router import RouteDecision, strip_marker
from suanpan.usage_extractor import UsageExtractor
from suanpan.usage_log import UsageEntry, UsageLogger

_log = structlog.get_logger()

# ── RetryPolicy（issue #7）────────────────────────────────────────────
# 无法证明请求未送达时，非幂等请求绝不自动重放：ReadError /
# RemoteProtocolError / WriteError / 读写超时都可能发生在上游已经处理
# 之后——重放即重复推理/计费/tool side effect。
# 可重试的充分条件：
#   1. pre-send-proven——连接建立/等池阶段失败（ConnectError/
#      ConnectTimeout/PoolTimeout/ProxyError/UnsupportedProtocol），
#      请求一个字节都没出本机；
#   2. idempotent-transport——幂等方法（GET/HEAD/PUT/DELETE/OPTIONS 或显式
#      幂等键）遇传输层错误，有界一次。
# 超时例外沿既有理由：慢上游不靠加倍修复（幂等也不重试）。
_PRE_SEND_PROVEN = (httpx.ConnectError, httpx.ConnectTimeout,
                    httpx.PoolTimeout, httpx.ProxyError,
                    httpx.UnsupportedProtocol)
_TRANSPORT_ERRORS = (httpx.NetworkError, httpx.ProtocolError,
                     httpx.ProxyError, httpx.UnsupportedProtocol)
_IDEMPOTENT_METHODS = ("GET", "HEAD", "PUT", "DELETE", "OPTIONS")
_MAX_RETRIES = 1


def should_retry(method, error, *, idempotent=None, attempt=0):
    """→ (是否重试, reason)。判定见模块 RetryPolicy 注释。"""
    if isinstance(error, _PRE_SEND_PROVEN) and attempt < _MAX_RETRIES:
        return True, "pre-send-proven"
    if isinstance(error, httpx.TimeoutException):
        return False, "timeout-ambiguous"
    idem = (idempotent if idempotent is not None
            else method.upper() in _IDEMPOTENT_METHODS)
    if idem and isinstance(error, _TRANSPORT_ERRORS) and attempt < _MAX_RETRIES:
        return True, "idempotent-transport"
    return False, "post-send-ambiguous"


async def _send_with_retry(
    http_client: httpx.AsyncClient, req: httpx.Request, *, idempotent=None,
) -> httpx.Response:
    """Send once; auto-retry only per RetryPolicy（issue #7）.

    req 必须可重发（content 为完整 bytes，非已消费 stream）——调用方
    构造的 upstream_req 即此形态。幂等性：方法族自动判定，或显式传
    ``idempotent=True``（如携带服务端幂等键的 POST）。
    """
    attempt = 0
    while True:
        try:
            return await http_client.send(req, stream=True)
        except (httpx.TransportError, httpx.TimeoutException) as e:
            retry, reason = should_retry(req.method, e,
                                         idempotent=idempotent, attempt=attempt)
            if not retry:
                raise
            attempt += 1
            _log.warning("transport_retry", url=str(req.url),
                         error=type(e).__name__, reason=reason, attempt=attempt)


async def drain_and_log(
    response: httpx.Response,
    extractor: UsageExtractor,
    logger: UsageLogger,
    *,
    provider: str,
    source_model: str,
    target_model: str,
    scenario: str,
    started: float,
):
    """Stream response bytes to the caller while extracting usage.

    Feeds each raw chunk to *extractor* for SSE parsing, yields the bytes
    unchanged for the StreamingResponse, then logs a UsageEntry in the
    finally block — even if the consumer disconnects mid-stream.

    Extracted from a closure so the SSE→extractor→usage chain is testable
    with real byte arrays (e.g. GLM ``data:`` vs KIMI ``data:`` prefix).
    """
    stream_error = None
    try:
        async for chunk in response.aiter_raw():
            extractor.feed(chunk)
            yield chunk
    except Exception as e:  # noqa: BLE001 — recorded, then re-raised to caller
        stream_error = f"{type(e).__name__}: {e}"
        raise
    finally:
        try:
            await response.aclose()
        except Exception:  # noqa: BLE001 — closing must never mask the stream
            pass
        try:
            logger.write(
                UsageEntry(
                    provider=provider,
                    source_model=source_model,
                    target_model=target_model,
                    scenario=scenario,
                    input_tokens=extractor.input_tokens,
                    output_tokens=extractor.output_tokens,
                    cache_read_tokens=extractor.cache_read_tokens,
                    cache_creation_tokens=extractor.cache_creation_tokens,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    status=response.status_code,
                    error=stream_error,
                )
            )
        except Exception:  # noqa: BLE001 — usage logging must never truncate the stream
            logger_exc = logger.__class__.__name__
            _log.warning("usage_log.write failed (%s): %s", logger_exc, stream_error)


def filter_response_headers(headers: httpx.Headers) -> dict[str, str]:
    drop = {"content-length", "transfer-encoding", "connection"}
    return {k: v for k, v in headers.items() if k.lower() not in drop}


def make_502(
    provider: str, source_model: str, target_model: str, scenario: str,
    error: str, started: float, logger: "UsageLogger",
) -> JSONResponse:
    """Build a standard 502 failure response + log entry."""
    logger.write(
        UsageEntry(
            provider=provider,
            source_model=source_model,
            target_model=target_model,
            scenario=scenario,
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            latency_ms=int((time.monotonic() - started) * 1000),
            status=502,
            error=error,
        )
    )
    return JSONResponse(
        {"error": "backend request failed", "provider": provider, "last_error": error},
        status_code=502,
        headers={"x-suanpan-provider": provider},
    )


async def forward_request(
    request: Request,
    body: dict,
    decision: RouteDecision,
    config: AppConfig,
    logger: UsageLogger,
    http_client: httpx.AsyncClient,
) -> StreamingResponse | JSONResponse:
    if decision.strip_marker:
        strip_marker(body)

    source_model = body.get("model", "")
    started = time.monotonic()

    # Convert headers once
    incoming_headers = dict(request.headers)

    provider_name = decision.provider
    target_model = decision.target_model
    provider_cfg = config.providers[provider_name]
    api_key = provider_cfg.resolve_api_key()

    body["model"] = target_model
    normalize_body(body, provider_name,
                   anthropic_native=provider_cfg.anthropic_native)
    headers = provider_cfg.build_outbound_headers(
        incoming_headers, api_key)
    url = f"{provider_cfg.base_url.rstrip('/')}/v1/messages"

    try:
        upstream_req = http_client.build_request("POST", url, json=body, headers=headers)
        upstream_resp = await _send_with_retry(
            http_client, upstream_req,
            # 明确幂等语义（issue #7 验收③）：客户端携带幂等键头即视为
            # 可安全重放
            idempotent=any(h in request.headers
                           for h in ("idempotency-key", "x-idempotency-key")))
    except httpx.HTTPError as e:
        error = f"{type(e).__name__}: {e}"
        _log.error("upstream_error", provider=provider_name, error=error)
        return make_502(provider_name, source_model, target_model, decision.scenario, error, started, logger)

    # 5xx → 502 (no retry, no backend switch)
    if upstream_resp.status_code >= 500:
        await upstream_resp.aclose()
        error = f"HTTP {upstream_resp.status_code}"
        _log.error("upstream_5xx", provider=provider_name, status=upstream_resp.status_code)
        return make_502(provider_name, source_model, target_model, decision.scenario, error, started, logger)

    # Success (2xx or 4xx) — stream response to client
    out_headers = filter_response_headers(upstream_resp.headers)
    out_headers["x-suanpan-provider"] = provider_name

    # Non-streaming upstreams answer application/json with usage at the
    # top level — the SSE scanner would read zeros (DeepSeek stream:false
    # requests logged empty rows from 8/15 until this content-type switch).
    content_type = upstream_resp.headers.get("content-type", "")
    extractor = UsageExtractor(json_mode="json" in content_type.lower())

    return StreamingResponse(
        drain_and_log(
            upstream_resp, extractor, logger,
            provider=provider_name,
            source_model=source_model,
            target_model=target_model,
            scenario=decision.scenario,
            started=started,
        ),
        status_code=upstream_resp.status_code,
        headers=out_headers,
        media_type=upstream_resp.headers.get("content-type"),
    )


async def forward_count_tokens(
    request: Request,
    body: dict,
    decision: RouteDecision,
    config: AppConfig,
    http_client: httpx.AsyncClient,
) -> JSONResponse:
    """Non-streaming variant — hits /v1/messages/count_tokens."""
    provider_name = decision.provider
    target_model = decision.target_model
    provider_cfg = config.providers[provider_name]
    api_key = provider_cfg.resolve_api_key()
    if decision.strip_marker:
        strip_marker(body)
    body["model"] = target_model
    normalize_body(body, provider_name,
                   anthropic_native=provider_cfg.anthropic_native)
    headers = provider_cfg.build_outbound_headers(
        dict(request.headers), api_key)
    url = f"{provider_cfg.base_url.rstrip('/')}/v1/messages/count_tokens"

    try:
        upstream_req = http_client.build_request("POST", url, json=body, headers=headers)
        r = await _send_with_retry(http_client, upstream_req)
        # _send_with_retry 恒以 stream=True 发送（forward_request 的流式
        # 转发需要）；count_tokens 是非流式语义，读全响应体后再消费——
        # 未读即 .json()/.text 会抛 httpx.ResponseNotRead（它不是
        # HTTPError 子类，会直穿成 500）。读取放在同一 try 内：读体中途
        # 的传输错误（ReadError 等）与发送失败同样走 502 塑形，失败即关流。
        try:
            await r.aread()
        except httpx.HTTPError:
            try:
                await r.aclose()
            except Exception:  # noqa: BLE001 — 关闭失败不得掩过原始流错误
                pass
            raise
    except httpx.HTTPError as e:
        error = f"{type(e).__name__}: {e}"
        _log.error("upstream_error", provider=provider_name, error=error)
        return JSONResponse(
            {"error": "backend request failed", "provider": provider_name,
             "last_error": error},
            status_code=502,
            headers={"x-suanpan-provider": provider_name},
        )

    out_headers = filter_response_headers(r.headers)
    out_headers["x-suanpan-provider"] = provider_name
    return JSONResponse(
        content=r.json() if r.headers.get("content-type", "").startswith("application/json") else {"raw": r.text},
        status_code=r.status_code,
        headers=out_headers,
    )
