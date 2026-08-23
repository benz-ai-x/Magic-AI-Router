"""FastAPI app factory + uvicorn launcher."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from suanpan.config import AppConfig, load_config
from suanpan.middleware import APIKeyMiddleware, BodyLimitMiddleware
from suanpan.proxy import (
    forward_request,
    forward_count_tokens,
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

        Returns Anthropic-compatible /v1/models format so tools like
        CC Switch can discover available models. Model ids use
        "provider/model" form matching inline-override routing.
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
                    "display_name": mid,
                    "created_at": "2025-01-01T00:00:00Z",
                })
        return {
            "object": "list",
            "data": models,
            "has_more": False,
            "first_id": models[0]["id"] if models else None,
            "last_id": models[-1]["id"] if models else None,
        }

    @app.post("/v1/messages")
    async def messages(request: Request):
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON body"}, status_code=400)
        try:
            decision = decide_route(
                body,
                config=app.state.config,
            )
        except NoRouteMatched as e:
            return JSONResponse(
                {"error": "no route matched", "source_model": e.source_model},
                status_code=400,
            )
        return await forward_request(
            request, body, decision, app.state.config,
            app.state.usage_logger, app.state.http_client,
        )

    @app.post("/v1/messages/count_tokens")
    async def count_tokens(request: Request):
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
        return await forward_count_tokens(
            request, body, decision, app.state.config,
            app.state.http_client,
        )

    return app


def run_from_config_path(path: str | Path = "./suanpan.yaml") -> None:
    from mpconf import netloc
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
