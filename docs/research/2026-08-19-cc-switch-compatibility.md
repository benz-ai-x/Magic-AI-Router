# CC-Switch 与 Magic AI Router 兼容模式对比

调研对象为 CC-Switch v3.20.0，固定在 commit [`0b5da510168914b251481654a568c3ffacd62cf4`](https://github.com/farion1231/cc-switch/tree/0b5da510168914b251481654a568c3ffacd62cf4)。以下只引用官方仓库中的手册、release notes、源码与测试。

## 结论

1. **CC-Switch 既能只写 Claude Code 配置，也能成为请求代理。**普通供应商切换会更新 `~/.claude/settings.json` 中的 API Key 与 `ANTHROPIC_BASE_URL`，此时 CC-Switch 不在请求路径中；开启 Local Routing/takeover 后，它会启动默认监听 `127.0.0.1:15721` 的本地 HTTP 代理并接管 Base URL，格式转换、故障转移和用量记录才在代理路径发生。[普通切换手册](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/docs/user-manual/en/2-providers/2.2-switch.md#L53-L91) [代理职责与监听地址](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/docs/user-manual/en/4-proxy/4.1-service.md#L3-L40) [接管流程](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/docs/user-manual/en/4-proxy/4.1-service.md#L112-L165)

2. **v3.20.0 的 Claude “Upstream Format” 是上游协议枚举，不等价于 Magic 的 `anthropic_native`。**每个 provider 可选 `anthropic`、`openai_chat`、`openai_responses` 或 `gemini_native`；后三者需要 routing takeover。v3.20.0 release notes 明确记录了 UI 从 “API Format” 改名为 “Upstream Format”，并为各选项标注是否需要接管。Magic 的开关是在仍使用 Anthropic Messages 的前提下选择保留原生 body 或做字段降级；CC-Switch 则选择真实上游 wire protocol，没有“Anthropic 原生 body / Anthropic 降级 body”的等价二态开关。[v3.20.0 release notes](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/docs/release-notes/v3.20.0-en.md#L124-L126) [Claude 表单选项](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src/components/providers/forms/ClaudeFormFields.tsx#L810-L850) [格式解析](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/claude.rs#L37-L104)

3. **`apiFormat=anthropic` 不做 Magic 兼容模式那种通用 body 清洗；跨协议 transformer 才会改变这些字段。**Anthropic 分支在格式层返回原 body，因此不会统一拍平 `system`，也不会统一删除 `cache_control`、`document`、`defer_loading` 或 `eager_input_streaming`；不过代理仍会递归删除以下划线开头的私有字段，并对 DeepSeek/MiMo thinking history 做定向修补，所以不是字节级透明。[转换调度与 identity 分支](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/claude.rs#L345-L462) [出站通用过滤](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L1613-L1624) [供应商定向修补](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/claude.rs#L113-L247)

   跨协议时行为由目标协议决定：OpenAI Chat 会合并 system、丢弃 `cache_control`，普通 message 的 `document` 因没有匹配分支而被跳过，function tool 只重建 `name`、`description`、`parameters`；OpenAI Responses 会把 system 变为 `instructions`、把受支持的 document 变为 `input_file`；Gemini 会变为 `systemInstruction`、`inlineData` 与 `functionDeclarations`。这些是完整协议转换的结果，不能视为 Magic `anthropic_native=false` 的等价实现。[Chat system 转换](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform.rs#L151-L184) [Chat cache 测试](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform.rs#L1475-L1578) [Chat tool 重建](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform.rs#L215-L235) [Chat message 分支](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform.rs#L381-L466) [Responses system/tool](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform_responses.rs#L1783-L1904) [Responses document](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform_responses.rs#L2305-L2340) [Gemini system/tool](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform_gemini.rs#L72-L114) [Gemini document](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/transform_gemini.rs#L631-L653)

4. **几个看似相似的设置，本质都不同。**旧 `openrouter_compat_mode=true` 只是当前 `openai_chat` 的 legacy fallback，写入 Claude Code live settings 前还会被移除；Local Routing 只决定代理是否在数据路径；Full URL/auth 只决定 URL 和认证头；`CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` 是让客户端少发送实验字段，而不是代理按 provider 清洗 Messages body。[legacy 映射](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/claude.rs#L66-L96) [live settings 清理](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/services/provider/live.rs#L168-L177) [Full URL 与认证元数据](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/provider.rs#L481-L503) [实验 beta 预设示例](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src/config/claudeProviderPresets.ts#L587-L606)

5. **Bedrock Cache Injection 与 Codex `promptCacheRouting` 也不能和该问题混淆。**Bedrock Optimizer 的 Cache Injection 默认关闭、只对 Bedrock 生效，作用是将字符串 system 变成 block 数组并向 tools/system/messages 注入最多四个 5 分钟 `cache_control` 断点，方向与 Magic 兼容模式的删除行为相反；`promptCacheRouting` 则是 Codex Responses → Chat 路径对 OpenAI `prompt_cache_key` 的三态发送策略，不控制 Anthropic `cache_control`。[Bedrock 配置与默认值](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/types.rs#L243-L268) [Bedrock 注入实现](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/cache_injector.rs#L1-L70) [Codex cache routing](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/codex.rs#L87-L159)

## 源码控制链：CC-Switch 如何取得“Anthropic native”效果

### 1. 不存在 `anthropic_native` 字段

在固定 commit 上对整个官方仓库精确搜索 `anthropic_native` 与 `anthropicNative`，结果均为 0。前端 `ProviderMeta` 声明的是 `apiFormat` 四值枚举，后端以 Serde 将 JSON 字段 `apiFormat` 映射到 Rust 成员 `api_format`；源码中没有同名布尔开关。[前端类型](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src/types.ts#L196-L205) [后端 ProviderMeta](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/provider.rs#L481-L486) [固定 commit 源码树](https://github.com/farion1231/cc-switch/tree/0b5da510168914b251481654a568c3ffacd62cf4)

实际控制链是：

```text
UI Upstream Format = "anthropic"
  → localApiFormat
  → payload.meta.apiFormat
  → ProviderMeta.api_format（Serde rename）
  → SQLite providers.meta
  → get_claude_api_format(provider)
  → normalize_anthropic_messages_for_provider(...)
  → needs_transform = false
  → 请求 identity 分支 + 响应通用透传路径
```

### 2. UI 与持久化

Claude 表单的 Upstream Format 选择器把 Anthropic 原生选项的值设为字面量 `"anthropic"`；编辑已有 provider 时从 `initialData.meta.apiFormat` 初始化，缺省同样是 `"anthropic"`。提交时，普通 Claude provider 把本地选择写入 `payload.meta.apiFormat`；XAI OAuth 是例外，表单直接强制保存为 `openai_responses`。[选择器](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src/components/providers/forms/ClaudeFormFields.tsx#L810-L850) [表单状态](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src/components/providers/forms/ProviderForm.tsx#L541-L548) [提交组装](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src/components/providers/forms/ProviderForm.tsx#L1714-L1795)

后端把整个 `ProviderMeta` 序列化到 SQLite `providers.meta` 列，所以 `apiFormat` 是 CC-Switch 自己的持久化元数据。它不是 Claude Code 设置：live 配置清洗器会移除历史上可能混入 `settings_config` 的 `api_format`/`apiFormat`，再写 `~/.claude/settings.json`。[数据库持久化](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/database/dao/providers.rs#L180-L264) [live 字段清理](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/services/provider/live.rs#L168-L177) [Claude live 写入](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/services/provider/live.rs#L1241-L1248)

### 3. 代理接管是源码执行这些分支的前提

不开 Local Routing/takeover 时，请求由 Claude Code 直达 provider；DB 中即使保存了 `apiFormat`，CC-Switch 的 forwarder 也不会运行。开启 Claude takeover 时，服务先启动代理、备份 live 配置并设置 app 的 `proxy_config.enabled`，随后把 Claude live env 的 `ANTHROPIC_BASE_URL` 改成本地代理 URL。[单应用接管流程](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/services/proxy.rs#L1140-L1278) [Claude Base URL 改写](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/services/proxy.rs#L486-L508) [Claude live 接管](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/services/proxy.rs#L2057-L2075)

因此 `apiFormat=anthropic` 有两种运行形态：不接管时是 Claude Code 直接连接上游，CC-Switch 完全不处理 body；接管时才是 CC-Switch 代理中的 Anthropic identity 路径。

### 4. `apiFormat` 解析与 identity 分支

解析优先级为：托管的 Codex/XAI OAuth 强制 `openai_responses` → `meta.apiFormat` → 旧 `settings_config.api_format` → 旧 `openrouter_compat_mode` → 默认 `anthropic`。对 `meta.apiFormat` 来说，只有三个非 Anthropic 已知值有专门映射，`"anthropic"`、未知值和缺省都落到 `anthropic`。Copilot 又在 forwarder 中按模型动态选择 `openai_responses` 或 `openai_chat`，不走 Anthropic identity。[完整解析器](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/claude.rs#L37-L104) [Copilot 动态解析](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L2583-L2604)

forwarder 解析格式后，以 `claude_api_format_needs_transform` 判定是否转换；只有 Chat、Responses、Gemini 返回 true。请求转换函数的兜底分支直接 `Ok(body)`，所以 `anthropic` 不进入跨协议 transformer。[forwarder 判定与端点选择](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L1364-L1407) [请求 identity 分支](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/claude.rs#L345-L462)

### 5. Anthropic identity 前后仍有独立清理

“identity”只表示不做协议转换，并不保证字节级透传。其请求顺序是：

1. 先调用只接受 `api_format="anthropic"` 的供应商 normalizer。它仅为 DeepSeek/MiMo 类标识补齐带 `tool_use` 历史中的 thinking，并对 DeepSeek 官方 Anthropic endpoint 的 `thinking=disabled` 删除冲突的 `output_config.effort`/`reasoning_effort`；它不是通用的 system、document、cache 或 beta tool 清洗器。[Anthropic normalizer](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/providers/claude.rs#L113-L247)
2. 若全局 Rectifier 的 media fallback 已开启，代理可把 text-only 模型请求中的 image block 替换为标记；这是独立优化器，不由 `apiFormat` 控制。[media prevention](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L174-L199)
3. identity 分支保留 body 结构后，所有代理请求都会进入 `prepare_upstream_request_body`：递归删除以下划线开头的私有字段（JSON Schema 字段名有例外），并递归按 key 排序；显式配置的 per-provider local proxy body override 也可能修改 body，修改后再执行同样过滤。[forwarder 出站定稿](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L1547-L1625) [私有字段递归规则](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/body_filter.rs#L1-L10) [过滤与 canonicalize 调用](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/forwarder.rs#L3570-L3572)

所以，原生路径默认会保留 `system` 数组、`cache_control`、`document` 和 Anthropic beta tool 字段；只有上面这些相互独立、条件明确的处理可能改变请求。不存在一个隐藏的 `anthropic_native=false` 分支去统一删除它们。

### 6. 响应回程

`/v1/messages` handler 从 forwarder 取回 provider 和已解析格式。若 adapter 需要跨协议转换，就调用 `handle_claude_transform`：流式响应分别用 Responses/Gemini/Chat → Anthropic SSE 转换器，非流式响应也按 `api_format` 重建 Anthropic JSON。若 `apiFormat=anthropic` 且没有上述托管 provider 强制规则，`needs_transform=false`，直接进入通用 `process_response`。[响应分流](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/handlers.rs#L226-L267) [跨协议流式转换](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/handlers.rs#L384-L451) [跨协议非流式转换](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/handlers.rs#L539-L703)

通用响应路径不会做 JSON schema 转换：SSE 以原字节流返回，同时旁路解析 usage/执行超时控制；非流响应可能按 `Content-Encoding` 解压和清理相应 headers，但最终返回读取到的 body bytes。因此 Anthropic identity 的响应是语义透传，不应表述成绝对的网络字节级透明。[通用 SSE 路径](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/response_processor.rs#L144-L210) [通用非流路径](https://github.com/farion1231/cc-switch/blob/0b5da510168914b251481654a568c3ffacd62cf4/src-tauri/src/proxy/response_processor.rs#L212-L321)

最短答案：**CC-Switch 有按供应商选择上游协议的 transformer，但没有 Magic AI Router 这种 Anthropic Messages 协议内部的“原生/兼容”开关。**
