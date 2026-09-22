"""FastAPI app factory + uvicorn launcher."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from suanpan.compat import (
    openai_system_text,
    responses_system_text,
)
from suanpan.config import AppConfig, load_config
from suanpan.middleware import APIKeyMiddleware, BodyLimitMiddleware
from suanpan.proxy import (
    forward_chat_passthrough,
    forward_count_tokens,
    forward_request,
    forward_responses_passthrough,
)
from suanpan.router import NoRouteMatched, decide_route
from suanpan.usage_log import UsageLogger



def create_app(config: AppConfig, config_path: str = "./suanpan.yaml") -> FastAPI:
    # Shared connection pool — tuned for low-latency domestic API access
    http_client = httpx.AsyncClient(
        http2=True,  # multiplexing: multiple requests per TCP connection
        timeout=httpx.Timeout(
            connect=10.0,
            read=float(config.request_timeout_s),
            write=float(config.request_timeout_s),
            pool=5.0,
        ),
        limits=httpx.Limits(
            max_keepalive_connections=20,
            max_connections=100,
            keepalive_expiry=300.0,  # keep warm 5 min (default 5s is too aggressive)
        ),
    )

    @asynccontextmanager
    async def lifespan(app):
        app.state.http_client = http_client
        # issue #15：预热走 best-effort adapter——有界并发+总预算+可取消；
        # 单个慢 Provider 不再线性拖慢 readiness，失败不影响启动。
        from suanpan.prewarmer import ProviderPrewarmer
        await ProviderPrewarmer().warm(config.providers, http_client)
        yield
        await http_client.aclose()

    app = FastAPI(title="算盘 (Suanpan) — AI router", lifespan=lifespan)
    app.state.config = config
    app.state.usage_logger = UsageLogger(
        enabled=config.usage_log.enabled, path=config.usage_log.path
    )
    app.state.http_client = http_client

    if config.api_key:
        app.add_middleware(APIKeyMiddleware, api_key=config.api_key)
    app.add_middleware(BodyLimitMiddleware, max_bytes=config.body_limit_mb * 1048576)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def list_models() -> dict:
        """Aggregate model list across all enabled providers.

        ADR-010：返回超集形状——Anthropic 字段（type/display_name/
        created_at，CC Switch 等工具消费）+ OpenAI 字段（object/created/
        owned_by，OpenAI SDK 消费），两种客户端都可解析。
        """
        config = app.state.config
        models = []
        for name, p in config.providers.items():
            if not p.enabled:
                continue
            for m in p.models:
                mid = f"{name}/{m}"
                models.append({
                    "id": mid,
                    "type": "model",
                    "object": "model",
                    "display_name": mid,
                    "created_at": "2025-01-01T00:00:00Z",
                    "created": 1735689600,
                    "owned_by": "suanpan",
                })
        return {
            "object": "list",
            "data": models,
            "has_more": False,
            "first_id": models[0]["id"] if models else None,
            "last_id": models[-1]["id"] if models else None,
        }

    async def _parse_and_route(request: Request):
        """两端点共用的前奏：body 解析 + 路由决策。

        返回 (body, decision) 或错误 JSONResponse——调用方经 isinstance
        分流（提取前两处理器逐行重复的同一前奏）。
        """
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            decision = decide_route(body, config=app.state.config)
        except NoRouteMatched as e:
            return JSONResponse(
                {"error": "no route matched", "source_model": e.source_model},
                status_code=400,
            )
        return body, decision

    @app.post("/v1/messages")
    async def messages(request: Request):
        routed = await _parse_and_route(request)
        if isinstance(routed, JSONResponse):
            return routed
        body, decision = routed
        return await forward_request(
            request, body, decision, app.state.config,
            app.state.usage_logger, app.state.http_client,
        )

    @app.post("/v1/chat/completions")
    async def chat_completions(request: Request):
        """ADR-010 直通车道：OpenAI Chat 入站（OpenCode/Kilo/Grok/Droid 等）。

        路由决策复用 decide_route（model 字段同名；system 经
        ``system_text`` seam 从首条 system 消息提取）。供应商必须是
        openai 协议端点——「openai 入站 × anthropic 端点」象限刻意不
        实现（协议引导消灭，见 ADR-010 决策一）。
        """
        def _err(msg: str, status: int = 400) -> JSONResponse:
            return JSONResponse({"error": {"message": msg,
                                           "type": "invalid_request_error"}},
                                status_code=status)

        try:
            body = await request.json()
        except Exception:
            return _err("invalid JSON body")
        if not isinstance(body, dict):
            return _err("invalid JSON body")
        try:
            decision = decide_route(body, config=app.state.config,
                                    system_text=openai_system_text(body))
        except NoRouteMatched as e:
            return _err(f"no route matched for model {e.source_model!r}")
        provider_cfg = app.state.config.providers[decision.provider]
        if (provider_cfg.protocol or "anthropic") != "openai":
            return _err(
                f"供应商 {decision.provider!r} 是 Anthropic 端点；OpenAI "
                "协议客户端请在供应商配置里选择 openai 端点"
                "（protocol: openai）", 400)
        return await forward_chat_passthrough(
            request, body, decision, app.state.config,
            app.state.usage_logger, app.state.http_client,
        )

    @app.post("/v1/responses")
    async def responses(request: Request):
        """ADR-010 M3a：OpenAI Responses 入站（Codex CLI 专用——Codex 已
        移除 wire_api="chat"，只说 Responses 协议）。

        直通车道：仅当供应商配了原生 Responses 端点
        （``responses_base_url``，OpenAI/DeepSeek/GLM/Kimi）。未配时返回
        可行动错误（转换 C 未实施，见 ADR-010）。
        """
        def _err(msg: str, status: int = 400) -> JSONResponse:
            return JSONResponse({"error": {"message": msg,
                                           "type": "invalid_request_error"}},
                                status_code=status)

        try:
            body = await request.json()
        except Exception:
            return _err("invalid JSON body")
        if not isinstance(body, dict):
            return _err("invalid JSON body")
        try:
            decision = decide_route(body, config=app.state.config,
                                    system_text=responses_system_text(body))
        except NoRouteMatched as e:
            return _err(f"no route matched for model {e.source_model!r}")
        provider_cfg = app.state.config.providers[decision.provider]
        if not provider_cfg.responses_base_url:
            return _err(
                f"供应商 {decision.provider!r} 暂不支持 Responses 协议"
                "（Codex）——需配置厂商原生 Responses 端点"
                "（responses_base_url）", 400)
        return await forward_responses_passthrough(
            request, body, decision, app.state.config,
            app.state.usage_logger, app.state.http_client,
        )

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request):
        routed = await _parse_and_route(request)
        if isinstance(routed, JSONResponse):
            return routed
        body, decision = routed
        return await forward_count_tokens(
            request, body, decision, app.state.config,
            app.state.http_client,
        )

    return app


def run_from_config_path(path: str | Path = "./suanpan.yaml") -> None:
    from shared import netloc
    config = load_config(path)
    app = create_app(config, config_path=str(path))
    host, port = netloc.parse_listen(config.listen_address(), default_port=9527)
    netloc.require_loopback(host)
    import uvicorn

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level="info",
    )
