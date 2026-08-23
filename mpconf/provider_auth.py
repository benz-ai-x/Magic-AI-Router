"""provider_auth — 供应商认证的唯一纯逻辑实现。

Single implementation of "how to resolve a provider's API key" and "how to
shape outbound auth headers", shared by:
- suanpan/config.py ProviderConfig (typed path — its methods delegate here)
- balance_usage.py (dict path for the config UI — no pydantic needed)

No third-party imports: the config server must work even when the Suanpan
gateway deps (pydantic/FastAPI) are absent (ADR-000 lazy-import design).
"""
import os

# Headers never forwarded to the backend (hop-by-hop or auth-related).
HOP_HEADERS = frozenset({
    "host",
    "content-length",
    "connection",
    "keep-alive",
    "transfer-encoding",
    "upgrade",
    "authorization",
    "x-api-key",
})

# ── 供应商知识注册表（#51）───────────────────────────────────────────
# 「新增一家供应商要改哪里」的单一答案（余额 API 与 UI 模板共消费）。
# balance_apis 的 auth-style："bearer" → `Authorization: Bearer <key>`；
# "raw" → 裸 key 直接作 Authorization 值。
# 字段：label（UI 显示名）/ hosts（base_url 子串匹配片段，余额侧用）/
# base_url + anthropic_native（UI 模板种子，兼容 Anthropic 原生 body
# 的后端才置 True）/ balance_apis（[(url, auth-style, label)]，无则空）。
#
# 刻意不在注册表的消费方：
# - capture/ai_capture_addon.identify()——在 mitmdump 子进程内独立运行
#   （零仓内 import 是资源契约的一部分），且其知识是抓包专属的端点
#   变体（chat/completions vs responses 等），与账户/路由知识不同域。
# - suanpan/compat.normalize_body——已由 anthropic_native 旗标驱动
#   （配置态数据，非供应商名硬编码）。
PROVIDER_REGISTRY = {
    "deepseek": {
        "label": "DeepSeek",
        "hosts": ["api.deepseek.com"],
        "base_url": "https://api.deepseek.com",
        "anthropic_native": False,
        "balance_apis": [
            ("https://api.deepseek.com/user/balance", "bearer", "余额"),
        ],
    },
    "glm": {
        "label": "GLM",
        "hosts": ["bigmodel.cn"],
        "base_url": "https://open.bigmodel.cn/api/anthropic",
        "anthropic_native": True,
        "balance_apis": [
            ("https://open.bigmodel.cn/api/monitor/usage/quota/limit",
             "raw", "Coding Plan"),
            ("https://www.bigmodel.cn/api/biz/account/query-customer-account-report",
             "raw", "账户余额"),
        ],
    },
    "kimi": {
        "label": "KIMI",
        "hosts": ["api.kimi.com"],
        "base_url": "https://api.kimi.com/coding",
        "anthropic_native": True,
        "balance_apis": [
            ("https://api.kimi.com/coding/v1/usages", "bearer", "Coding Plan"),
        ],
    },
}

def restore_masked_key(new_val, old_val, keep):
    """掩码保存契约的 key 解析（唯一实现即此；唯一消费方
    ConfigStateStore._restore_masked_sp_keys，suanpan 侧只掩码不恢复）。

    ``keep``（UI 的 api_key_set 布尔）真且无新值 → 保留旧 key；否则用
    新值（空/None 即清除）。
    """
    if keep and not new_val:
        return old_val
    return new_val or None


def resolve_api_key(provider):
    """Resolve a provider's API key from a plain dict.

    Literal ``api_key`` wins; falls back to the ``api_key_env`` environment
    variable; None when neither yields a key. The masked UI view never carries
    a real key (it sends ``api_key: None`` + ``api_key_set``), so no
    mask-placeholder check is needed here.
    """
    key = provider.get("api_key")
    if key:
        return key
    env = provider.get("api_key_env")
    if env:
        return os.environ.get(env)
    return None


def build_outbound_headers(incoming, api_key, auth_header=None):
    """Build outbound headers: filter hop-by-hop, apply provider auth.

    With a key, writes it per ``auth_header`` convention ("x-api-key" → bare
    header, "Authorization" → Bearer; None/other defaults to Bearer). Without
    a key, strips ALL incoming credentials unconditionally (issue #9) —
    the gateway's own gate key, placeholder or user token, must never reach
    a backend.
    """
    out = {}
    for k, v in incoming.items():
        if k.lower() in HOP_HEADERS:
            continue
        out[k] = v

    if api_key:
        if auth_header and auth_header.lower() == "x-api-key":
            out["x-api-key"] = api_key
        else:
            out["Authorization"] = f"Bearer {api_key}"
    else:
        # issue #9：keyless Provider 出站**无条件**剥除一切入站凭证——
        # `mage-router` 占位、本地客户端 token、用户真实 token 都绝不
        # 透传到上游。网关自己的凭证不得成为后端凭证。
        out.pop("Authorization", None)
        out.pop("authorization", None)
        out.pop("x-api-key", None)
        out.pop("X-Api-Key", None)

    return out
