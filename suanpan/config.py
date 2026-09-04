"""Config schemas and YAML loader.

镖路图：所有路由规则与后端定义都在这里。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal

from shared import netloc
from shared.defaults import DEFAULT_GATEWAY_PORT
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from shared.provider_auth import build_outbound_headers as _build_outbound
from shared.provider_auth import resolve_api_key as _resolve_key
from shared.identity import IdentityMigrationError


class ProviderConfig(BaseModel):
    model_config = ConfigDict(validate_assignment=True)

    # 稳定身份（issue #8）：不可变、无业务含义；显示名可随意改，
    # api_key 的 keep/replace/clear 恢复按 id 匹配旧值。
    id: str | None = None
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
    ) -> dict[str, str]:
        """Build outbound headers: filter hop-by-hop, apply auth.

        Keyless 出站无条件剥除一切入站凭证（issue #9）——网关自己的
        gate key 绝不落到后端。"""
        return _build_outbound(incoming, api_key, auth_header=self.auth_header)


class RouterConfig(BaseModel):
    default: str | None = None


class Rule(BaseModel):
    match_prefix: str
    route_to: str


class UsageLogConfig(BaseModel):
    enabled: bool = True
    path: str = "~/.suanpan/logs/usage.jsonl"


class AppConfig(BaseModel):
    listen_port: int = DEFAULT_GATEWAY_PORT
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

    @model_validator(mode="before")
    @classmethod
    def _coerce_null_sections(cls, data: Any) -> Any:
        """#47：显式 null 节 = 缺省——「节标题 + 下面全注释」是最常见的
        手编 YAML 姿势（解析为 null），应等价于缺省而非 ValidationError。
        仅归一显式 null：键缺失仍走必填校验（providers 不会因此放宽）。
        """
        if isinstance(data, dict):
            for key, default in (("rules", []), ("router", {}),
                                 ("usage_log", {}), ("providers", {})):
                if key in data and data[key] is None:
                    data[key] = default
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

        # 文法单一所有者（#70 S1）：消费 router.parse_route_target——
        # 不再内联双分隔符逻辑（曾三处漂移）
        from suanpan.router import parse_route_target
        for where, target in targets:
            provider, _model = parse_route_target(target)
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


def friendly_config_error_lines(exc: Exception) -> list:
    """#47：配置装载错误 → 可定位行列表（字段路径 + 消息）。

    pydantic ValidationError 取逐字段 ``loc: msg``（pydantic v2 的 msg
    不含字段值，无 secret 泄漏面）；其余异常取 ``类型: 消息``。返回
    列表——消费方（网关 factory 塑形 / ConfigStateStore 事务校验）
    各自决定 join 形态，不以分隔符做隐式协议。
    """
    items = getattr(exc, "errors", None)
    if callable(items):
        parts = []
        for it in items():
            loc = ".".join(str(x) for x in it.get("loc", ()))
            parts.append(f"{loc or '配置'}: {it.get('msg', '')}".rstrip(": "))
        if parts:
            return parts
    return [f"{type(exc).__name__}: {exc}"]


def friendly_config_error(exc: Exception) -> str:
    """单行形态（菜单栏/通知的展示契约：一行可截断）。"""
    return "；".join(friendly_config_error_lines(exc))


def dump_config(config: AppConfig) -> str:
    """Serialize AppConfig back to YAML string (round-trips with load_config)."""
    data = config.model_dump()
    return yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)


# ── dict-level helpers (for config_server) ───────────────────────
# API-key 掩码契约：发给 UI 的 provider 不含真实 key，用 api_key_set 布尔
# 标记"已保存"；UI 未修改时回传 api_key 为空/缺失 + api_key_set 为 true，
# 保存端据此保留旧 key。掩码字符（•）不再是任何一层的判断依据。



def assign_provider_ids(cfg: dict) -> int:
    """为无 id 的 provider 赋确定性 id（p-<sha1(name)[:10]>）。

    重复 id 抛可行动错误——不猜测 secret 归属（issue #8）。
    """
    import hashlib
    seen, migrated = set(), 0
    for name, p in (cfg.get("providers") or {}).items():
        if not isinstance(p, dict):
            continue
        pid = p.get("id")
        if pid:
            if pid in seen:
                raise IdentityMigrationError(
                    f"供应商存在重复 id：{pid}（请修正配置文件后重试）")
            seen.add(pid)
            continue
        p["id"] = "p-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:10]
        seen.add(p["id"])
        migrated += 1
    return migrated


def load_config_raw(path: Path | str) -> dict:
    """Read raw (unmasked) config dict from YAML. Returns {} on any error."""
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text())
    except Exception:
        return {}
    if isinstance(data, dict):
        # issue #8：装载即赋幂等 id；重复 id 等可行动错误上抛——
        # 「任何错误返回 {}」的旧契约对迁移错误失真，读方按需捕获。
        # 形状守卫（#69 R7）：providers 非 dict（数组/标量）时
        # assign_provider_ids 会 .items() 裸抛——规整为空 dict
        if not isinstance(data.get("providers"), (dict, type(None))):
            data["providers"] = {}
        assign_provider_ids(data)
    return data if isinstance(data, dict) else {}


def load_config_masked(path: Path | str) -> dict:
    """Read config dict for the UI: real API keys never leave the process.

    Each provider (and the top-level key) is rewritten to ``api_key: None``
    plus ``api_key_set: bool`` telling the UI whether a key is saved.
    迁移可行动错误降级为 ``{"_load_error": msg}``——UI 可展示，服务不 500。
    """
    try:
        cfg = load_config_raw(path)
    except ValueError as e:
        return {"_load_error": str(e), "providers": {}, "rules": [],
                "router": {}}
    for p in cfg.get("providers", {}).values():
        if isinstance(p, dict):
            p["api_key_set"] = bool(p.get("api_key"))
            p["api_key"] = None
    cfg["api_key_set"] = bool(cfg.get("api_key"))
    cfg["api_key"] = None
    return cfg


