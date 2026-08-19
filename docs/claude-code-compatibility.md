# Claude Code × Suanpan 网关兼容性分析

> 基于 [Claude Code env-vars 文档](https://code.claude.com/docs/en/env-vars)（2026-08-12）+
> 三家供应商 API 文档 / GitHub Issues 社区报告（专家 agent 验证，2026-08-12）+
> 本仓库 `provider_auth.py` / `suanpan/proxy.py` 源码分析。

---

## 0. 前提：网关的转发行为

**Header**（`provider_auth.py` `HOP_HEADERS`）：剥除 `host` `content-length` `connection`
`keep-alive` `transfer-encoding` `upgrade` `authorization` `x-api-key`，其余透传（含
`anthropic-beta`）。

**Body**（`suanpan/proxy.py` `forward_request`）：仅改写 `body["model"]` 和剥除路由标记，
**其余原样转发**——包括 `thinking`、`tools[].defer_loading`、`cache_control`、`system` 数组
格式、`document` 内容块等。

**结论：网关当前是「接近透明」的转发器。兼容性问题的根源不在网关本身，而在于上游供应商
对 Claude Code 发出的 Anthropic 专有字段是否容忍——以及容忍到什么程度。**

---

## 1. 已确认的实际 400 错误（专家验证，有 Issue 出处）

以下不是推测——每一条都有 GitHub Issue 或官方文档佐证，是**实际会发生的 400**。

### 1.1 DeepSeek：`system` 数组格式 → 反序列化 400

Claude Code v2.1.154+ 把 `system` 从字符串改成了带 `cache_control` 的内容块数组：
```json
"system": [{"type":"text","text":"...","cache_control":{"type":"ephemeral"}}]
```

DeepSeek `/anthropic` 端点只处理字符串格式，收到数组时转换层把内容块放入 messages 数组
作为 `role:"system"` → 反序列化失败：

```
400 Failed to deserialize: messages[1].role: unknown variant 'system',
    expected 'user' or 'assistant'
```

> 来源：[deepseek-ai/DeepSeek-V3#1369](https://github.com/deepseek-ai/DeepSeek-V3/issues/1369)

### 1.2 KIMI：`document` 内容块 → 直接 400

Claude Code 在某些场景（如 PDF 附件、图片引用）发送 `document` 类型的内容块。KIMI 不支持，
直接返回 400。上游 Issue 作者确认必须客户端剥离。

> 来源：[MoonshotAI/Kimi-K2#129](https://github.com/MoonshotAI/Kimi-K2/issues/129)

### 1.3 KIMI：tool schema 不符合 MFJS → 400

KIMI 后端使用 **Walle 验证器**（MoonshotAI/walle），要求 tool 的 `input_schema` 符合
**Moonshot Flavored JSON Schema (MFJS)**——比标准 JSON Schema 更严格：
- 每个 property **必须**显式定义 `type`（标准 JSON Schema 中 type 可选）
- `anyOf` 父级不能定义 `type`，必须在子项中定义
- 不符合时：`400 Invalid request: tools.function.parameters is not a valid moonshot flavored json schema`

> 来源：[MoonshotAI/kimi-cli#1595](https://github.com/MoonshotAI/kimi-cli/issues/1595)、
> [MoonshotAI/kimi-code#792](https://github.com/MoonshotAI/kimi-code/issues/792)

### 1.4 KIMI：`thinking` 参数模型相关

| KIMI 模型 | thinking 行为 |
|-----------|--------------|
| kimi-k3 | 默认开启，开箱即用 |
| kimi-k2.7-code | **强制要求** `thinking.type=enabled`，缺失或 type≠enabled → `400 invalid thinking: only type=enabled is allowed` |
| kimi-k2.6 | 可选 |

> 来源：[KIMI 官方 Claude Code 指南](https://platform.kimi.com/docs/guide/claude-code-kimi)

### 1.5 GLM：参数验证 400

GLM 有参数级验证（错误码 1210 "Invalid API parameter" / 1213 "Parameter was not received
normally" / 1214 "Parameter is invalid"）。Claude Code + GLM-4.6 通过 Anthropic 端点反复报
1213 错误。Z.ai **没有发布任何 Anthropic 兼容性字段表**，所有字段行为只能通过实测探测。

> 来源：[zed-industries/zed#45704](https://github.com/zed-industries/zed/issues/45704)、
> [Z.ai 错误码参考](https://docs.z.ai/api-reference/api-code)

---

## 2. Body 字段兼容性矩阵（专家验证）

| 字段 | DeepSeek | GLM (Z.ai) | KIMI (Moonshot) |
|------|----------|------------|-----------------|
| `thinking` | ✅ 支持（`budget_tokens` 忽略） | ❓ 无文档，Reddit 反馈不返回 thinking tokens | ✅ 但**模型相关**（见 1.4） |
| `system` 数组格式 | ❌ **触发 400**（见 1.1） | ❓ 未知 | ❓ 未知 |
| `document` 内容块 | ❓ 未知 | ❓ 未知 | ❌ **触发 400**（见 1.2） |
| `tools[].defer_loading` | ❓ 未列出（推测忽略） | ❓ 无文档 | ❓ 无文档（但 MFJS 极严格） |
| `tools[].eager_input_streaming` | ❓ 同上 | ❓ 无文档 | ❓ 同上 |
| `cache_control`（system 内） | ❌ **触发 400**（见 1.1） | ❓ 无文档 | 🟡 忽略 |
| `cache_control`（message 内） | 🟡 文档标 "Ignored" | ❓ 无文档 | 🟡 忽略 |
| `anthropic-beta` 头 | 🟡 Ignored | ❓ 无文档 | ❓ 无文档 |
| Schema 验证策略 | **Lenient** | **未知**（有参数验证） | **STRICT**（MFJS + Walle） |

> DeepSeek 兼容性表（最好）：https://api-docs.deepseek.com/guides/anthropic_api/
> GLM Claude Code 指南（无字段表）：https://docs.z.ai/scenario-example/develop-tools/claude
> KIMI Claude Code 指南：https://platform.kimi.com/docs/guide/claude-code-kimi

---

## 3. 不可避免的 trade-off（非冲突，用户需知情）

### 3.1 Remote Control 禁用（硬编码，无绕过）

> env-vars 文档原文："As of v2.1.196, Remote Control is disabled when `ANTHROPIC_BASE_URL`
> points at a host other than `api.anthropic.com`."

没有任何环境变量可以绕过。用户如果依赖手机推送，走网关就失去这个功能。

### 3.2 Prompt cache 失效

DeepSeek / KIMI 忽略 `cache_control`（KIMI 用自己的基于内容的自动缓存）。GLM 未知。
**不会报错**，只是不省钱。但对 DeepSeek，`system` 数组内的 `cache_control` 会触发 400（见 1.1）。

### 3.3 上下文窗口不匹配

删除 `ANTHROPIC_MODEL` 后 Claude Code 发 `claude-sonnet-5`（内置可识别），context window
按内置值（200K / 1M）。但实际路由到的模型可能只有 128K → 超长请求 400。

无法自动解决，需文档提示。

---

## 4. 伪冲突（已排除）

| 项目 | 初版结论 | 重新评估 | 理由 |
|------|---------|---------|------|
| `API_FORCE_IDLE_TIMEOUT=0` | 🔴 高优先 | ⬜ 不需要 | 商业 API 供应商不会 5 分钟不出字节（SSE keepalive） |
| MCP tool search 禁用 | 🟡 功能损失 | 🟢 反而更安全 | 禁用 = 不发 `defer_loading` = 更兼容 |
| `DISABLE_INTERLEAVED_THINKING=1` | 🔴 独立冲突 | ⬜ 冗余 | 被 `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` 完全包含 |
| Fast mode 检查 | 🟡 冲突 | ⬜ 无关 | 检查直连 `api.anthropic.com`，不经过网关 |
| `CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK` | 🔴 高优先 | 🟡 锦上添花 | 修 HTTP/2 stale 后已很少触发 |

---

## 5. 已正确处理

| 项目 | 代码依据 |
|------|---------|
| `ANTHROPIC_AUTH_TOKEN` 占位值不泄露 | `HOP_HEADERS` 剥除 `authorization` 头 |
| 客户端 `x-api-key` 不干扰供应商 | `HOP_HEADERS` 剥除 |

---

## 6. 修改清单

基于以上分析，以下是需要实施的代码修改。按实施位置分三层，每条标注工作量和优先级。
**审核确认后再实施。**

### 第一层：客户端环境变量（`config_server.py` `_setup_claude_code`，改 1 行）

| # | 修改 | 修复的问题 | 工作量 | 优先级 |
|---|------|-----------|--------|--------|
| M1 | `env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"` | 阻止 Claude Code 发送 `defer_loading` / `eager_input_streaming` / beta 头；三家供应商对这几个字段行为全部未知 | 1 行 | **P0** |

> **理由**：这是唯一一个客户端侧就能解决的问题。设了之后 Claude Code 自己就不发这些字段，不需要网关清理。副作用：fine-grained tool streaming 在网关连接上默认关闭（但本来对非 Anthropic host 就是关闭的，无额外损失）。

### 第二层：网关按供应商清理 body（新增 `suanpan/compat.py`，`proxy.py` 调用）

当前 `forward_request` 路由决策后、转发前，插入一个 per-provider body 标准化步骤。

| # | 修改 | 修复的问题 | 涉及供应商 | 工作量 | 优先级 |
|---|------|-----------|-----------|--------|--------|
| M2 | `system` 数组 → 字符串（拼接 text 块，丢弃 `cache_control`） | DeepSeek 反序列化 400（[Issue #1369](https://github.com/deepseek-ai/DeepSeek-V3/issues/1369)） | DeepSeek（GLM/KIMI 待测，建议全量做） | 小（`router.py` 已有 `_extract_system_text` 可复用） | **P0** |
| M3 | 剥除 `document` 类型内容块 | KIMI 直接 400（[Issue #129](https://github.com/MoonshotAI/Kimi-K2/issues/129)） | KIMI（其他家未知，建议全量做） | 小 | **P0** |
| M4 | 剥除 `tools[].defer_loading` + `tools[].eager_input_streaming` | 三家全部未知，不可假设安全 | 全量（M1 的兜底） | 小 | **P1** |
| M5 | 剥除 `system` 数组项内的 `cache_control` | DeepSeek 1.1 的根因之一；其他家无意义 | 全量 | 极小（含在 M2 内） | **P0** |

**架构建议**：

```python
# suanpan/compat.py
def normalize_body(body: dict, provider: str) -> None:
    """Per-provider body normalization before forwarding."""
    _flatten_system(body)          # M2+M5: array → string
    _strip_document_blocks(body)   # M3: document → text or remove
    _strip_beta_tool_fields(body)  # M4: defer_loading, eager_input_streaming
```

`proxy.py` `forward_request` 第 99 行后加一行：
```python
body["model"] = target_model
normalize_body(body, provider_name)  # ← 新增
```

> **不做全量 per-provider 分支**——M2/M3/M4 对三家都安全（剥离无害字段不会导致功能损失，
> 因为供应商本来就不认），所以统一执行比分 provider 判断更简洁。如果将来某供应商开始支持
> `thinking` 或 `cache_control`，再加条件分支。

### 第三层：不立即做，记录为已知限制

| # | 问题 | 为什么暂不处理 | 建议 |
|---|------|---------------|------|
| M6 | KIMI MFJS schema 严格验证 | 需要递归遍历 JSON Schema 补 `type`、修 `anyOf` 结构，工作量中等偏大且容易引入新 bug | 文档标注；等实际用户报告再做 |
| M7 | KIMI `thinking` 模型相关性（k2.7-code 强制 `type=enabled`） | 需要在 `suanpan.yaml` 加 per-model 配置；当前用户配的模型主要是 k3，默认就开 | 文档提示；等用户报错再加 |
| M8 | 上下文窗口不匹配 | 无法自动解决（网关不知道 Claude Code 侧假设的窗口大小） | 文档提示用户手动设 `CLAUDE_CODE_MAX_CONTEXT_TOKENS` |
| M9 | GLM 参数验证 1213 | Z.ai 无文档，根因不明 | 需实测复现后再定方案 |

### 实施顺序

```
Phase 1（快修）: M1 + M2 + M3 + M5    ← 解决已确认的实际 400
Phase 2（加固）: M4                    ← 兜底防御
Phase 3（按需）: M6 / M7 / M8 / M9     ← 等用户报告
```

Phase 1 的 4 项加起来约 30-40 行代码（新增 `suanpan/compat.py` ~25 行 + `proxy.py` +2 行 +
`config_server.py` +1 行），附带测试约 8-10 个 case。

---

## 参考链接

- [Claude Code 环境变量文档](https://code.claude.com/docs/en/env-vars)
- [DeepSeek Anthropic 兼容性表](https://api-docs.deepseek.com/guides/anthropic_api/)
- [KIMI Claude Code 指南](https://platform.kimi.com/docs/guide/claude-code-kimi)
- [GLM Claude Code 接入](https://docs.z.ai/scenario-example/develop-tools/claude)
- 接入指南：[`docs/claude-code-env.md`](claude-code-env.md)
- 网关转发逻辑：[`suanpan/proxy.py`](../suanpan/proxy.py) `forward_request()`
- Header 过滤：[`provider_auth.py`](../provider_auth.py)
