"""provider_auth — 供应商认证的唯一纯逻辑实现。

Single implementation of "how to resolve a provider's API key" and "how to
shape outbound auth headers", shared by:
- suanpan/config.py ProviderConfig (typed path — its methods delegate here)
- balance_usage.py (dict path for the config UI — no pydantic needed)

No third-party imports: the config server must work even when the Suanpan
gateway deps (pydantic/FastAPI) are absent (ADR-000 lazy-import design).
"""
import hmac
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

def restore_masked_key(new_val, old_val, keep):
    """掩码保存契约的 key 解析（#46 自 suanpan/config._restore_key 收编，
    双消费方——suanpan 掩码视图与 ConfigStateStore 事务恢复——共一处）。

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


def _is_gateway_credential(value, gateway_key):
    """True when a passthrough auth value is the gateway's own gate key.

    Matches the bare key (x-api-key form) and the Bearer form (any scheme
    casing). Compared in constant time — it is a secret.
    """
    if not gateway_key or not isinstance(value, str):
        return False
    key_b = gateway_key.encode("utf-8", "replace")
    if hmac.compare_digest(value.encode("utf-8", "replace"), key_b):
        return True
    scheme, _, rest = value.partition(" ")
    return (scheme.lower() == "bearer"
            and hmac.compare_digest(rest.encode("utf-8", "replace"), key_b))


def build_outbound_headers(incoming, api_key, auth_header=None, gateway_key=None):
    """Build outbound headers: filter hop-by-hop, apply provider auth.

    With a key, writes it per ``auth_header`` convention ("x-api-key" → bare
    header, "Authorization" → Bearer; None/other defaults to Bearer). Without
    a key, passes through the incoming auth header (OAuth from /login) —
    except values that are the gateway's own gate key (``gateway_key``),
    which must never reach a backend.
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
