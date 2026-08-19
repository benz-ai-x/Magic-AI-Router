# 运行统计：缓存命中率与多维用量分析（独立统计页）

> 当前生效的跟踪单：benz-ai-x/Magic-AI-Router#1。历史：初版发布于已删除的旧库（2026-08-19），于本库重新发布，内容未变。状态：ready-for-agent。

## Problem Statement

我把 Claude Code 接到 Suanpan 网关后面跑第三方供应商（GLM / KIMI / DeepSeek）。**Prompt caching 是这套架构省钱省延迟的核心机制**——ADR-004 的前缀稳定性契约、`anthropic_native` 开关，整个架构纪律都是为了让上游缓存能命中。但我现在完全看不见缓存有没有命中：

- 「运行状态」页只有一张全时段供应商表（调用 / Input / Output / 错误），没有缓存读/写、没有命中率、没有时间维度
- 我无法回答：「缓存命中率是多少？」「今天用了多少 token？」「请求是走 rule 命中还是 default 兜底？」「开了 anthropic_native 之后命中率有没有真的上来？」
- 全时段聚合让「昨天改了配置有没有效果」这类问题无法验证

数据其实早已完整落账——`usage.jsonl` 每条记录含四桶（input / cache_read / cache_creation / output）、时间戳、scenario、latency、status。**缺的只是聚合与展示**。ADR-004 决策 4 明确把「缓存命中率的滚动聚合与 UI」留作 backlog，本 issue 兑现它。

## Solution

侧栏新增独立「**运行统计**」视图（AI 路由分组内），原「运行状态」页改名「**余额速览**」并只保留余额/配额卡（外部 API 慢查询不再拖慢本地统计的打开速度）。

「运行统计」页包含：

1. **总览卡**：总调用、错误数、输入 token、输出 token、**缓存命中率**、平均延迟
2. **时间范围切换**：今日 / 近 7 天 / 全部（日历日按 CST 对齐，日志 ts 本即 +08:00）
3. **每日趋势表**（ccusage daily 式）：日期、调用、输入、输出、缓存读、缓存写、命中率
4. **供应商分组表**：现有调用/Input/Output/错误 + 新增缓存读、缓存写、命中率列
5. **路由来源分组表**：inline / subagent / rule / default 四类 scenario 的调用数与命中率——全网的独家数据（cc-switch / ccusage 都拿不到路由路径）

**缓存命中率口径**：`cache_read / (input + cache_read + cache_creation)`。四桶不相交（ADR-004 决策 2），分母即计费输入总量；DeepSeek 无写指标（creation=0）自动兼容；分母为 0 显示 `—` 而非 0%。

## User Stories

1. 作为网关用户，我想在总览卡看到缓存命中率，以便确认 prompt caching 真的在生效
2. 作为网关用户，我想看到缓存读 / 缓存写 token 的绝对值分列展示（ccusage 模式），以便理解缓存复用规模——缓存读常常占总输入 90% 以上，混在 Input 里就看不见
3. 作为刚开启 anthropic_native 的用户，我想按供应商对比命中率，以便确认开关对哪个供应商起了作用
4. 作为网关用户，当某 Anthropic 家族供应商命中率长期为 0 时，我想看到一条轻提示（「检查该供应商是否开启 Anthropic 原生模式」），以便发现配置遗漏
5. 作为网关用户，我想切换今日 / 近 7 天 / 全部，以便回答「今天 vs 累计」两类问题
6. 作为刚改了路由配置的用户，我想用时间段对比改前改后的命中率和用量分布，以便验证改动效果
7. 作为网关用户，我想看每日趋势表，以便发现用量突增或命中率骤降（骤降可能意味着归一化形状变化打冷了缓存——ADR-004 决策 3 的预警）
8. 作为网关用户，我想按路由来源（inline / subagent / rule / default）看调用分布，以便理解规则命中率和兜底流量占比
9. 作为网关用户，我想在路由来源分组里看到各 scenario 的缓存命中率，以便判断哪类流量的缓存效率低
10. 作为网关用户，我想看到错误数与平均延迟随时间范围联动变化，以便按时间段评估供应商稳定性
11. 作为网关用户，我想统计页 60 秒自动刷新（沿用运行状态页模式），以便观察进行中的会话
12. 作为网关用户，我想大数字沿用 K/M 格式化、数字列右对齐等宽字体（#53 刚定的视觉语言），以便跨行比大小
13. 作为网关用户，当日志为空时我想看到引导性空态文案（先向网关发一条请求），而不是空白页
14. 作为网关用户，我想统计完全基于本地日志、不发起任何外部请求，以便打开即得、不泄露隐私
15. 作为余额查看者，我想余额/配额卡仍在原处（改名余额速览），以便沿用既有肌肉记忆
16. 作为维护者，我想写路径（UsageLogger / UsageExtractor / proxy 记账）零改动，以便此功能不引入任何请求链路风险
17. 作为维护者，我想聚合逻辑是纯函数（config dict 进、统计出，文件路径来自配置），以便单元测试不起服务

