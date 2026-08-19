# CC-Switch v3.20.0 上游 Prompt Cache 架构审查

- 日期：2026-08-19
- 审查对象：CC-Switch v3.20.0，固定提交 [`0b5da510168914b251481654a568c3ffacd62cf4`](https://github.com/farion1231/cc-switch/tree/0b5da510168914b251481654a568c3ffacd62cf4)
- 一手材料：用户本地官方仓库源码；本文所有源码链接均固定到上述提交
- 范围：上游 prompt caching。响应/协议回放状态、认证 token、用量查询等内部缓存只用于划清边界，不把它们误称为 prompt cache

## 结论摘要

1. **CC-Switch 没有本地模型响应缓存。** 每个 `/v1/messages` 请求都会进入 `forward_with_retry`，每个通过校验、可发出的 provider attempt 都会真实执行 HTTP `send`；本地没有按 prompt 查找并直接返回 completion 的命中路径。[请求入口](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/handlers.rs#L165-L267)；[上游发送](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L2181-L2313)
2. **它实际协调三种互不等价的缓存信号：** Anthropic Messages 的 `cache_control` 断点、OpenAI Chat/Responses 的 `prompt_cache_key` 路由，以及 Codex OAuth 的稳定会话 headers。它们不能由一个“兼容模式”布尔值准确表达。[ProviderMeta 字段](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/provider.rs#L481-L507)
3. **Claude → Anthropic identity 基本保留调用方的 `cache_control`；跨协议转换则重建 body 并删除它。** Claude → OpenAI Responses 可改用 `prompt_cache_key`，Claude → OpenAI Chat 只有显式 provider key 才注入，Claude → Gemini 不创建任何 Gemini cached-content 资源。
4. **自动断点注入有两条入口：** 显式开启的 Bedrock Request Optimizer，以及默认开启的 Codex Responses → Anthropic bridge。两者复用同一套最多 4 个断点算法，但新断点固定为 Anthropic 默认 5 分钟 TTL；源码不存在可配置 TTL 字段。[注入器](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/cache_injector.rs#L8-L137)
5. **缓存真正位于上游。** 故障转移会为每个 provider 重做协议、模型、认证和 body 处理，并不会迁移缓存。应把 provider/account/model/格式变化按“冷缓存”处理，除非两个配置实际上落到相同上游缓存命名空间且最终 prefix 完全相同；这是基于源码边界作出的运营推论，不是 CC-Switch 提供的跨 provider 保证。

## 1. 总体数据流与责任边界

```text
客户端请求
  │
  ├─ 提取真实客户端 session（没有则生成仅供日志用的 UUID）
  │
  ├─ 每个 provider 独立执行
  │    ├─ 模型映射 / wire format 转换
  │    ├─ Anthropic: 保留或注入 cache_control
  │    ├─ OpenAI: 可注入 prompt_cache_key
  │    ├─ provider override / 私有字段过滤
  │    └─ JSON canonicalization
  │
  ├─ 总是发送到上游；上游拥有并命中/写入 prompt cache
  │
  └─ 响应 usage 跨协议归一化
       ├─ fresh input
       ├─ output
       ├─ cache read
       └─ cache creation/write
            → SQLite 明细 / 日聚合 → 分桶计价 → UI
```

`apiFormat` 决定真实 wire protocol：`anthropic` 为默认 identity，另有 `openai_chat`、`openai_responses`、`gemini_native`；源码中没有名为 `anthropic_native` 的缓存开关。[格式解析与默认值](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/claude.rs#L37-L104) 缓存策略另由 `promptCacheKey`、`promptCacheRouting`、`optimizer_config.cacheInjection` 以及请求自身字段决定，因此 wire format 与 cache policy 是两个维度。

## 2. 请求路径矩阵

| 客户端 → 上游 wire format | `cache_control` | `prompt_cache_key` / 等价信号 | 实际控制点 |
|---|---|---|---|
| Claude → Anthropic identity | 调用方字段保留；Bedrock optimizer 可补断点 | 不转换为 OpenAI key | `apiFormat=anthropic`；Bedrock optimizer 另行控制 |
| Claude → OpenAI Responses | system/message/tool 上的 Anthropic 字段随结构转换删除 | provider `promptCacheKey` 优先，否则使用真实客户端 session | `ProviderMeta.promptCacheKey`；无真实 session 时不注入 |
| Claude → OpenAI Chat | Anthropic 字段删除 | **仅**显式 provider `promptCacheKey`；不会自动使用 session | `ProviderMeta.promptCacheKey` |
| Claude → Gemini Native | 重建为 `systemInstruction`/`contents`/`tools`，Anthropic 字段不进入结果 | 没有 `prompt_cache_key`，也没有创建/引用 Gemini `cachedContent` | 无请求侧 prompt-cache 配置；只解析响应的 cache-read usage |
| Codex Responses → Chat | Responses 本来没有 Anthropic 断点 | 三态 `promptCacheRouting` 决定是否注入；请求自带 key 优先，其次真实客户端 session | `auto/enabled/disabled` |
| Codex Responses → Anthropic | 转换后自动补最多 4 个 5m 断点 | 不使用 OpenAI key | `optimizer_config.cacheInjection`；不受 optimizer 总开关约束 |
| Codex → native Responses | 原样保留 native body 信号，受特定 provider 清洗影响 | CC-Switch 不自动补 body key；Codex OAuth 可补稳定 session headers | xAI 会删除不支持的 `prompt_cache_retention`，但清洗是确定性的 |
| Claude → Bedrock/Anthropic | 调用方字段保留，并可补最多 4 个断点 | 无 OpenAI key | `CLAUDE_CODE_USE_BEDROCK=1` 且 optimizer 总开关与 cache 子开关均开启 |

矩阵的转换依据来自 Claude 分派函数：[Responses、Chat、Gemini、identity 四分支](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/claude.rs#L345-L462)。

## 3. Claude → Anthropic identity

### 3.1 保留与修改边界

`anthropic` 是默认 wire format，最终 transform 的 fallback 直接返回原 body；因此 `system`、`messages`、`tools` 上原有的 `cache_control` 及调用方给出的 `ttl` 不会被通用转换器删除。[identity 返回 body](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/claude.rs#L401-L462)

这不是字节级“完全透传”，请求仍可能经历以下处理：

- provider 定向的 Anthropic normalizer 只处理 DeepSeek/MiMo thinking history，以及 DeepSeek `thinking: disabled` 与 effort 冲突，没有通用删除 `cache_control` 的分支。[Anthropic 定向 normalizer](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/claude.rs#L113-L247)
- 模型映射、媒体 fallback、provider body override 都可能改变 prefix。媒体 fallback 把图片换成文本时会显式把原 block 的 `cache_control` 搬到替代 block，尽量不断开断点语义。[媒体替换保留 marker](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/media_sanitizer.rs#L397-L409)
- 出站前递归删除下划线开头的私有字段，随后 canonicalize JSON object key；`cache_control` 不以 `_` 开头，所以不受该过滤规则影响。[私有字段过滤](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/body_filter.rs#L68-L117)；[最终 prepare](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L3570-L3572)

### 3.2 Bedrock 条件注入

Bedrock 身份只由 provider env 中 `CLAUDE_CODE_USE_BEDROCK == "1"` 判断。[识别函数](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L2746-L2755) 在 failover 循环中，只有 `optimizer.enabled` 且当前 attempt 是 Bedrock 时才 clone 原 body 并执行 thinking/cache optimizer；非 Bedrock attempt 拿到另一份原始 clone，因此注入结果不会泄漏到下一个 provider。[per-provider clone 与注入](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L479-L493)

配置存入 settings 的 `optimizer_config`，字段只有 `enabled`、`thinkingOptimizer`、`cacheInjection`；总开关默认 false，两个子开关默认 true。[配置 schema/default](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/types.rs#L243-L268) UI 也明确称为 “Bedrock Request Optimizer”。[设置页](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src/components/settings/RectifierConfigPanel.tsx#L147-L205)

## 4. Claude → OpenAI / Gemini

### 4.1 OpenAI Responses

转换器把 Anthropic `system` 合并成 `instructions`、把 message blocks 重建成 `input`、把 tools 重建成 Responses function tools；Anthropic `cache_control` 没有对应输出字段。源码明确说明 Responses 的 `input[].content[]` 不接受它，测试也覆盖 text 与 tool 两类删除。[请求重建](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform_responses.rs#L1762-L1805)；[content 删除](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform_responses.rs#L2305-L2319)；[回归测试](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform_responses.rs#L4687-L4733)

替代信号是 top-level `prompt_cache_key`：

1. provider meta 的 `promptCacheKey` 优先；
2. 没有显式 key 时，Claude/Copilot 可使用真实客户端 session/thread identity；
3. forwarder 只把 `session_client_provided=true` 的 session 交给转换器，生成的请求级 UUID 不会上送；
4. 转换器最后把 key 写进 body。[key 选择](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/claude.rs#L345-L415)；[注入点](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform_responses.rs#L1950-L1953)

另外，Claude Code system 开头可能有动态 `x-anthropic-billing-header: ...` 行。Chat/Responses 转换都会只删除开头这一行，原因正是旋转值会令每轮 prompt prefix 不同、破坏缓存复用。[prefix 稳定化](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform.rs#L16-L54)

### 4.2 OpenAI Chat

Chat 转换重建 `messages` 与 tools，测试明确验证 system、message content、tool 上所有 `cache_control` 均被去掉，且基础 transformer 自身不会凭空注入 `prompt_cache_key`。[转换实现](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform.rs#L126-L241)；[cache_control/key 测试](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform.rs#L1463-L1504)

生产分支仅在 provider meta 配置了 `promptCacheKey` 时写入；与 Responses 分支不同，它不会自动用 Claude session 补 key。[Chat 分支](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/claude.rs#L434-L453) 流式请求同时补 `stream_options.include_usage=true`，否则许多 Chat 兼容上游不返回 usage，缓存命中 token 也会漏记。[usage 注入](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform.rs#L244-L269)

### 4.3 Gemini Native

Gemini 转换从零构造 `systemInstruction`、`contents`、`generationConfig` 与 `functionDeclarations`，没有转发 Anthropic `cache_control`，也没有产生 `prompt_cache_key` 或 Gemini `cachedContent` 字段。[Gemini 请求构建](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform_gemini.rs#L50-L120) 但响应侧会读取 `cachedContentTokenCount`，把它单列为 Anthropic `cache_read_input_tokens`。[Gemini usage 转换](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform_gemini.rs#L1197-L1238)

因此“看见 Gemini cache-read usage”不代表 CC-Switch 主动建立了 Gemini 缓存资源；它只是观测并归一化上游报告。

## 5. Codex Responses → Chat：`promptCacheRouting` 三态

该配置只控制 **Responses → Chat 转换后是否允许发送 `prompt_cache_key`**，不是响应缓存开关，也不控制 Anthropic `cache_control`。

- `enabled`：无条件允许注入；严格网关若不认识字段，可能返回 400。
- `disabled`：不注入。
- `auto`（缺省或其他值）：只对 `api.openai.com`，以及 `api.kimi.com/coding[/...]` 开启；未知兼容网关默认关闭。[三态与 host allowlist](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/codex.rs#L87-L131)

允许注入后，key 选择顺序是：转换前请求体已有的 `prompt_cache_key` > 真实客户端 session。两者都没有就不写；生成的 UUID 永远不能参与。[注入函数](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/codex.rs#L133-L160) Forwarder 在转换前先保存显式 key，完成 Responses → Chat 后再调用此函数。[生产调用](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L1460-L1490)

Codex session 来源与稳定性约束：

- headers `session_id` 或 `x-session-id`，trim 后长度大于 20；
- fallback 为 `body.metadata.session_id`，长度大于 10；
- 两者都会加 `codex_` 前缀；
- `previous_response_id` 被明确排除，因为它通常是每轮随机变化的 response cursor；
- 全部缺失时虽然内部生成 UUID供日志关联，但 `client_provided=false`，不能成为 cache key。[session 提取](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/session.rs#L71-L102)；[Codex 规则](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/session.rs#L126-L176)

UI 将该字段直接存为 `promptCacheRouting: auto|enabled|disabled`，提示语也写明只使用客户端提供的稳定 session。[UI](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src/components/providers/forms/CodexFormFields.tsx#L1071-L1111)

## 6. Anthropic 断点注入：TTL 与最多 4 个断点

### 6.1 算法

注入器首先统计已有断点，用 `4 - existing` 作为新增预算，然后按次序尝试：

1. 最后一个 tool；
2. 最后一个 system block；字符串 system 会先变成单个 text block 数组；
3. 从后向前找到最后一条可缓存消息，在其最后一个非 `thinking` / `redacted_thinking` block 上加断点；
4. history 至少 4 条时，再在倒数第二个 user message 上加旧锚点，用于长 tool-result 回合、降低稳定 prefix 掉出 20-block lookback 的概率。

完整实现见[注入顺序和预算](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/cache_injector.rs#L14-L110)；[message block 选择](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/cache_injector.rs#L113-L137)。

已有 marker 属于调用方：已有 4 个就 no-op；超过 4 个只 warning、原样交给上游，不删除、不重排。已有 marker 的 `ttl: "1h"` 也会保留。[已有 marker/TTL 测试](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/cache_injector.rs#L243-L319)

### 6.2 TTL 的真实实现

新 marker 恒为：

```json
{"type":"ephemeral"}
```

即省略 `ttl`，代码与 UI 均把它解释为标准 5 分钟。[marker 构造](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/cache_injector.rs#L135-L137)；[5m 测试](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/cache_injector.rs#L340-L353)；[UI 文案](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src/i18n/locales/en.json#L414-L422)

源码没有 TTL 配置字段。Forwarder Codex → Anthropic 分支中“reuse configured TTL”的注释与真实代码不一致；同文件稍后的 helper 明确写的是 standard 5-minute TTL。[过时注释所在调用点](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L1534-L1545)；[真实 bridge 配置](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L3016-L3027)

### 6.3 两个调用入口并不完全同构

- **Bedrock：** 必须 `optimizer.enabled=true` 且 `cacheInjection=true`。
- **Codex → Anthropic：** bridge 在 Responses body 转成 Anthropic body 后总是调用 injector，并构造 `enabled=true` 的临时 config；只读取共享的 `cacheInjection` 子开关，**忽略 optimizer 总开关**。[转换和注入](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L1491-L1546)；[bridge config](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L3016-L3027)

这造成一个 UI/配置耦合：总开关关闭时 cache 子开关在 UI 中不可操作，但其默认值仍为 true，Codex → Anthropic bridge 因此通常仍会注入。`OptimizerConfig` 上“仅对 Bedrock provider 生效”的注释也已不完整。

### 6.4 算法边界

`count_existing` 只统计 `tools[*]`、`system[*]` 和每条 message 的第一层 `content[*]`；它不是递归扫描，因此不会发现更深层（例如嵌套 tool-result content）的 marker。[计数范围](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/cache_injector.rs#L139-L168) 所以“最多 4 个”的保证只对它识别的结构成立；这是 Magic 若复用算法时应补强的地方。

## 7. Codex Responses → Anthropic

是否接管由 provider 明确配置的 `apiFormat` / `wire_api=anthropic` 决定，不按 base URL 猜测；只有 Responses endpoint 才触发 bridge。[判断逻辑](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/codex.rs#L162-L207)

处理顺序是：provider 模型映射 → Responses body 转 Anthropic Messages → 可选 Claude Code impersonation system prompt → 注入 cache breakpoints → 私有字段过滤/canonicalization → 发送。请求原来没有 Anthropic `cache_control`，因此通常由 injector 新建 3～4 个标准 5m 断点。[bridge 主路径](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L1491-L1546)

响应侧会反向恢复 OpenAI Responses 的计量语义：Anthropic `input_tokens` 是 fresh input，转换器把 `fresh + cache_read + cache_creation` 写为 Responses `input_tokens`，并把两类缓存 token 放进 `input_tokens_details.cached_tokens/cache_write_tokens`。[响应 usage 反向转换](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform_codex_anthropic.rs#L139-L200)

## 8. Canonicalization、prefix stability 与 headers

### 8.1 稳定化措施

- 所有最终出站 body 都递归排序 JSON object keys；数组顺序保持不变。[canonicalize 实现](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/json_canonical.rs#L1-L49)
- Responses → Chat 对 tool arguments、JSON tool output、嵌入 description 的 tool definition 使用 canonical JSON，避免 map 插入顺序变化导致 prefix byte drift。[tool output](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform_codex_chat.rs#L701-L721)；[tool definition/arguments](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform_codex_chat.rs#L1252-L1266)
- Claude → OpenAI 去掉 system 开头动态 billing header。
- xAI native Responses 清洗只做确定性字段删除/结构提升，明确以 prompt-cache prefix 稳定为约束；其中会删除 xAI 不支持的 `prompt_cache_retention`。[xAI 清洗契约](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform_codex_responses_xai_sanitize.rs#L1-L18)；[字段表](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform_codex_responses_xai_sanitize.rs#L28-L41)
- Debug `CacheTrace` 只记录 key 是否存在/长度，以及 instructions/system/tools/input/messages/include/body 的短 hash 和 marker TTL 汇总，不记录 prompt/key 明文；它是诊断，不是缓存存储。[CacheTrace](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L3574-L3658)

Canonicalization 只能消除 object key 顺序差异，不能弥补数组顺序、文本、tool list、system、模型映射、provider override、历史补全等真实变化。稳定 session key 也只是路由提示，不等于 prefix 内容相同。

### 8.2 Header 行为

- Claude → Anthropic identity 才保留/重建 Anthropic 协议 headers；`anthropic-beta` 会确保包含 `claude-code-20250219`，同时保留客户端原 beta 值。[beta 构建](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L1908-L1940)
- Codex → Anthropic 默认补 `anthropic-version: 2023-06-01`。`anthropic-beta` 只因 Claude Code impersonation 或 `[1m]` context marker 添加；普通 prompt caching 本身不要求 beta。[版本与 beta 边界](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L2153-L2173)
- 认证 header 被 provider auth 替换；连接/CDN/trace headers 被过滤；`anthropic-version` 只在 Anthropic wire path 透传，其他未命中过滤规则的 headers 默认透传。[header filter](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L1953-L2117)
- Codex OAuth 仅在 session 确为客户端提供时，发送同一稳定值到 `session_id`、`x-client-request-id` 和 `${session}:0` 形式的 `x-codex-window-id`；生成 UUID 不发送，避免每轮打散 prefix/cache identity。[headers 构造与条件](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L1817-L1822)；[具体 header](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L3293-L3313)

HTTP `Cache-Control` header 即使经默认规则透传，也不是本文的 Anthropic body `cache_control`，源码没有用它做 prompt-cache 断点或本地 completion cache。

## 9. Cache usage：解析、归一化、持久化、计价与展示

### 9.1 上游字段解析

内部统一为四个计量桶：`input_tokens`、`output_tokens`、`cache_read_tokens`、`cache_creation_tokens`。[TokenUsage](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/usage/parser.rs#L57-L99)

| 响应格式 | cache read 来源 | cache write/create 来源 |
|---|---|---|
| Anthropic non-stream / stream | `cache_read_input_tokens` | `cache_creation_input_tokens` |
| OpenAI Responses / Chat | 顶层 `cache_read_input_tokens`，或 `input_tokens_details.cached_tokens`，或 `prompt_tokens_details.cached_tokens`；DeepSeek 再 fallback `prompt_cache_hit_tokens` | 顶层 `cache_creation_input_tokens`，或 details 中 `cache_write_tokens` |
| Gemini | `usageMetadata.cachedContentTokenCount` | 不报告，记 0 |

字段优先级见[OpenAI helpers](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/usage/parser.rs#L12-L35)、[Anthropic non-stream/stream](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/usage/parser.rs#L102-L245)、[Codex/OpenAI](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/usage/parser.rs#L248-L379)、[Gemini](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/usage/parser.rs#L381-L407)。即使 input/output 都是 0，只要 cache read/write 非 0，也被视为可计费 usage，不会丢弃。

### 9.2 跨协议响应归一化

这是防止缓存 token 双算的关键：

- OpenAI Chat → Anthropic：上游 `prompt_tokens` 含缓存子集，先减 cache read/write 得 fresh input，再单列两个 cache 桶。[Chat → Anthropic usage](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform.rs#L685-L743)
- OpenAI Responses → Anthropic：同样从 inclusive input 减 read/write，direct Anthropic-style 字段优先于 nested details。[Responses → Anthropic usage](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform_responses.rs#L2112-L2255)
- Gemini → Anthropic：`promptTokenCount - cachedContentTokenCount = fresh input`。[Gemini 归一化](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform_gemini.rs#L1208-L1238)
- Chat → Codex Responses：保留 inclusive `input_tokens`，把 cache read/write 放进 `input_tokens_details`；也兼容 DeepSeek 的 `prompt_cache_hit_tokens`。[Chat → Responses usage](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform_codex_chat.rs#L1860-L1913)
- Anthropic → Codex Responses：fresh + read + write 恢复为 inclusive input，细节分桶。[Anthropic → Responses usage](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform_codex_anthropic.rs#L139-L200)

### 9.3 落库与语义标记

SQLite `proxy_request_logs` 分列保存四类 token、四类 cost，以及 `input_token_semantics`；`model_pricing` 也分开保存 input/output/cache-read/cache-create 每百万 token 价格。[表结构](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/database/schema.rs#L194-L244) 旧明细被归档时，`usage_daily_rollups` 继续保留两类 cache token 和 input semantics。[日聚合 schema](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/database/schema.rs#L272-L297)

写日志时 `codex/gemini/grokbuild` 标为 TOTAL（input 含 cache），其余标为 FRESH；proxy 数据可替换同 request id 的 session-log 数据，并对语义相同项去重。[写入和去重](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/usage/logger.rs#L100-L220)

聚合时：

- 新 TOTAL 行减 `cache_read + cache_creation`；
- legacy TOTAL 行只减 cache read；
- FRESH 行不再扣减。

这套规则集中在 [`fresh_input_sql`](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/services/sql_helpers.rs#L1-L67)。

### 9.4 计价

对 cache-inclusive app，正常 input 价只乘 `input - cache_read - cache_creation`；output、cache read、cache creation 分别乘各自价格，四项相加后再统一乘 provider `cost_multiplier`。Anthropic input 已是 fresh，不能再减。[成本公式](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/usage/calculator.rs#L31-L113)

当前模型只有 aggregate `cache_creation_tokens` 和一个 `cache_creation_cost_per_million`，没有按 5m/1h TTL 分桶。由于 injector 会保留调用方已有的 `ttl: "1h"`，这类写入能被计数，却不能与标准 5m 写入分别套价；除非用户配置的单一写价正好符合实际流量，否则 1h cache-write 成本只能近似。CC-Switch 自己新增的 marker 固定 5m，不受此歧义影响。[usage 结构](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/usage/parser.rs#L57-L65)；[pricing schema](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/database/schema.rs#L232-L244)

### 9.5 展示

后端 summary 定义：

- `real_total = fresh_input + output + cache_creation + cache_read`
- `cache_hit_rate = cache_read / (fresh_input + cache_creation + cache_read)`

见[summary 公式](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/services/usage_stats.rs#L18-L62)；查询同时合并明细与 rollup。[聚合查询](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/services/usage_stats.rs#L682-L751) UI Hero 显示 real total、hit rate、read/write，趋势图把四桶分别绘制。[Hero](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src/components/usage/UsageHero.tsx#L182-L210)；[趋势图](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src/components/usage/UsageTrendChart.tsx#L285-L323)

### 9.6 当前源码中的前后端落差

后端已经解析并计价 OpenAI `cache_write_tokens`，但前端仍有两处旧假设：

1. `getCacheWriteAvailability` 对 `codex/gemini/grokbuild` 一律返回 N/A 或 partial；所以即使 Codex 上游真实返回 cache-write，Hero 也可能隐藏数值。[前端 availability](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src/types/usage.ts#L220-L248)
2. 单请求 `getFreshInputTokens` 对 TOTAL app 只减 `cacheReadTokens`，没有减 `cacheCreationTokens`，而后端新语义会减两者。[前端 fresh helper](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src/types/usage.ts#L250-L270) Request table/detail 直接用该 helper，detail 的“总计”又只算 `freshInput + output`。[请求详情](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src/components/usage/RequestDetailPanel.tsx#L156-L207)

因此聚合/计价后端是四桶一致的，但含 cache-write 的单请求 UI 可能高估 fresh input，并与 Hero/趋势口径不完全一致。

## 10. 故障转移、provider 切换与缓存隔离

路由器在 failover 关闭时只返回当前 provider；开启时严格按 failover queue 顺序选择，并跳过断路 provider。Codex Official 账号卡禁止跨账号 failover，避免把一个账号的 Authorization 用到另一账号。[provider 选择](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/provider_router.rs#L15-L21)；[队列逻辑](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/provider_router.rs#L40-L130)

每次 attempt 都使用 provider 自己的：

- base URL、认证与 headers；
- model mapping / upstream model；
- wire format 与转换器；
- `promptCacheKey` / `promptCacheRouting`；
- body overrides；
- Bedrock 注入资格。

同一客户端请求的 session ID 在 attempts 之间稳定，但某个 provider 可能发送 key、另一个不发送；即使 key 相同，最终模型/prefix/account 也可能不同。成功落到 fallback 后，CC-Switch 会异步把该 provider 切成当前 provider。[attempt 循环](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L400-L520)；[成功后切换](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L522-L572)

**源码事实：** CC-Switch 没有缓存迁移/共享代码，Bedrock 注入 body 也按 provider clone 隔离。**运营推论：** provider 切换应视为 cold cache；只有目标配置实际共享同一上游缓存边界且最终请求完全相容时，才可能继续命中，CC-Switch 不作保证。

## 11. “Cache”名下的其他内部状态，不是 prompt cache

| 本地状态 | 用途与边界 | 为什么不是响应/prompt cache |
|---|---|---|
| `CodexChatHistoryStore` | 最多保存 512 个 response 的 function-call/reasoning 片段，用 `previous_response_id` 或唯一 `call_id` 补回 Responses → Chat 缺失的 assistant tool call | 不保存/返回完整 completion；只是重建下一次上游请求。[实现](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/codex_chat_history.rs#L10-L45)；[上限](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/codex_chat_history.rs#L208-L240) |
| `GeminiShadowStore` | 按 `(provider_id, session_id)` 保存 assistant/tool metadata 与 thought signature，默认 200 sessions × 64 turns | 用于 Gemini 协议回放；provider scoped，不是模型结果命中层。[定义与范围](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/gemini_shadow.rs#L1-L25)；[容量](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/gemini_shadow.rs#L104-L133) |
| `UsageCache` | 托盘订阅/脚本用量 snapshot，进程内 write-through，重启即空 | 缓存的是配额展示数据，不参与请求/响应。[说明](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/services/usage_cache.rs#L1-L17) |
| `CodexReplayCaches` | 缓存 session JSONL 的 parent timeline、文件 stamp 与 replay prefix，加速用量导入/去重 | 处理本地日志，不处理 prompt 或 completion。[结构](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/services/session_usage_codex.rs#L179-L251) |
| OAuth access-token cache | 按账号缓存尚未临期的 access token | 认证快路径；不含模型内容。[Codex OAuth cache](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/codex_oauth_auth.rs#L165-L181)；[内存 map](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/codex_oauth_auth.rs#L294-L307) |
| 全局 HTTP client | 复用连接池/代理配置 | 传输层连接复用；每次仍发送完整上游请求。[client singleton](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/http_client.rs#L1-L17) |

`CodexChatHistoryStore` 当前不按 provider id 分区，而 Gemini shadow 明确按 provider id 分区。前者可在 provider 切换后继续把已知 tool-call 片段补进新上游请求，但这仍是协议连续性，不是把旧 provider 的 prompt cache 搬到新 provider。

## 12. 对 Magic AI Router 的建议

### 值得借鉴

1. **把 wire format、cache signaling、usage semantics 拆成独立类型。** 建议至少分为 `upstream_protocol`、`anthropic_cache_policy`、`openai_cache_routing`、`input_token_semantics`，不要让 `anthropic_native` 同时承担协议兼容、字段清洗、cache 开关与计费语义。
2. **只用真实、稳定、客户端提供的会话身份做自动 key。** 明确排除每请求 UUID 与 `previous_response_id`；允许显式 key 覆盖，同时给严格网关提供 `auto/on/off` 能力策略。
3. **在转换完成、override 应用后统一 canonicalize，并提供无敏感明文的 cache trace。** 至少记录最终 wire format、provider/model、key presence、system/tools/messages hash、marker 数量/TTL，方便定位“同 session 不命中”。
4. **保持 caller-owned marker/TTL，不静默删除或重排。** 自动注入必须先算预算；failover 要以原始 body 为基线，为每个 provider 独立构造。
5. **四桶 usage + 明确 input semantics。** raw input、fresh input、cache read、cache write 必须守恒；计价、聚合、趋势和单请求 UI 共用同一个语义函数，避免双算。
6. **把“是否本地缓存响应”在产品文案中说清楚。** 建议使用“上游提示词缓存路由/断点”而非笼统“缓存”，并把协议回放 store、用量 cache 放在不同命名空间。

### 不应照搬

1. **不要复用一个隐藏耦合的全局子开关。** CC-Switch 的 `cacheInjection` 同时影响 Bedrock optimizer 与 Codex → Anthropic bridge，而 UI 又在总开关关闭时禁用它。Magic 应采用 per-provider、per-path policy，并显示最终生效值。
2. **不要照搬浅层 4-breakpoint 计数。** 应递归识别所有合法 cache-control 位置，保存路径，注入后再做完整数量/TTL 验证；超过上游限制时应给用户可操作的错误或明确策略。
3. **不要声称 TTL 可配置而实现里没有。** schema、UI、运行日志和注释必须由同一枚举/配置生成；若支持 `5m/1h/preserve`，要分别测试 caller marker 与新增 marker。
4. **不要在 Rust/TypeScript 各复制一套 token 语义白名单。** CC-Switch 当前后端已扣 cache write，前端仍只扣 read。Magic 应由后端直接返回 `fresh_input_tokens`、`raw_input_tokens` 和 availability，UI 不再推导。
5. **不要只靠硬编码 hostname 判断能力。** `auto` allowlist 是安全默认，但长期应把 `supports_prompt_cache_key`、支持字段版本、失败降级策略放进 provider capability/schema。
6. **不要把稳定 key 当成命中保证。** 真正的诊断必须同时检查最终 provider/account/model、prefix hashes、tool/system 数组顺序、转换版本和 cache TTL。

## 13. Magic 当前替代边界

对“Claude Code → Anthropic-compatible `/v1/messages`”这个明确范围，Magic 已经覆盖缓存核心闭环：

- `anthropic_native=true` 跳过兼容清洗，保留 Claude Code 原有 `cache_control`、document block 与 beta tool 字段；
- `anthropic_native=false` 做确定性的 system 扁平化/document/beta 字段清洗，适合不完整兼容端点；DeepSeek 的自动前缀缓存不依赖 `cache_control`；
- Anthropic 流式和非流式响应都提取 input/output/cache-read/cache-create 四桶；
- 统计页的命中率口径与 CC-Switch 后端一致。

实现依据：[Magic compat](https://github.com/benz-ai-x/Magic-AI-Router/blob/27145fff7db5d7b3a0e45ddba54f0adcec151735/suanpan/compat.py#L42-L117)、[转发路径](https://github.com/benz-ai-x/Magic-AI-Router/blob/27145fff7db5d7b3a0e45ddba54f0adcec151735/suanpan/proxy.py#L143-L197)、[usage extractor](https://github.com/benz-ai-x/Magic-AI-Router/blob/27145fff7db5d7b3a0e45ddba54f0adcec151735/suanpan/usage_extractor.py#L1-L95)、[ADR-004](https://github.com/benz-ai-x/Magic-AI-Router/blob/27145fff7db5d7b3a0e45ddba54f0adcec151735/docs/adr/004-prompt-caching-and-prefix-stability.md#L1-L60)。

尚未覆盖的 CC-Switch 能力：

| 缺口 | 对替代的影响 |
|---|---|
| Claude→OpenAI Responses/Chat/Gemini bridge | 这类 provider 不能直接由 Magic 接管 |
| stable session→`prompt_cache_key` 与 capability gate | OpenAI-compatible 自动缓存路由不等价 |
| Bedrock / Codex→Anthropic 断点注入 | 需要代理生成 marker 的场景不等价 |
| 全 body/tool JSON canonicalization 与 CacheTrace | 能运行，但复杂转换后的命中稳定性和诊断能力较弱 |
| provider failover、circuit breaker、成功后热切换 | 故障恢复能力不等价；Magic 当前只对传输错误原 provider 重试一次 |
| 多协议 input semantics、成本数据库和四桶 UI 一致性 | Anthropic 直连够用，跨协议计价不等价 |
| DeepSeek thinking 等窄域 normalizer | 新版客户端与特定上游组合仍可能出现兼容缺口 |

所以结论不是简单的“能/不能”：**Magic 已能替代 CC-Switch 的 Claude Code 直连 Anthropic-compatible 核心场景；要覆盖 CC-Switch 的全部 Claude provider 类型与缓存优化，现在还不能直接下线 CC-Switch。**

## 14. 最终判断

CC-Switch 的核心不是“缓存 AI 回答”，而是**把客户端请求转换成目标协议能够识别的上游缓存信号，再把上游返回的 cache read/write usage 归一化、落库和计价**。Anthropic identity、OpenAI key routing、Codex bridge、Bedrock injector 四条路径各自有不同生效条件；compatibility/wire-format 开关只能决定转换方式，不能单独说明缓存行为。

对 Magic AI Router 最重要的设计原则是：**协议身份决定哪些缓存字段可以保留，provider cache policy 决定是否及如何新增信号，usage semantics 决定如何解释结果；三者应显式分层。**
