"""Config schemas and YAML loader.

镖路图：所有路由规则与后端定义都在这里。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

import netloc
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from provider_auth import HOP_HEADERS as _HOP_HEADERS
from provider_auth import build_outbound_headers as _build_outbound
from provider_auth import resolve_api_key as _resolve_key


class ProviderConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    base_url: str
    api_key: str | None = None
    api_key_env: str | None = None
    auth_header: Literal["x-api-key", "Authorization"] | None = None
    enabled: Annotated[bool, Field(strict=True)] = True
    anthropic_native: Annotated[bool, Field(strict=True)] = False
    models: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check_auth(self) -> "ProviderConfig":
        if self.api_key_env and not self.auth_header:
            raise ValueError(
                "providers.<name>: api_key_env is set but auth_header is null; "
                "specify 'x-api-key' or 'Authorization'."
            )
        return self

    def resolve_api_key(self) -> str | None:
        """Resolve the API key: direct value → env var → None."""
        return _resolve_key({"api_key": self.api_key, "api_key_env": self.api_key_env})

    def build_outbound_headers(
        self, incoming: dict[str, str], api_key: str | None,
        gateway_key: str | None = None,
    ) -> dict[str, str]:
        """Build outbound headers: filter hop-by-hop, apply auth.

        ``gateway_key`` (the Suanpan gate key, AppConfig.api_key) is stripped
        from passthrough auth so it never reaches a keyless backend."""
        return _build_outbound(incoming, api_key, auth_header=self.auth_header,
                               gateway_key=gateway_key)


class RouterConfig(BaseModel):
    default: str | None = None


class Rule(BaseModel):
    match_prefix: str
    route_to: str


class UsageLogConfig(BaseModel):
    enabled: bool = True
    path: str = "~/.suanpan/logs/usage.jsonl"


class AppConfig(BaseModel):
    listen_port: int = 9527
    api_key: str | None = None
    request_timeout_s: int = 3600
    body_limit_mb: int = 50
    usage_log: UsageLogConfig = Field(default_factory=UsageLogConfig)
    providers: dict[str, ProviderConfig]
    router: RouterConfig = Field(default_factory=RouterConfig)
    rules: list[Rule] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _coerce_listen_to_port(cls, data: Any) -> Any:
        """Backward compat: accept legacy ``listen`` "host:port" string and
        normalize it to ``listen_port`` (the host must be loopback).
        """
        if isinstance(data, dict) and "listen" in data and "listen_port" not in data:
            try:
                host, port = netloc.parse_listen(str(data.pop("listen")))
                netloc.require_loopback(host)
                data["listen_port"] = port
            except ValueError:
                pass  # let pydantic's int validator surface the error
        return data

    @model_validator(mode="after")
    def _check_route_targets(self) -> "AppConfig":
        provider_names = set(self.providers)
        targets: list[tuple[str, str]] = []

        def _collect(where: str, value: str | None) -> None:
            if value:
                targets.append((where, value))

        for slot in ("default",):
            _collect(f"router.{slot}", getattr(self.router, slot))
        for i, rule in enumerate(self.rules):
            _collect(f"rules[{i}].route_to", rule.route_to)

        for where, target in targets:
            # Slash takes precedence; comma is the fallback separator.
            # "/" not in target → the comma branch must be reachable so that
            # targets like "glm,glm-4.6" validate against "glm", not the whole
            # string (which would never match a provider name).
            if "/" in target:
                provider = target.partition("/")[0]
            else:
                provider = target.partition(",")[0]
            if provider not in provider_names:
                raise ValueError(
                    f"{where}={target!r} references unknown provider {provider!r}; "
                    f"known providers: {sorted(provider_names)}"
                )
        return self

    def listen_address(self) -> str:
        """Compose the loopback "host:port" listen string for uvicorn et al."""
        return netloc.format_listen("127.0.0.1", self.listen_port)


def load_config(path: Path | str) -> AppConfig:
    raw = yaml.safe_load(Path(path).read_text())
    return AppConfig.model_validate(raw)


def dump_config(config: AppConfig) -> str:
    """Serialize AppConfig back to YAML string (round-trips with load_config)."""
    data = config.model_dump()
    return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)


# ── dict-level helpers (for config_server) ───────────────────────
# API-key 掩码契约：发给 UI 的 provider 不含真实 key，用 api_key_set 布尔
# 标记"已保存"；UI 未修改时回传 api_key 为空/缺失 + api_key_set 为 true，
# 保存端据此保留旧 key。掩码字符（•）不再是任何一层的判断依据。


def _restore_key(new_val, old_val, keep):
    """Resolve the key to persist.

    ``keep`` (the UI's ``api_key_set`` flag) true + no new value → keep the
    existing key. Otherwise use the new value (empty/None clears it).
    """
    if keep and not new_val:
        return old_val
    return new_val or None


def load_config_raw(path: Path | str) -> dict:
    """Read raw (unmasked) config dict from YAML. Returns {} on any error."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text())
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def load_config_masked(path: Path | str) -> dict:
    """Read config dict for the UI: real API keys never leave the process.

    Each provider (and the top-level key) is rewritten to ``api_key: None``
    plus ``api_key_set: bool`` telling the UI whether a key is saved.
    """
    cfg = load_config_raw(path)
    for p in cfg.get("providers", {}).values():
        if isinstance(p, dict):
            p["api_key_set"] = bool(p.get("api_key"))
            p["api_key"] = None
    cfg["api_key_set"] = bool(cfg.get("api_key"))
    cfg["api_key"] = None
    return cfg


def save_config_dict(data: dict, path: Path | str) -> tuple[bool, str | None]:
    """Validate and write a config dict to YAML.

    Restores masked API keys from the existing file before writing.
    Returns (ok, error_msg).
    """
    old = load_config_raw(path)
    old_providers = old.get("providers", {})
    for name, p in data.get("providers", {}).items():
        p["api_key"] = _restore_key(
            p.get("api_key"),
            old_providers.get(name, {}).get("api_key"),
            bool(p.pop("api_key_set", False)))
    data["api_key"] = _restore_key(
        data.get("api_key"), old.get("api_key"),
        bool(data.pop("api_key_set", False)))
    try:
        config = AppConfig.model_validate(data)
    except Exception as e:
        return False, f"Suanpan 配置校验失败: {e}"
    # Atomic write (0600 — the file holds API keys) with pre-write .bak backup.
    from config_store import atomic_write
    if not atomic_write(str(path), dump_config(config), backup=True):
        return False, "配置文件写入失败（详见日志）"
    return True, None