## Implementation Decisions

- **唯一新逻辑在聚合层**：扩展 `balance_usage` 的 usage 聚合（现有 `fetch_usage` 纯函数接缝，吃 raw config dict）。**写路径零改动**——四桶、ts、scenario、latency、status 全部已在账上（ADR-004 决策 2/4）
- **API**：`GET /api/usage` 增加 `range` 查询参数（`today` | `7d` | `all`，默认 `all`），config_server 对取值做白名单校验。响应在现有 `{total, providers}` 形状上扩展，旧键保持兼容：
  - `total` / `providers[*]` 各增 `cache_read_tokens`、`cache_creation_tokens`、`cache_hit_rate`（0–1 浮点或 null）、`errors` 保持
  - 新增 `daily`（按 CST 日历日分桶，含各桶与命中率）与 `scenarios`（按 scenario 分组）两个数组/映射
- **分桶时区**：按日志 ts 的 +08:00 对齐日历日；「近 7 天」= 含今天共 7 个日历日
- **聚合范围**：只读当前 usage.jsonl；50MB 轮转产生的 .1 旧文件不纳入（与现状一致，spec 内注明即可）
- **UI 架构**：新视图注册进 VIEWS 注册表（与现有六视图同构）；范围切换、命中率计算、每日分桶的视图模型转换放 LAYER 1 纯逻辑（node 可测）；渲染复用 stat-card / rules-table / mono 右对齐 / fmtNum 现有组件与视觉语言
- **页面拆分**：「运行状态」视图移除用量部分、改名「余额速览」；侧栏 AI 路由分组变为「供应商 / Claude Code 同步 / 运行统计 / 余额速览」。注意 CLAUDE.md 侧栏描述与 `tests/test_docs_drift.py` 守卫需同步更新（#53-4 刚修过漂移）
- **空态**：无日志时沿用现有引导文案模式
- **缓存为 0 提示**：当某 anthropic_native=false 的供应商在选定范围内命中率恰为 0 且有流量时，该行显示轻提示（条件渲染，不做告警系统）

## Testing Decisions

好测试 = 只测外部行为，不测实现细节。三个现有接缝各补一组：

- **聚合纯函数**（`tests/test_balance_usage.py` 现有模式：临时目录写 JSONL + config dict 直调）：四桶聚合正确、命中率口径（含分母为 0 → null）、DeepSeek 无写指标场景、today/7d 边界（跨午夜、恰好第 7 天）、空文件、坏行跳过、轮转 .1 文件不纳入
- **HTTP 端点**（`tests/test_config_server.py::TestBalanceUsageEndpoints` 模式）：range 合法值三种、非法值 400、缺省 = all、响应含 daily/scenarios 键
- **JS LAYER 1**（`tests/js/model.test.mjs` + extract.mjs 模式）：命中率格式化（null→`—`、0.42→`42%`）、范围切换的视图模型、scenario 中文标签映射

不做 UI 截图/E2E 测试（仓库无此惯例）；视觉验收沿用 Chrome MCP 走查。

## Out of Scope

- **费用估算**（无定价数据源，不引入价格表维护负担）
- **source_model → target_model 模型映射表**（数据已在账上，留 backlog）
- 逐请求明细日志查看器
- 用量限额、阈值告警系统
- 实时推送（60s 轮询足够）
- 与抓包模式（ai_capture_addon 的 JSONL）数据整合
- `UsageLogger.rolling` 内存总计的治理（当前无消费者，不动）

## Further Notes

**同类产品调研**：

- [ccusage](https://ccusage.com/guide/claude/)（[GitHub](https://gitee.com/mirrors_trending/ccusage?skip_mobile=true)）：时间分桶报表（daily/weekly/monthly/5-hour blocks）、cache creation 与 cache read **独立列**展示（[缓存读可占 95%+](https://github.com/anthropics/claude-code/issues/44494)，单列才能看见）、per-model breakdown、since/until 过滤
- [cc-switch](https://ccswitch.co/zh/)（[GitHub](https://github.com/farion1231/cc-switch)）：按供应商和模型的 Token 仪表盘、每日/每周花费趋势、请求日志、限额——前提也是走它的本地代理（与我们的网关位置相同）
- [tally](https://github.com/a77ming/tally)：macOS 菜单栏实时用量表，与我们形态最接近

**我们的差异化数据**：`scenario`（路由来源）与延迟是代理位独有的，ccusage 类事后分析工具拿不到。

**数据来源**：`~/.suanpan/logs/usage.jsonl`（默认路径，配置可改），当前约 1.1 万条 / 全量扫描 < 100ms，无需索引或预聚合。