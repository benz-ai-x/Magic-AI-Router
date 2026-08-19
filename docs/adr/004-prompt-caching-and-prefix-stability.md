# ADR-004: Prompt caching 架构与网关前缀稳定性契约

- 状态：Accepted
- 日期：2026-08-16
- 决策者：tech-lead（用户确认）
- 原编号：ADR-025（2026-08-19 仓库重建后重编号压缩间隙）
- 影响范围：suanpan/compat.py、suanpan/config.py、suanpan/proxy.py、suanpan/usage_extractor.py
- 关联：#48（anthropic_native）、#49（非流式记账）；调研依据 deepseek-harness（DeepSeek 官方 agent harness，`packages/llm/llm-deepseek/`）

## 上下文

供应商分两个缓存家族，行为完全不同：

| | Anthropic 显式缓存（GLM/KIMI 的 Anthropic 兼容端点） | DeepSeek 自动前缀缓存 |
|---|---|---|
| 触发方式 | 请求携带 `cache_control` 标记（位于 system 等 content block 上） | 服务端自动，无需任何标记 |
| 写指标 | `cache_creation_input_tokens` | **无**（DeepSeek 不报告缓存写） |
| 读指标 | `cache_read_input_tokens` | `cache_read_input_tokens`（Anthropic 端点）/ `prompt_cache_hit_tokens`（chat-completions 端点，折入 `prompt_tokens`） |
| TTL | 5 分钟 / 1 小时 | 服务端管理，无客户端旋钮 |

历史问题：`compat.normalize_body` 对所有供应商无差别剥离（system 扁平化 + document 块剥离 + beta 字段剥离），`cache_control` 随 system 数组一起被毁——Anthropic 家族的 prompt caching 在网关后面不可能生效。同时 usage 记账只扫 SSE `data:` 行，非流式 JSON 响应（Claude Code 自 8/15 起对 sonnet 档发送 `stream:false`）记账全零。

DeepSeek 官方 harness 的调研结论（同仓库旁证）：其 DeepSeek 适配器**零 `cache_control` 代码**，缓存复用完全靠「请求前缀逐字节稳定」的架构纪律 + 读指标落账；换 provider/model 即换缓存域，全冷。

## 决策

### 决策 1：`anthropic_native` 供应商开关（#48）

per-provider 布尔字段（`ProviderConfig.anthropic_native`，默认 false）。置 true 时 `normalize_body` 跳过全部剥离——`cache_control`、document 块、beta 工具字段原样过闸。适用于原生接受 Anthropic 请求形状的端点（GLM / KIMI）。**DeepSeek 不需要**：自动前缀缓存与标记无关。

### 决策 2：usage 记账覆盖两种传输形状（#49）

`UsageExtractor(json_mode=True)`：非流式响应是单个 JSON 文档，usage 在顶层；`forward_request` 按上游 `content-type` 选模式（含 `json` → json_mode；`text/event-stream` 或缺失 → SSE 扫描）。四桶不相交约定：`input_tokens` / `cache_read_tokens` / `cache_creation_tokens` 各自独立，计费输入 = 三者之和（与 dsh `TokenUsage` 同约定；Anthropic 格式下 `input_tokens` 本就不含缓存读，无需扣减）。

### 决策 3：前缀稳定性契约（本文档核心新增）

上游前缀缓存以「从首个变更 token 起失效」的语义工作。因此：

1. **`normalize_body` 必须是其输入的确定性函数**——同一请求体在任何时刻过闸，产出的内容形状相同。当前实现满足（纯函数、无时间/随机依赖）。修改归一化规则（增删剥离字段、改拼接行为）前必须意识到：**上线即一次性打冷所有经网关会话的上游缓存**。这是可接受的一次性成本，但要作为变更说明写进 PR；禁止用运行时可变的配置（如每请求读取的开关）切换归一化形状——抖动等于每请求全冷。
2. **`anthropic_native` 开关属于部署级配置**：一旦启用不应反复切换（切换即形状变更，同上）。
3. **网关不改写语义内容**：只做剥离/扁平化这类确定性变换，不重排、不注入请求体内容。JSON 键序/空白不影响供应商 tokenization（前缀按解析后内容计算），无需字节级一致。

### 决策 4：可观测性边界

usage.jsonl 四桶已落（流式 + 非流式）；缓存命中率的滚动聚合与 UI 列为 backlog（交接文档 backlog #2/3），不在本 ADR 范围。

> 后续状态（2026-08-19）：Issue #1 已在只读聚合层与独立「运行统计」页兑现该 backlog；usage 写路径与本 ADR 的前缀稳定性契约均未改动。

## 否决

- **给 DeepSeek 注入 `cache_control`**：无意义（服务端自动缓存，标记被忽略），且其 Anthropic 端点对标记的接受行为未承诺——不实现。
- **全局放行 Anthropic 形状**（不设 per-provider 开关）：document 块对 KIMI chat 端点是硬错误（MoonshotAI/Kimi-K2#129），必须按供应商选择。
- **网关侧自建请求去重/缓存**：上游自动缓存已覆盖主流场景，网关加缓存层只会引入新的一致性风险。
- **把归一化规则做成可热更的运行时配置**：见决策 3.1，形状抖动 = 每请求全冷。

## 测试

- `tests/test_suanpan_compat.py::TestAnthropicNative`——开关保留 `cache_control` / document / beta 字段。
- `tests/test_suanpan_usage_extractor.py::TestNonStreamJsonBody`——json_mode 跨 chunk 解析、错误 JSON 记零、SSE 模式不受影响。
- `tests/test_coverage_push.py::test_nonstream_json_response_usage_logged` / `test_stream_response_still_uses_sse_mode`——content-type 接线双向回归。
- `tests/test_docs_drift.py::TestPromptCachingAdrDocumented`——本 ADR 术语（`anthropic_native` / `json_mode` / 缓存桶字段名 / 确定性规则）与代码双向锁死。
