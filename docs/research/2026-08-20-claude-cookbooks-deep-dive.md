# Anthropic `claude-cookbooks` 深入研究

核验日期：2026-08-20（Asia/Shanghai）  
默认分支核验点：[`35f2eec7`](https://github.com/anthropics/claude-cookbooks/commit/35f2eec7e44897c537e44441b7dff2f0ecbfb804)，2026-08-14

## 结论先行

[`anthropics/claude-cookbooks`](https://github.com/anthropics/claude-cookbooks) 是 Anthropic 官方维护的“可复制模式库”：以 Jupyter Notebook、样例数据和少量完整应用展示如何把 Claude API、Claude Agent SDK 和 Claude Managed Agents 组合成实际系统。它不是一门严格按顺序推进的课程，也不是一个统一、稳定、可直接作为生产依赖安装的框架。

它最值得学习的不是某个提示词，而是这些可迁移的工程思想：

- 用固定评测集同时测质量、延迟和成本，再选择模型与架构；
- 用结构化输出、静态校验和确定性代码约束模型的不确定性；
- 把 Agent 的工具权限、预算、人类审批、版本和运行环境作为一等资源；
- 区分模型调用、Agent harness 与托管运行时，按任务复杂度选最小足够抽象；
- 为长任务设计事件流、恢复、幂等、验证和清理，而不只关注最终文本。

截至核验点，仓库有 670 个受 Git 跟踪文件、98 个 Notebook；[`registry.yaml`](https://github.com/anthropics/claude-cookbooks/blob/main/registry.yaml) 登记 94 条 recipe，其中 31 条的发布日期为 2026 年。默认分支 2026 年有 155 个提交，说明主线仍在快速扩展。

## 它是什么，不是什么

根 [`README`](https://github.com/anthropics/claude-cookbooks/blob/main/README.md) 将项目定义为可以复制进自己项目的代码和指南，主要使用 Python，但模式可迁移到其他语言。实际仓库已明显超出根 README 的旧目录：除了基础 Messages API 示例，还有完整的 Agent SDK 教程、Managed Agents 教程、容器/Kubernetes 部署、Skills、成本优化和内容策略执行。

`pyproject.toml` 的可构建包只是空的 `anthropic_cookbook` 占位包，主要目的是让 `uv` 管理统一开发环境。因此：

- 可以复制某个 recipe 的代码和设计；
- 不应把 `anthropic-cookbook` 当作提供稳定公共 API 的 Python 库；
- 不应假定不同年份、不同目录的示例使用同一代模型、SDK 或 Beta 接口；
- 第三方集成代码尤其应回到对应厂商文档重新核对。

仓库采用 [MIT License](https://github.com/anthropics/claude-cookbooks/blob/main/LICENSE)，可以修改和商用，但按原样提供、没有适用性保证。

## 内容地图

| 目录 | Notebook 数 | 核心用途 | 阅读建议 |
|---|---:|---|---|
| `managed_agents/` | 19 | 托管 Agent、环境、会话、事件流、预算、记忆、多人协作、HITL、Vault、地域与自托管 sandbox | 当前最前沿，也最依赖 Beta API；先读入口教程再选专题 |
| `tool_use/` | 14 | 手写工具循环、并行工具、Pydantic、工具搜索、程序化工具调用、记忆与上下文压缩 | 学 Messages API Agent loop 的主目录 |
| `misc/` | 14 | Prompt caching、Batch、评测、JSON、引用、PDF、会话压缩等单点能力 | 按需求选读，注意旧例子 |
| `third_party/` | 13 | Pinecone、MongoDB、LlamaIndex、ElevenLabs、Deepgram、Wikipedia、VoyageAI 等 | 漂移风险最高；只提取架构模式 |
| `claude_agent_sdk/` | 9 | 从 `query()` 到状态化客户端、MCP、hooks、SRE、安全、部署和动态工作流 | 仓库中最接近线性课程的一组 |
| `capabilities/` | 7 | 分类、RAG、Contextual Retrieval、摘要、Text-to-SQL、知识图谱、内容策略 | 适合学习“数据集—实现—评测”闭环 |
| `multimodal/` | 6 | 图片、OCR、图表、裁剪/放大和多 Agent 视觉分析 | 关注输入预处理和工具反馈循环 |
| `patterns/agents/` | 4 | Prompt chain、route、parallel、orchestrator-workers、evaluator-optimizer | 最适合先理解抽象模式 |
| `skills/` | 3 | 内置/自定义 Skills、文档生成和渐进加载 | 使用 Beta headers，先核对现行文档 |
| 其他目录 | 9 | Extended thinking、成本优化、评测、观测、Bedrock 微调、前端提示等 | 专题选读 |

`registry.yaml` 是比根 README 更可靠的索引：每条记录包含标题、路径、作者、发布日期和分类。它目前没有登记 4 个实际存在的 Notebook：

- `managed_agents/CMA_explore_unfamiliar_codebase.ipynb`
- `managed_agents/CMA_gate_human_in_the_loop.ipynb`
- `managed_agents/CMA_orchestrate_issue_to_pr.ipynb`
- `tool_use/tool_search_alternate_approaches.ipynb`

这说明 registry 是发布目录，不应被当作完整文件清单。

## 三套开发抽象必须分开理解

| 抽象 | 典型入口 | 谁维护循环和状态 | 工具在哪里执行 | 适合 |
|---|---|---|---|---|
| Messages API | `client.messages.create(...)` | 你的应用 | 你的应用或你接入的服务 | 同步请求、确定性流程、细粒度控制 |
| Claude Agent SDK | `query(...)`、`ClaudeSDKClient(...)` | SDK harness，但进程由你运行 | 你控制的本地/容器环境 | 文件、Shell、MCP、hooks、长一些的自主任务 |
| Claude Managed Agents | `client.beta.agents/environments/sessions...` | Anthropic 托管的有状态运行时 | 云 sandbox 或你的 self-hosted worker | 异步长任务、持久事件、版本化 Agent、托管会话 |

官方文档把 [Messages API](https://platform.claude.com/docs/en/build-with-claude/working-with-messages) 定义为直接模型访问、由调用方管理完整对话和工具循环；[Managed Agents quickstart](https://platform.claude.com/docs/en/managed-agents/quickstart) 则把 Agent、Environment、Session 和 Events 分成持久资源。官方的 [Agent SDK → Managed Agents 迁移表](https://platform.claude.com/docs/en/managed-agents/migration) 进一步说明：Agent SDK 在调用方进程中执行，Managed Agents 把配置、工具和会话映射到服务端资源。

### Messages API 的典型模式

基础 recipe 会：

1. 构造 `system`、`messages`、`tools`；
2. 调用 `messages.create`；
3. 检查 `stop_reason == "tool_use"`；
4. 校验工具输入并在本地执行；
5. 把 `tool_result` 追加到历史，再调用模型。

优点是控制力强、行为容易测试；代价是状态、重试、超时、权限、幂等和上下文增长都由应用承担。

### Agent SDK 的典型模式

[`claude_agent_sdk/README.md`](https://github.com/anthropics/claude-cookbooks/blob/main/claude_agent_sdk/README.md) 是一组 00–08 的递进教程：

- 00：用 `query()` 和 `WebSearch` 做最小研究 Agent；
- 01：用 CLAUDE.md、hooks、subagents 和脚本构建 Chief of Staff；
- 02–03：通过 MCP 接 GitHub、Prometheus 和运维工具；
- 04：把 OpenAI Agents SDK 的 tool、guardrail、session、handoff 映射过来；
- 05：列出、读取、重命名、标记和 fork 本地会话；
- 06：威胁建模、漏洞发现、分流和结构化报告；
- 07：同一镜像依次部署到 Docker、Modal、Kubernetes；
- 08：让 Agent 生成确定性工作流脚本，再并行运行验证者和质疑者。

这一组最重要的边界是：SDK 给了 Claude Code 式 harness，但基础设施仍是你的。仓库的 [`hosting/server.py`](https://github.com/anthropics/claude-cookbooks/blob/main/claude_agent_sdk/hosting/server.py) 明确提醒示例服务默认没有完整认证，必须放在网关后；它还刻意移除 hosted Agent 的 `Read` 工具，避免提示注入读取其他会话或环境变量。这比“能跑起来”更接近正确的生产思维。

### Managed Agents 的典型模式

[`managed_agents/README.md`](https://github.com/anthropics/claude-cookbooks/blob/main/managed_agents/README.md) 推荐从 `CMA_iterate_fix_failing_tests.ipynb` 开始。基本生命周期是：

1. 创建版本化 Agent，声明模型、system、工具和权限；
2. 创建 Environment，限定 sandbox 和网络；
3. 上传/挂载资源；
4. 创建 Session；
5. 发送 `user.message`，消费事件流；
6. 处理 `end_turn`、`requires_action`、预算耗尽或终止状态；
7. 验证输出；
8. archive/delete 会话、环境和 Agent。

后续 recipe 增加 prompt 版本回滚、HITL、memory store、MCP credential vault、session budget、inference geo、multiagent、advisor 和 outcome grader。接口仍通过 `client.beta` 暴露；官方文档也标明 Managed Agents 使用 Beta header，因此复制前必须核对当前 SDK 和 API 文档。

## 最值得复用的五个工程模式

### 1. 评测驱动的成本优化

[`cost_optimization/cost_optimization.ipynb`](https://github.com/anthropics/claude-cookbooks/blob/main/cost_optimization/cost_optimization.ipynb) 不是简单建议“换便宜模型”，而是固定任务集和裁判，记录 pass rate、每任务成本与回合数，再比较 caching、输入裁剪、deferred tools、code execution、compaction、subagent、Batch API、模型和 effort，最后画 Pareto frontier。

核心原则：没有质量基线的降本不是优化。对 Router 来说，路由规则必须以业务评测结果为依据，而不是只按模型价格或上下文长度判断。

### 2. LLM 负责理解，确定性代码负责裁决

最新的 [`content_moderation`](https://github.com/anthropics/claude-cookbooks/tree/main/capabilities/content_moderation) 把自然语言政策编译为受 JSON Schema 限制的规则；静态校验器发现字段、类型和运算符错误后让模型修复。运行时 Claude 只提取类型化字段，最终 block/flag/review/approve 由确定性三值逻辑引擎决定。

这是比“直接问模型是否违规”更强的生产模式：模型处理模糊语义，程序掌握最终政策、审计轨迹和 fail-safe 行为。

### 3. 上下文是资源，不是无限聊天记录

[`context_engineering_tools.ipynb`](https://github.com/anthropics/claude-cookbooks/blob/main/tool_use/context_engineering/context_engineering_tools.ipynb) 分别测 memory、compaction 和 tool-result clearing：

- memory 保存跨阶段仍需保留的高价值事实；
- compaction 压缩旧对话；
- tool clearing 删除已经消费、体积很大的中间结果。

三者解决的问题不同，可以组合；都可能损失信息，所以应通过任务评测决定阈值和策略。

### 4. 自主性必须配预算、权限和人类闸门

Managed Agents 示例把 `permission_policy`、sandbox networking、session budget、human approval、vault 和 data residency 放到 API 资源层。教学入口里出现 `always_allow` 是为了缩短示例，不应原样搬到生产。生产系统至少要：

- 工具最小权限与参数校验；
- 用户与 session/resource 的所有权绑定；
- 外网 allowlist 和凭证隔离；
- 花费/回合/时间硬上限；
- 对不可逆动作设置人工批准；
- 保存事件、工具调用和版本用于审计。

### 5. 多 Agent 不是默认答案

[`patterns/agents`](https://github.com/anthropics/claude-cookbooks/tree/main/patterns/agents) 从 chain、route、parallel、orchestrator-workers 到 evaluator-optimizer 逐层增加复杂度。Agent SDK 的 dynamic workflow 又区分：让模型在上下文中协调 subagents，还是让模型生成脚本后由确定性 runtime 执行 fan-out/fan-in。

合理顺序是单调用 → 确定性 workflow → 单 Agent → 多 Agent；只有并行性、上下文隔离或专业化确实带来可测收益时才升级。

## 2026 年新增内容说明了什么

`registry.yaml` 中 31 条 2026 recipe 的主题已经从传统 RAG/提示工程转向系统工程：

- 内容策略执行和成本优化；
- Managed Agents 的预算、Advisor、Skills、地域、记忆、多人协作和生产运维；
- Agent SDK 的 SRE、安全、session browser、托管部署和动态工作流；
- Context engineering、知识图谱、Agentic Search 评测；
- 模型 fallback 与计费。

因此，今天阅读这个仓库不应只从根 README 的 Classification/RAG/Summarization 开始；更有效的入口是 `registry.yaml`、`claude_agent_sdk/README.md` 和 `managed_agents/README.md`。

## 质量与维护边界

### 做得好的部分

- 根环境用 [`uv.lock`](https://github.com/anthropics/claude-cookbooks/blob/main/uv.lock) 锁定，要求 Python 3.11–3.12；核验点锁定 `anthropic==0.109.0`、`claude-agent-sdk==0.1.72`。
- 使用 Ruff、pytest、pre-commit、Notebook 结构验证和链接检查。
- [CI workflows](https://github.com/anthropics/claude-cookbooks/tree/main/.github/workflows) 包含 lint/format、Notebook 测试、链接、作者/registry 校验及 Claude 辅助 review。
- 新 recipe 普遍开始显式展示成本、评测、清理、权限和失败路径。
- Notebook 有数据、输出和图表，适合看到预期结果，而不是只有代码片段。

### 必须警惕的部分

1. **年代混杂。** 仓库同时包含 2023–2026 的示例；部分老 Notebook 仍引用旧模型或旧 API。比如 `tool_use/tool_use_with_pydantic.ipynb` 当前仍使用 `claude-opus-4-1`，而仓库开发规范和现行模型文档已采用更新别名。
2. **根 README 落后。** 它没有完整列出 Agent SDK、Managed Agents、Skills、成本和评测目录，并仍含重命名前 `anthropic-cookbook` 的旧链接；GitHub 通常会重定向，但不应据此判断内容完整度。
3. **依赖不统一。** 根 `pyproject.toml` 只覆盖通用环境，很多 Notebook 会 `%pip install` 自己的依赖；第三方示例还需要各自的 API key、Docker、Node、数据库或云账号。
4. **Beta 漂移。** Skills、Files、context management、Managed Agents 等使用带日期的 Beta API/tool type，不能把 Notebook 中的日期或请求形状视为长期稳定契约。
5. **示例不等于生产服务。** 认证、租户隔离、重试、幂等、告警、速率限制、数据保留和灾备并非每个 recipe 都覆盖。

### 本地静态测试实测

在上述默认分支核验点执行：

```bash
uv run --frozen pytest -q -m 'not slow'
```

结果为：`1550 passed, 163 failed, 51 skipped, 98 deselected`。失败构成为：

- 74：Notebook 有未执行代码单元；
- 39：保存的 execution count 顺序不连续；
- 37：execution count 没从 1 开始；
- 8：非 Python/异常 kernel metadata；
- 3：缺少 kernelspec；
- 1：secret 检测；
- 1：deprecated model 检测。

这不表示有 163 个业务逻辑 bug；大部分是 Notebook 输出/元数据没有满足仓库新测试规则。但它证明当前全仓库并非一次全量绿色验证的版本。[`notebook-tests.yml`](https://github.com/anthropics/claude-cookbooks/blob/main/.github/workflows/notebook-tests.yml) 只检查本次变更的 Notebook；需要真实 API key 的执行测试只对维护者运行，并且没有 key 时跳过。因此，使用者仍需对自己选择的 recipe 单独执行和验证。

本研究没有使用真实 API key 执行会产生费用或外部副作用的 Notebook。

## 建议的学习路线

### 路线 A：第一次使用 Claude API

1. 先按[现行 Messages API 文档](https://platform.claude.com/docs/en/build-with-claude/working-with-messages)完成一次调用；根 README 推荐的旧 `courses` 不应作为 2026 唯一主线。
2. 读 `patterns/agents/basic_workflows.ipynb`，理解 chain、route、parallel。
3. 读 `tool_use/calculator_tool.ipynb` 和 `tool_use/tool_use_with_pydantic.ipynb`，但先更新模型名和 SDK。
4. 读 `misc/prompt_caching.ipynb`、`misc/batch_processing.ipynb`、`misc/building_evals.ipynb`。
5. 最后做 `cost_optimization`，把成本和质量联动起来。

### 路线 B：构建 Agent SDK 应用

按 `claude_agent_sdk/00` → `01` → `02/03` → `05` → `07` 的顺序；需要迁移时插入 04，需要安全时看 06，需要大规模并行时最后看 08。运行前需要 Node、Claude Code CLI、Python 环境和对应外部服务凭证。

### 路线 C：构建 Managed Agent

1. `CMA_iterate_fix_failing_tests`：掌握 Agent/Environment/Session/Event；
2. `CMA_gate_human_in_the_loop`：掌握 `requires_action`；
3. `CMA_prompt_versioning_and_rollback`：建立评测和发布闸门；
4. `CMA_cap_session_spend`：加预算；
5. `CMA_operate_in_production`：Vault、webhook、地域和资源生命周期；
6. 最后才选择 memory、multiagent、advisor 和外部集成。

建议用单独 workspace/API key、低花费模型和硬 spend limit；先在 fixture 上运行，确认清理单元执行成功，再连接真实仓库、Slack、MongoDB 或生产工具。

## 对 Magic-AI-Router 最有价值的部分

1. **路由评价方法：** 直接借鉴 `cost_optimization` 的固定 eval set、pass rate、每任务成本和 Pareto frontier，把“选模型”变成数据决策。
2. **Fallback 与账单：** 参考 `fable_5_fallback_billing/guide.ipynb` 设计 fallback 原因、实际执行模型、计费归因和观测字段，避免路由日志只记录请求模型。
3. **缓存边界：** 从 `prompt_caching` 和 context engineering 提取稳定前缀、自动 caching、compaction 和 tool clearing 规则；缓存键必须包含模型、Beta、system、tool schema 和租户边界。
4. **工具与能力注册：** 借鉴 `registry.yaml` 的元数据驱动目录，但为 Router 增加版本、提供商、地区、价格、能力、上下文、健康状态和弃用字段。
5. **安全策略：** 借鉴内容策略 recipe 的“模型提取 + schema 校验 + 确定性裁决”，用于路由合规、地域和数据敏感度判断，而不是让 LLM 直接决定最终 provider。
6. **可观测性：** 同时记录 input/output/cache token、实际模型、回合数、tool/subagent cost、fallback 和最终评测结果；否则只看单次请求价格会误导优化。

## 实际使用建议

只运行一个专题时，不必把整个仓库当应用安装。仓库包含大量图片和保留的 Notebook 输出，完整工作区较大。推荐：

```bash
git clone https://github.com/anthropics/claude-cookbooks.git
cd claude-cookbooks
cp .env.example .env
uv sync --frozen --all-extras
uv run jupyter lab
```

随后只打开目标 Notebook，先阅读 prerequisites、环境变量、费用和 cleanup，再逐单元运行。复制进项目时，应保留设计与测试，重写凭证、权限、错误处理、状态持久化和部署边界。

