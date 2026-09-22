# ADR-010: 网关协议矩阵（直通优先）+ Agent 自动配置契约

- 状态：Accepted
- 日期：2026-09-21
- 决策者：tech-lead（用户确认「一个 Key 进，所有 Agent 通」方向 + 零新增代码文件约束）
- 影响范围：`suanpan/{config,compat,main,proxy,usage_extractor}.py`、`shared/provider_auth.py`、`services/{balance_usage,config_server,claude_code_setup}.py`、`shellui/{config_ui.html,menu_builder.py}`、`docker/entry.py`
- 关联：ADR-002（掩码契约）、ADR-003（Claude Code env 契约）、ADR-004（前缀稳定性与 cache_control）；issue #7（RetryPolicy）、#9（keyless 剥凭证）

## 上下文

产品方向：用户只输入「厂商 + API Key」，网关自动完成协议适配，并把各编程 Agent
（Claude Code / Codex / OpenCode / ZCode…）自动配置指向本网关。调研钉死的事实：

- **Codex CLI 已于 2026-02 移除 `wire_api="chat"`**（openai/codex#7782 + PR#10157），
  是纯 OpenAI **Responses API** 客户端（`POST {base}/responses`）；其
  `experimental_bearer_token` 字段是官方认可的编程化写 key 途径。
- 主流厂商**几乎全部同时提供** Anthropic 兼容端点与 OpenAI Chat 兼容端点，
  但路径与认证头惯例不一（Anthropic 生态 base 不含 `/v1`；OpenAI 生态 base
  含版本段；个别厂商路径特殊——MiniMax 的 chat 路径、火山方舟的
  `/api/v3/messages`——不进内置模板）。
- OpenCode 可自选协议（`@ai-sdk/anthropic` 或 `@ai-sdk/openai-compatible`），
  但 **models 块必写**否则 provider 配置静默失效；ZCode 的 provider 带
  `kind` 字段选协议。

## 决策一：协议矩阵——直通优先，失配才转换

入站三协议 × 出站端点协议，能对上就走**直通车道**（不整体转译协议）：

| 入站 \ 出站 | anthropic 端点 | openai chat 端点 | responses 端点 |
|---|---|---|---|
| `/v1/messages`（Claude Code/ZCode） | **直通**（现状） | **转换 A** | 不发生（引导避开） |
| `/v1/chat/completions`（OpenCode 可选/Kilo/Grok/Droid） | 不实现（协议引导消灭） | **直通** | 不发生 |
| `/v1/responses`（Codex） | **转换 C**（可延后） | 转换 C 同族 | **直通** |

「Chat 入站 → Anthropic 出站」这一族**刻意不实现**：所有内置厂商都有 openai
端点，而能说 Chat 协议的 Agent（OpenCode/Droid）也都能改说 Anthropic 协议，
向导按探测结果引导即可消灭该象限。

### 直通车词语义（各车道统一的窄改写集）

- 请求侧仅做：`model` 改写为路由目标、（openai 直通车道）注入
  `stream_options.include_usage=true` 以获得末块用量、（anthropic 车道）
  既有 `normalize_body` 归一化。**不做其它 body 改写**。
- 响应侧**字节直通**（SSE 原样转发），用量经 tee 提取。
- anthropic×anthropic 是现状主力路径，行为零变化（金样本回归钉住）。

### 转换 A：Anthropic Messages ⇄ OpenAI Chat（Claude Code 用 OpenAI 系厂商）

确定性纯函数，住在 `suanpan/compat.py`（协议适配唯一归宿，与既有归一化
同族）。请求向映射：`system`（含 blocks）→ 首条 `system` 消息；content
blocks → 多部分文本；`tool_use` → `tool_calls`；`tool_result` → `tool` 角色
消息；`max_tokens`/`stop_sequences`/`temperature` 等同名直传；`cache_control`
丢弃（OpenAI 协议无此概念）。响应向（含 SSE 流式翻译为 Anthropic 事件序列）：
`finish_reason` 映射 `stop→end_turn`、`length→max_tokens`、`tool_calls→tool_use`；
用量从末块 `usage` 注入翻译后的 `message_start`/`message_delta`。**翻译后的
字节流即合法 Anthropic SSE，UsageExtractor 无需感知 openai 格式**。

### 转换 C：Responses ⇄ Anthropic（Codex 用无 Responses 端点的厂商）

独立里程碑（M3b，可砍）。请求向：`instructions` → `system`；input items 的
`message`/`function_call`/`function_call_output` → Anthropic 消息/`tool_use`/
`tool_result`；`reasoning` items **丢弃**（Anthropic thinking block 需签名，
伪造即拒；重推理的成本记入用量可见）。响应向：Anthropic SSE 翻译为
Responses 事件序列（`response.created`/`output_item.added`/`output_text.delta`/
`response.completed` 等）。砍掉时向导对该组合标「暂不支持」。

### 路径与认证惯例（注册表端点卡的契约）

- `protocol=anthropic`（默认，现状）：出站 `base_url + /v1/messages`；
  `auth_header` 沿用现有字段（x-api-key 或 Bearer）。
