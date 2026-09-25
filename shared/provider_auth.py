"""provider_auth — 供应商认证的唯一纯逻辑实现。

Single implementation of "how to resolve a provider's API key" and "how to
shape outbound auth headers", shared by:
- suanpan/config.py ProviderConfig (typed path — its methods delegate here)
- balance_usage.py (dict path for the config UI — no pydantic needed)

No third-party imports: the config server must work even when the Suanpan
gateway deps (pydantic/FastAPI) are absent (ADR-000 lazy-import design).
"""
import os
import re

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

# ── 供应商知识注册表（#51，ADR-010 协议化）─────────────────────────
# 「新增一家供应商要改哪里」的单一答案（余额 API、UI 模板、端点探测共消费）。
#
# 端点矩阵（ADR-010 决策二）：每厂商 ``endpoints`` 按协议记端点卡——
#   "anthropic"：{"base_url", "anthropic_native", "auth_header"(可选)}，
#                出站 base + /v1/messages（Anthropic 生态惯例：base 不含 /v1）；
#   "openai"：   {"base_url"}，出站 base + /chat/completions（OpenAI SDK
#                惯例：base 含版本段），认证恒 Bearer；
#   "responses": {"base_url"}，出站 base + /responses（Codex 直通
#                车道），恒 Bearer。
# 顶层 base_url/anthropic_native 是 anthropic 卡的兼容投影（存量消费方
# 零迁移）；路径不合惯例的厂商端点（MiniMax chat 的
# /v1/text/chatcompletion_v2、火山方舟的 /api/v3/messages）不进内置卡，
# 经自定义 provider 手配。
#
# balance_apis 的条目：(url, auth-style, label, parser)。auth-style：
# "bearer" → `Authorization: Bearer <key>`；"raw" → 裸 key 直接作
# Authorization 值。parser 是响应语法名（balance_usage._BALANCE_PARSERS
# 的键）——余额归一按卡路由，新增厂商在卡上声明自己的语法，不再往
# normalize_balance 的形状嗅探链里加分支（嗅探仅作无卡名/形状不符的
# 兜底）。model_usage_url 同理：(url, parser) 二元组。
#
# 刻意不在注册表的消费方：
# - capture/ai_capture_addon.identify()——在 mitmdump 子进程内独立运行
#   （零仓内 import 是资源契约的一部分），且其知识是抓包专属的端点
#   变体（chat/completions vs responses 等），与账户/路由知识不同域。
#   该 fork 的分叉关系由 tests/test_capture_host_mirror.py 钉住：新增
#   厂商/host 须在报警测试白名单显式决定 capture 是否识别。
# - suanpan/compat.normalize_body——已由 anthropic_native 旗标驱动
#   （配置态数据，非供应商名硬编码）。
PROVIDER_REGISTRY = {
    "deepseek": {
        "label": "DeepSeek",
        "hosts": ["api.deepseek.com"],
        "base_url": "https://api.deepseek.com",
        "anthropic_native": False,
        "endpoints": {
            "anthropic": {"base_url": "https://api.deepseek.com",
                          "anthropic_native": False},
            "openai": {"base_url": "https://api.deepseek.com"},
            "responses": {"base_url": "https://api.deepseek.com"},
        },
        "balance_apis": [
            ("https://api.deepseek.com/user/balance", "bearer", "余额",
             "deepseek_balance"),
        ],
    },
    "glm": {
        "label": "GLM",
        "hosts": ["bigmodel.cn"],
        "base_url": "https://open.bigmodel.cn/api/anthropic",
        "anthropic_native": True,
        "endpoints": {
            "anthropic": {"base_url": "https://open.bigmodel.cn/api/anthropic",
                          "anthropic_native": True},
            "openai": {"base_url": "https://open.bigmodel.cn/api/paas/v4"},
            # 智谱 Responses 端点仅有文档快照线索，未经真机探测实证
            "responses": {"base_url": "https://open.bigmodel.cn/api/v1"},
        },
        # model_usage 端点：GLM 月度官方统计（本月窗口 token 用量+调用
        # 次数）——fetch_balance 组 startTime/endTime 本月范围；(url, parser)
        "model_usage_url": ("https://open.bigmodel.cn/api/monitor/usage/"
                            "model-usage", "glm_model_usage"),
        "balance_apis": [
            ("https://open.bigmodel.cn/api/monitor/usage/quota/limit",
             "raw", "Coding Plan", "glm_quota_limits"),
            ("https://www.bigmodel.cn/api/biz/account/query-customer-account-report",
             "raw", "账户余额", "glm_account"),
        ],
    },
    "kimi": {
        "label": "KIMI",
        "hosts": ["api.kimi.com"],
        "base_url": "https://api.kimi.com/coding",
        "anthropic_native": True,
        "endpoints": {
            "anthropic": {"base_url": "https://api.kimi.com/coding",
                          "anthropic_native": True},
            # 开放平台按量（api.moonshot.cn）与 Coding Plan（api.kimi.com）
            # 是两套入口；openai 卡给按量端点，anthropic 卡给 Coding Plan
            "openai": {"base_url": "https://api.moonshot.cn/v1"},
            "responses": {"base_url": "https://api.kimi.com/coding"},
        },
        "balance_apis": [
            ("https://api.kimi.com/coding/v1/usages", "bearer", "Coding Plan",
             "kimi_usage"),
        ],
    },
    # ── ADR-010 新增：openai 协议族厂商（无余额集成的先空着）──
    "openai": {
        "label": "OpenAI",
        "hosts": ["api.openai.com"],
        "base_url": None,  # 无 Anthropic 兼容端点
        "anthropic_native": False,
        "endpoints": {
            "openai": {"base_url": "https://api.openai.com/v1"},
            "responses": {"base_url": "https://api.openai.com/v1"},
        },
        "balance_apis": [],
    },
    "anthropic": {
        "label": "Anthropic",
        "hosts": ["api.anthropic.com"],
        "base_url": "https://api.anthropic.com",
        "anthropic_native": True,
        "endpoints": {
            "anthropic": {"base_url": "https://api.anthropic.com",
                          "auth_header": "x-api-key",
                          "anthropic_native": True},
            # 官方 OpenAI SDK 兼容层（chat）
            "openai": {"base_url": "https://api.anthropic.com/v1"},
        },
        "balance_apis": [],
    },
    "openrouter": {
        "label": "OpenRouter",
        "hosts": ["openrouter.ai"],
        "base_url": "https://openrouter.ai/api",
        "anthropic_native": False,
        "endpoints": {
            "anthropic": {"base_url": "https://openrouter.ai/api",
                          "anthropic_native": False},
            "openai": {"base_url": "https://openrouter.ai/api/v1"},
        },
        "balance_apis": [],
    },
    "qwen": {
        "label": "通义 Qwen",
        "hosts": ["dashscope.aliyuncs.com", "maas.aliyuncs.com"],
        "base_url": "https://dashscope.aliyuncs.com/apps/anthropic",
        "anthropic_native": False,
        "endpoints": {
            "anthropic": {"base_url":
                          "https://dashscope.aliyuncs.com/apps/anthropic",
                          "anthropic_native": False},
            "openai": {"base_url":
                       "https://dashscope.aliyuncs.com/compatible-mode/v1"},
        },
        "balance_apis": [],
    },
    "siliconflow": {
        "label": "硅基流动",
        "hosts": ["siliconflow.cn"],
        "base_url": "https://api.siliconflow.cn",
        "anthropic_native": False,
        "endpoints": {
            "anthropic": {"base_url": "https://api.siliconflow.cn",
                          "anthropic_native": False},
            "openai": {"base_url": "https://api.siliconflow.cn/v1"},
        },
        "balance_apis": [],
    },
    "minimax": {
        "label": "MiniMax",
        "hosts": ["minimax.io", "minimax.cn"],
        "base_url": "https://api.minimax.io/anthropic",
        "anthropic_native": False,
        "endpoints": {
            "anthropic": {"base_url": "https://api.minimax.io/anthropic",
                          "anthropic_native": False},
            # chat 端点路径特殊（/v1/text/chatcompletion_v2），不进内置卡
        },
        "balance_apis": [],
    },
    "volces": {
        "label": "火山方舟",
        "hosts": ["volces.com"],
        "base_url": None,  # anthropic 端点路径特殊（/api/v3/messages）
        "anthropic_native": False,
        "endpoints": {
            "openai": {"base_url":
                       "https://ark.cn-beijing.volces.com/api/v3"},
        },
        "balance_apis": [],
    },
}

# OpenAI 新推理系模型拒绝 max_tokens（要求 max_completion_tokens）；
# 其余厂商兼容面以 max_tokens 为准（deepseek/qwen/siliconflow 文档口径）
_NEEDS_COMPLETION_TOKENS = re.compile(r"^(gpt-5|o[134](\b|-))")


def openai_max_tokens_field(model: str) -> str:
    """OpenAI 系端点的最大输出参数名（供应商线格式知识，注册表之家）。

    消费方：suanpan/compat 转换 A（请求体改写）+ services/provider_probe
    的 test_provider 最小消息——两侧曾各自 lazy-import compat，网关依赖
    缺席时探测侧随之断供；归注册表叶子层后两消费方恒可达。
    """
    return ("max_completion_tokens"
            if _NEEDS_COMPLETION_TOKENS.match(str(model)) else "max_tokens")


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