- `protocol=openai`：出站 `base_url + /chat/completions`（**OpenAI SDK 惯例：
  base 含版本段**，如 `.../v1`、`.../compatible-mode/v1`）；认证恒
  `Authorization: Bearer`（研究核实所有 openai 端点均 Bearer）。
- responses 直通（M3a）：注册表端点卡提供 `responses` base，经模板/向导
  落为 `ProviderConfig.responses_base_url`（配置态消费，非运行时 host
  匹配）；出站 `base + /responses`；恒 Bearer。
- `/v1/models` 返回超集形状（现有 Anthropic 字段 + `object:"model"` /
  `created` / `owned_by`），两种 SDK 均可消费。
- `count_tokens` 仅服务 Anthropic 入站；openai 协议 provider 收到该请求时
  返回带说明的错误（OpenAI 协议无等价端点，不做估算）。

## 决策二：注册表协议化（`shared/provider_auth.py` 原地扩展）

`PROVIDER_REGISTRY` 每厂商增加 `endpoints` 端点矩阵：

```python
"endpoints": {
    "anthropic": {"base_url": "...", "anthropic_native": bool},   # auth_header 可选
    "openai":    {"base_url": "..."},                              # 恒 Bearer
    "responses": {"base_url": "...", "unverified": True},          # 存疑标记
}
```

- 顶层 `base_url`/`anthropic_native` 保留为 anthropic 卡的兼容投影（现有
  消费方零迁移）。
- `ProviderConfig`（`suanpan/config.py`）新增 `protocol:
  Literal["anthropic","openai"] = "anthropic"`——**单主端点**模型：一个
  provider 一个 base_url + protocol，转换层桥接一切协议组合，不为同厂商
  开多个 provider 条目。
- 路径特殊不进内置模板的厂商（MiniMax chat、火山方舟 anthropic）经自定义
  provider 手配。

### 端点连通性探测（三级）

`POST /api/probe-provider`（`services/balance_usage.py` 实现，它本就是出站
探测知识的归宿）：

1. **存在性**：对 POST-only 端点发 GET——405 = 路由存在，404 = 无
   （零成本零副作用）；
2. **认证**：401/403 = 端点在但 Key 无效；
3. **可用性确证**：一条最小真实请求 + `GET /v1/models` 拉模型清单。

返回每协议 `{reachable, auth_ok, latency_ms, models}`；注册表 `unverified`
标记项（如智谱 responses）由探测实证后转正。

## 决策三：Agent 自动配置契约（`claude_code_setup.py` 原地泛化）

沿用 Claude Code 同步已验证的五件套模式（owned-keys 单一真源、
plan/preview 共享计划、first_write 备份、幂等 already、diff 确认），泛化为
注册表引擎，每 Agent 一个 entry：

- **token**：单一 `local_client_token` 复用于所有 Agent（Bearer）——
  CC `ANTHROPIC_AUTH_TOKEN`（现状）、Codex `experimental_bearer_token`、
  OpenCode `options.apiKey`、ZCode `options.apiKey`。写入 Agent 配置的是
  **本地回环凭证，不是厂商真 key**；真 key 永不出网关进程（issue #9 边界
  不变）。
- **owned-keys**：每 entry 声明自己拥有的配置键，wipe-set 只删自己的
  （CC 的角色 env 契约见 ADR-003，不外溢到其他 Agent）。
- **Codex**：只写用户级 `~/.codex/config.toml`（项目级有 denylist）；
  tomlkit 增量编辑（Codex 自身 `/model` 会写回，必须 merge-not-overwrite）；
  独立 provider id（内置 `openai` id 不可覆盖）；顶层 `model` +
  `model_provider`。
- **OpenCode**：`opencode.json` provider 块 + **models 块必写**（坑：
  无 models 时 npm 静默忽略）+ 上下文限额；JSONC 解析失败安全降级提示
  手动处理。
- **其他 Agent 只写「默认模型」一个选择**，不做角色映射表——CC 的四角色
  表是其固定 env 契约的产物，Codex/OpenCode/ZCode 各自有模型选择 UX。
- Docker 形态：`sync-agent <name> [--dry-run]` 泛化现有 `sync-claude-code`。

## 后果

- Claude Code 可用 OpenAI/OpenRouter/通义/硅基流动等一切 openai 协议厂商
  （转换 A）；Codex 开箱即用 OpenAI/DeepSeek/GLM/Kimi（responses 直通），
  通义/MiniMax 等待转换 C；OpenCode/Droid/Kilo/Grok 经 chat 直通接入。
- `suanpan/compat.py` 章程升格为「协议适配唯一归宿」（归一化 + 双向转换，
  预计 ~800 行深模块）；**全计划零新增代码文件**（唯二新增：本 ADR 与
  tomlkit 依赖）。
- Gemini CLI 不支持（Gemini 私有协议，openaiCompatible 实验已移除），
  向导标注；未来若做 generateContent 端点另立 ADR。
- 新增依赖 tomlkit（纯 Python/MIT，零传递依赖）——仅 services 层使用，
  网关（suanpan/）零新依赖不变。
