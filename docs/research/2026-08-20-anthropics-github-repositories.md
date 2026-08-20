# Anthropic GitHub 公开仓库全量盘点

- 检索日期：2026-08-20（Asia/Shanghai）
- 检索对象：GitHub 组织 [`anthropics`](https://github.com/anthropics) 在检索时可由 GitHub REST API 返回的**全部公开仓库**；不包含私有、内部或已删除仓库。
- 清单方法：调用组织仓库接口，参数为 `type=all&sort=full_name&direction=asc&per_page=100`，完整读取[第 1 页](https://api.github.com/orgs/anthropics/repos?type=all&sort=full_name&direction=asc&per_page=100&page=1)和[第 2 页](https://api.github.com/orgs/anthropics/repos?type=all&sort=full_name&direction=asc&per_page=100&page=2)，合并后按 `full_name` 排序并去重。第 1 页 100 项、第 2 页 2 项，共 102 项。
- 用途判定：逐仓库调用官方 `GET /repos/anthropics/{repo}/readme` 并阅读根 README；99 个成功，3 个返回 404。README 与仓库简介有差异时以 README 为准，并在“异常与口径”中说明。
- 元数据口径：“语言”是 GitHub API 的 `language`（按仓库字节统计的主要语言，不等于项目唯一语言）；“归档”“Fork”“模板”分别来自 `archived`、`fork`、`is_template`。`未归档` 仅表示 GitHub 状态，不等于仍受维护。

## 总览

| 指标 | 数量 |
|---|---:|
| 公开仓库 | 102 |
| GitHub 标记为 Fork | 23 |
| 已归档 | 12 |
| 模板仓库 | 0 |
| 有根 README | 99 |
| 无根 README | 3 |
| API `description` 为空 | 34 |

| 主要类别 | 数量 | 说明 |
|---|---:|---|
| Claude API SDK、CLI 与分发 | 14 | 官方客户端、旧版 Bedrock 包、tokenizer、Apple 适配和 Homebrew 分发 |
| Claude Code、Agent、插件与行业工作流 | 18 | Claude Code/Agent SDK、GitHub Action、Skills、插件市场和垂直行业套件 |
| 教程、示例与参考实现 | 17 | Workshop、Cookbook、Quickstart、监控、迁移和生产流程参考 |
| 研究、论文、数据与评测 | 18 | 可解释性、安全、对齐、模型透明度、基准和形式化论文附件 |
| Anthropic 工程工具、通用项目与其他 | 12 | 自建或导入的工程组件、通用工具、镜像及一个空仓库 |
| GitHub 标记的上游 Fork | 23 | API `fork=true`；用途按该 fork 当前 README 说明 |
| **合计** | **102** | 每个仓库只归入一个主要类别 |

## 1. Claude API SDK、CLI 与分发（14）

| 仓库 | 用途 | 语言 | 状态 | 一手来源 |
|---|---|---|---|---|
| [`anthropic-bedrock-python`](https://github.com/anthropics/anthropic-bedrock-python) | 旧的 Anthropic Bedrock Python API 包；README 明确说明功能已迁入 `anthropic-sdk-python`，应改用主 Python SDK。 | 未识别 | 未归档；已迁移 | [API](https://api.github.com/repos/anthropics/anthropic-bedrock-python) · [README](https://github.com/anthropics/anthropic-bedrock-python#readme) |
| [`anthropic-bedrock-typescript`](https://github.com/anthropics/anthropic-bedrock-typescript) | 旧的 Anthropic Bedrock TypeScript API 包；README 明确说明已迁到主 TypeScript SDK 的 `packages/bedrock-sdk`。 | 未识别 | 未归档；已迁移 | [API](https://api.github.com/repos/anthropics/anthropic-bedrock-typescript) · [README](https://github.com/anthropics/anthropic-bedrock-typescript#readme) |
| [`anthropic-cli`](https://github.com/anthropics/anthropic-cli) | 官方 Claude Platform 命令行工具 `ant`，可从终端发送消息、管理 agents/sessions、上传文件并调用平台 API。 | Go | 未归档 | [API](https://api.github.com/repos/anthropics/anthropic-cli) · [README](https://github.com/anthropics/anthropic-cli#readme) |
| [`anthropic-sdk-csharp`](https://github.com/anthropics/anthropic-sdk-csharp) | Claude API 官方 C# SDK；README 特别说明 10+ 版本接替了早期社区包命名。 | C# | 未归档 | [API](https://api.github.com/repos/anthropics/anthropic-sdk-csharp) · [README](https://github.com/anthropics/anthropic-sdk-csharp#readme) |
| [`anthropic-sdk-go`](https://github.com/anthropics/anthropic-sdk-go) | Claude API 官方 Go SDK。 | Go | 未归档 | [API](https://api.github.com/repos/anthropics/anthropic-sdk-go) · [README](https://github.com/anthropics/anthropic-sdk-go#readme) |
| [`anthropic-sdk-java`](https://github.com/anthropics/anthropic-sdk-java) | Claude API 官方 Java SDK，提供 Gradle/Maven 包。 | Kotlin | 未归档 | [API](https://api.github.com/repos/anthropics/anthropic-sdk-java) · [README](https://github.com/anthropics/anthropic-sdk-java#readme) |
| [`anthropic-sdk-php`](https://github.com/anthropics/anthropic-sdk-php) | Claude API 官方 PHP SDK，基于 PSR-18 HTTP 客户端接口。 | PHP | 未归档 | [API](https://api.github.com/repos/anthropics/anthropic-sdk-php) · [README](https://github.com/anthropics/anthropic-sdk-php#readme) |
| [`anthropic-sdk-python`](https://github.com/anthropics/anthropic-sdk-python) | Claude API 官方 Python SDK。 | Python | 未归档 | [API](https://api.github.com/repos/anthropics/anthropic-sdk-python) · [README](https://github.com/anthropics/anthropic-sdk-python#readme) |
| [`anthropic-sdk-ruby`](https://github.com/anthropics/anthropic-sdk-ruby) | Claude API 官方 Ruby SDK。 | Ruby | 未归档 | [API](https://api.github.com/repos/anthropics/anthropic-sdk-ruby) · [README](https://github.com/anthropics/anthropic-sdk-ruby#readme) |
| [`anthropic-sdk-typescript`](https://github.com/anthropics/anthropic-sdk-typescript) | Claude API 官方服务端 TypeScript/JavaScript SDK。 | TypeScript | 未归档 | [API](https://api.github.com/repos/anthropics/anthropic-sdk-typescript) · [README](https://github.com/anthropics/anthropic-sdk-typescript#readme) |
| [`anthropic-tokenizer-typescript`](https://github.com/anthropics/anthropic-tokenizer-typescript) | 旧模型的 TypeScript token 计数器；README 警告其算法对 Claude 3 起已不准确，只能粗估，优先使用响应中的 `usage`。 | TypeScript | 未归档；README 警告已过时 | [API](https://api.github.com/repos/anthropics/anthropic-tokenizer-typescript) · [README](https://github.com/anthropics/anthropic-tokenizer-typescript#readme) |
| [`anthropic-tools`](https://github.com/anthropics/anthropic-tools) | 早期工具/函数调用研究预览 SDK；README 已标记弃用，推荐改用 Claude API 的正式 tool use。 | Python | **已归档**；已弃用 | [API](https://api.github.com/repos/anthropics/anthropic-tools) · [README](https://github.com/anthropics/anthropic-tools#readme) |
| [`ClaudeForFoundationModels`](https://github.com/anthropics/ClaudeForFoundationModels) | 把 Claude 作为服务端模型接入 Apple Foundation Models 的 Swift 包，适配 `LanguageModel`/`LanguageModelSession`、流式、结构化输出和工具调用；README 标为 beta。 | Swift | 未归档；Beta | [API](https://api.github.com/repos/anthropics/ClaudeForFoundationModels) · [README](https://github.com/anthropics/ClaudeForFoundationModels#readme) |
| [`homebrew-tap`](https://github.com/anthropics/homebrew-tap) | Anthropic 工具的 Homebrew Cask/Formula 分发仓库；无根 README，当前用途依据 API 描述和 `Casks/` 目录。 | Ruby | 未归档；无根 README | [API](https://api.github.com/repos/anthropics/homebrew-tap) · [Casks](https://github.com/anthropics/homebrew-tap/tree/main/Casks) |

## 2. Claude Code、Agent、插件与行业工作流（18）

| 仓库 | 用途 | 语言 | 状态 | 一手来源 |
|---|---|---|---|---|
| [`claude-agent-sdk-python`](https://github.com/anthropics/claude-agent-sdk-python) | Claude Agent SDK 的 Python 实现，用代码调用 Claude Code 的 agent 能力，支持工具、权限和异步消息流。 | Python | 未归档 | [API](https://api.github.com/repos/anthropics/claude-agent-sdk-python) · [README](https://github.com/anthropics/claude-agent-sdk-python#readme) |
| [`claude-agent-sdk-typescript`](https://github.com/anthropics/claude-agent-sdk-typescript) | Claude Agent SDK 的 TypeScript 实现，用于构建能理解代码库、编辑文件、运行命令和执行复杂工作流的 agents。 | Shell | 未归档 | [API](https://api.github.com/repos/anthropics/claude-agent-sdk-typescript) · [README](https://github.com/anthropics/claude-agent-sdk-typescript#readme) |
| [`claude-ai-mcp`](https://github.com/anthropics/claude-ai-mcp) | Claude.ai 与 MCP 集成的公告、兼容性变更、问题反馈和功能请求中心，不是 MCP 客户端/服务器实现。 | 未识别 | 未归档 | [API](https://api.github.com/repos/anthropics/claude-ai-mcp) · [README](https://github.com/anthropics/claude-ai-mcp#readme) |
| [`claude-code`](https://github.com/anthropics/claude-code) | Claude Code 主仓库：终端/IDE/GitHub 中的 agentic coding 工具，可理解代码库、执行任务、解释代码和处理 Git 工作流。 | Python | 未归档 | [API](https://api.github.com/repos/anthropics/claude-code) · [README](https://github.com/anthropics/claude-code#readme) |
| [`claude-code-action`](https://github.com/anthropics/claude-code-action) | 在 GitHub Issue/PR/工作流中运行 Claude Code 的通用 Action，可回答问题、审查或实现代码，支持多种云端认证。 | TypeScript | 未归档 | [API](https://api.github.com/repos/anthropics/claude-code-action) · [README](https://github.com/anthropics/claude-code-action#readme) |
| [`claude-code-base-action`](https://github.com/anthropics/claude-code-base-action) | `claude-code-action` 中 `base-action` 子目录的自动镜像；提供在 GitHub Actions 中安装并运行 Claude Code 的底层薄封装，不自行建立信任边界。 | TypeScript | 未归档；镜像 | [API](https://api.github.com/repos/anthropics/claude-code-base-action) · [README](https://github.com/anthropics/claude-code-base-action#readme) |
| [`claude-code-security-review`](https://github.com/anthropics/claude-code-security-review) | 用 Claude Code 对 PR 变更做语义安全审查并回写发现的 GitHub Action；README 警告它未对提示注入加固，应只审查可信 PR。 | Python | 未归档 | [API](https://api.github.com/repos/anthropics/claude-code-security-review) · [README](https://github.com/anthropics/claude-code-security-review#readme) |
| [`claude-for-legal`](https://github.com/anthropics/claude-for-legal) | 法律工作流的参考 agents、skills 和数据连接器，可作为 Cowork/Claude Code 插件或 Managed Agent 模板使用；产物仅供律师复核。 | Python | 未归档 | [API](https://api.github.com/repos/anthropics/claude-for-legal) · [README](https://github.com/anthropics/claude-for-legal#readme) |
| [`claude-plugins-community`](https://github.com/anthropics/claude-plugins-community) | Claude Cowork/Code 社区插件市场的只读镜像，清单由 Anthropic 内部审核流程每日同步，不接受直接 PR。 | Python | 未归档；只读镜像 | [API](https://api.github.com/repos/anthropics/claude-plugins-community) · [README](https://github.com/anthropics/claude-plugins-community#readme) |
| [`claude-plugins-official`](https://github.com/anthropics/claude-plugins-official) | Claude Code 的精选插件目录；README 表明其中既有 Anthropic 内部插件，也有达到目录标准的外部插件。 | Python | 未归档 | [API](https://api.github.com/repos/anthropics/claude-plugins-official) · [README](https://github.com/anthropics/claude-plugins-official#readme) |
| [`claude-tag-plugins`](https://github.com/anthropics/claude-tag-plugins) | 为 `@claude` agent 连接 Asana、BigQuery、Confluence、Datadog、Drive、Jira 等 SaaS 的插件集合。 | Shell | 未归档 | [API](https://api.github.com/repos/anthropics/claude-tag-plugins) · [README](https://github.com/anthropics/claude-tag-plugins#readme) |
| [`devcontainer-features`](https://github.com/anthropics/devcontainer-features) | Anthropic 的 Dev Container Features，目前包含自动安装 Claude Code CLI 的 feature 及测试。 | Shell | 未归档 | [API](https://api.github.com/repos/anthropics/devcontainer-features) · [README](https://github.com/anthropics/devcontainer-features#readme) |
| [`financial-services`](https://github.com/anthropics/financial-services) | 金融服务领域的参考 agents、skills 和连接器，覆盖投行、股票研究、私募和财富管理；输出需专业人员审核。 | Python | 未归档 | [API](https://api.github.com/repos/anthropics/financial-services) · [README](https://github.com/anthropics/financial-services#readme) |
| [`healthcare`](https://github.com/anthropics/healthcare) | 面向医疗支付方、服务方、药企和工程工作的 Claude 插件，包含临床文本抽取、试验方案、FHIR 等 skills 与托管 MCP 连接。 | JavaScript | 未归档 | [API](https://api.github.com/repos/anthropics/healthcare) · [README](https://github.com/anthropics/healthcare#readme) |
| [`k12-teacher-skills`](https://github.com/anthropics/k12-teacher-skills) | Claude for Teachers 使用的 K-12 课程设计、分层教学 skills 与评测 rubric，部分内容与 Learning Commons 共建。 | Python | 未归档 | [API](https://api.github.com/repos/anthropics/k12-teacher-skills) · [README](https://github.com/anthropics/k12-teacher-skills#readme) |
| [`knowledge-work-plugins`](https://github.com/anthropics/knowledge-work-plugins) | 面向生产力、销售、客服、产品、市场等知识工作角色的 Cowork/Claude Code 插件集合，组合 skills、连接器、命令和 sub-agents。 | Python | 未归档 | [API](https://api.github.com/repos/anthropics/knowledge-work-plugins) · [README](https://github.com/anthropics/knowledge-work-plugins#readme) |
| [`life-sciences`](https://github.com/anthropics/life-sciences) | 生命科学 Claude Code 市场，用于安装 PubMed、BioRender、Synapse 等 MCP/skills；API 描述进一步澄清仓库长期只保留 marketplace 清单，不托管实际 MCP server 源码。 | Python | 未归档 | [API](https://api.github.com/repos/anthropics/life-sciences) · [README](https://github.com/anthropics/life-sciences#readme) |
| [`skills`](https://github.com/anthropics/skills) | Anthropic 的 Claude Skills 公共实现与示例集合，包括文档、表格、演示、创意及技术工作流；部分是开源，部分仅 source-available。 | Python | 未归档 | [API](https://api.github.com/repos/anthropics/skills) · [README](https://github.com/anthropics/skills#readme) |

## 3. 教程、示例与参考实现（17）

| 仓库 | 用途 | 语言 | 状态 | 一手来源 |
|---|---|---|---|---|
| [`agent-sdk-workshop`](https://github.com/anthropics/agent-sdk-workshop) | Claude Agent SDK 的动手 Workshop；通过配置组件与编写提示来组装生产型 agent，无需在练习中编写代码。 | Python | 未归档 | [API](https://api.github.com/repos/anthropics/agent-sdk-workshop) · [README](https://github.com/anthropics/agent-sdk-workshop#readme) |
| [`anthropic-retrieval-demo`](https://github.com/anthropics/anthropic-retrieval-demo) | 使用 Claude 与 Elasticsearch、向量库、Web 搜索和 Wikipedia 实验搜索/检索的轻量演示，探索传统 RAG 的替代路径。 | Python | **已归档** | [API](https://api.github.com/repos/anthropics/anthropic-retrieval-demo) · [README](https://github.com/anthropics/anthropic-retrieval-demo#readme) |
| [`claude-agent-sdk-demos`](https://github.com/anthropics/claude-agent-sdk-demos) | Claude Agent SDK 的多应用演示，包括邮件助手、Excel 和 Hello World；README 明确只用于本地开发，不宜生产部署。 | TypeScript | 未归档；演示 | [API](https://api.github.com/repos/anthropics/claude-agent-sdk-demos) · [README](https://github.com/anthropics/claude-agent-sdk-demos#readme) |
| [`claude-code-monitoring-guide`](https://github.com/anthropics/claude-code-monitoring-guide) | 衡量 Claude Code 成本、使用、生产力和 ROI 的监控指南，涵盖 Prometheus/OpenTelemetry 和报表思路。 | 未识别 | 未归档 | [API](https://api.github.com/repos/anthropics/claude-code-monitoring-guide) · [README](https://github.com/anthropics/claude-code-monitoring-guide#readme) |
| [`claude-cookbooks`](https://github.com/anthropics/claude-cookbooks) | 可复制到应用中的 Claude 代码配方和指南，主要用 Python 展示分类、工具使用等能力与模式。 | Jupyter Notebook | 未归档 | [API](https://api.github.com/repos/anthropics/claude-cookbooks) · [README](https://github.com/anthropics/claude-cookbooks#readme) |
| [`claude-desktop-buddy`](https://github.com/anthropics/claude-desktop-buddy) | Claude Desktop/Cowork/Code 的 BLE maker API 协议参考和 ESP32 桌面宠物示例，可显示消息/权限提示并确认或拒绝。 | C++ | 未归档 | [API](https://api.github.com/repos/anthropics/claude-desktop-buddy) · [README](https://github.com/anthropics/claude-desktop-buddy#readme) |
| [`claude-quickstarts`](https://github.com/anthropics/claude-quickstarts) | 可部署的 Claude API 起步项目集合，包括客服、金融分析、computer/browser use 等应用骨架。 | TypeScript | 未归档 | [API](https://api.github.com/repos/anthropics/claude-quickstarts) · [README](https://github.com/anthropics/claude-quickstarts#readme) |
| [`code-migration-kit-with-claude-code`](https://github.com/anthropics/code-migration-kit-with-claude-code) | 用 Claude Code 做大规模语言迁移的提示、模板与脚本起步包，默认针对保持架构不变的迁移；README 称参考代码且不维护。 | Python | 未归档；README：不维护 | [API](https://api.github.com/repos/anthropics/code-migration-kit-with-claude-code) · [README](https://github.com/anthropics/code-migration-kit-with-claude-code#readme) |
| [`courses`](https://github.com/anthropics/courses) | Anthropic 教学课程集合，覆盖 API 基础、提示工程、真实场景提示、prompt eval 和 tool use。 | Jupyter Notebook | 未归档 | [API](https://api.github.com/repos/anthropics/courses) · [README](https://github.com/anthropics/courses#readme) |
| [`cwc-long-running-agents`](https://github.com/anthropics/cwc-long-running-agents) | Code with Claude 2026 的长时运行 agent harness 原语示例，展示默认失败契约、独立 evaluator 和跨会话 handoff；不是开箱即用框架。 | Shell | 未归档；README：活动示例/不维护 | [API](https://api.github.com/repos/anthropics/cwc-long-running-agents) · [README](https://github.com/anthropics/cwc-long-running-agents#readme) |
| [`cwc-workshops`](https://github.com/anthropics/cwc-workshops) | Anthropic Code with Claude 工作坊材料，覆盖模型选择、多 agent 分解、Managed Agents、记忆、评测驱动开发等；README 称不维护。 | TypeScript | 未归档；README：不维护 | [API](https://api.github.com/repos/anthropics/cwc-workshops) · [README](https://github.com/anthropics/cwc-workshops#readme) |
| [`defending-code-reference-harness`](https://github.com/anthropics/defending-code-reference-harness) | 用 Claude 自主执行漏洞侦察、发现、分诊、报告和修复的参考 harness，附威胁建模与扫描 skills；README 称不维护。 | Python | 未归档；README：不维护 | [API](https://api.github.com/repos/anthropics/defending-code-reference-harness) · [README](https://github.com/anthropics/defending-code-reference-harness#readme) |
| [`html-effectiveness`](https://github.com/anthropics/html-effectiveness) | 配合“HTML 的非凡有效性”文章的独立 HTML 示例画廊，展示代码审查、设计系统、幻灯片、状态报告和小编辑器。 | HTML | 未归档 | [API](https://api.github.com/repos/anthropics/html-effectiveness) · [README](https://github.com/anthropics/html-effectiveness#readme) |
| [`launch-your-agent`](https://github.com/anthropics/launch-your-agent) | 引导技术创始人从访谈、定义 v0 到部署、评分、迭代和定时运行 Claude Managed Agent 的 Claude Code skill；教育性参考实现。 | HTML | 未归档；README：不维护 | [API](https://api.github.com/repos/anthropics/launch-your-agent) · [README](https://github.com/anthropics/launch-your-agent#readme) |
| [`oncall-kit`](https://github.com/anthropics/oncall-kit) | Claude 辅助值班的参考套件：从历史事故生成 triage playbook，在事故频道做可追溯初诊并由人类决定和部署修复。 | Python | 未归档；README：不维护 | [API](https://api.github.com/repos/anthropics/oncall-kit) · [README](https://github.com/anthropics/oncall-kit#readme) |
| [`prompt-eng-interactive-tutorial`](https://github.com/anthropics/prompt-eng-interactive-tutorial) | 交互式提示工程教程，以 9 章练习讲解提示结构、常见失败和优化方法。 | Jupyter Notebook | 未归档 | [API](https://api.github.com/repos/anthropics/prompt-eng-interactive-tutorial) · [README](https://github.com/anthropics/prompt-eng-interactive-tutorial#readme) |
| [`riv2025-long-horizon-coding-agent-demo`](https://github.com/anthropics/riv2025-long-horizon-coding-agent-demo) | AWS re:Invent 2025 演示：由 GitHub Issue 驱动、基于 Bedrock AgentCore 和 Claude Agent SDK 的长周期全栈编码 agent。 | Python | **已归档**；演示 | [API](https://api.github.com/repos/anthropics/riv2025-long-horizon-coding-agent-demo) · [README](https://github.com/anthropics/riv2025-long-horizon-coding-agent-demo#readme) |

## 4. 研究、论文、数据与评测（18）

| 仓库 | 用途 | 语言 | 状态 | 一手来源 |
|---|---|---|---|---|
| [`attribution-graphs-frontend`](https://github.com/anthropics/attribution-graphs-frontend) | “On the Biology of a Large Language Model”与“Circuit Tracing”研究所用 attribution graph 前端代码快照。 | JavaScript | **已归档** | [API](https://api.github.com/repos/anthropics/attribution-graphs-frontend) · [README](https://github.com/anthropics/attribution-graphs-frontend#readme) |
| [`claude-constitution`](https://github.com/anthropics/claude-constitution) | 发布 Claude 宪章，即描述期望价值观、行为原则和困难权衡的基础文档，并保留未来版本。 | 未识别 | 未归档 | [API](https://api.github.com/repos/anthropics/claude-constitution) · [README](https://github.com/anthropics/claude-constitution#readme) |
| [`ConstitutionalHarmlessnessPaper`](https://github.com/anthropics/ConstitutionalHarmlessnessPaper) | “Constitutional AI: Harmlessness from AI Feedback”论文的补充材料。 | 未识别 | **已归档** | [API](https://api.github.com/repos/anthropics/ConstitutionalHarmlessnessPaper) · [README](https://github.com/anthropics/ConstitutionalHarmlessnessPaper#readme) |
| [`cryptography-research-demo`](https://github.com/anthropics/cryptography-research-demo) | 相关密码分析论文的研究代码附件，分 AES、HAWK、LEA 三个独立组件；README 称不维护。 | C | 未归档；README：研究附件/不维护 | [API](https://api.github.com/repos/anthropics/cryptography-research-demo) · [README](https://github.com/anthropics/cryptography-research-demo#readme) |
| [`DecompositionFaithfulnessPaper`](https://github.com/anthropics/DecompositionFaithfulnessPaper) | “Question Decomposition Improves the Faithfulness of Model-Generated Reasoning”论文实验所用 prompts。 | Python | **已归档** | [API](https://api.github.com/repos/anthropics/DecompositionFaithfulnessPaper) · [README](https://github.com/anthropics/DecompositionFaithfulnessPaper#readme) |
| [`evals`](https://github.com/anthropics/evals) | “Discovering Language Model Behaviors with Model-Written Evaluations”使用的模型生成评测数据，涵盖 persona、谄媚、高级 AI 风险和性别偏差。 | 未识别 | 未归档 | [API](https://api.github.com/repos/anthropics/evals) · [README](https://github.com/anthropics/evals#readme) |
| [`headvis`](https://github.com/anthropics/headvis) | Transformer attention head 可视化参考工具，可查看高激活序列、attention patterns、head 指标与 Q/K/O/V 投影；README 称不维护。 | Svelte | 未归档；README：不维护 | [API](https://api.github.com/repos/anthropics/headvis) · [README](https://github.com/anthropics/headvis#readme) |
| [`hh-rlhf`](https://github.com/anthropics/hh-rlhf) | Helpful/Harmless RLHF 人类偏好数据和红队数据；README 指向 Hugging Face 上的替代托管版本。 | 未识别 | **已归档**；数据已迁移 | [API](https://api.github.com/repos/anthropics/hh-rlhf) · [README](https://github.com/anthropics/hh-rlhf#readme) |
| [`jacobian-lens`](https://github.com/anthropics/jacobian-lens) | “Verbalizable Representations Form a Global Workspace in Language Models”的配套代码，用平均 Jacobian 将中间激活映射到最终词表表示。 | Python | 未归档；README：不维护 | [API](https://api.github.com/repos/anthropics/jacobian-lens) · [README](https://github.com/anthropics/jacobian-lens#readme) |
| [`model-cards`](https://github.com/anthropics/model-cards) | Claude Model Cards 的补充材料；无根 README，当前仓库只展示 `claude-opus-4-5-20251101` 资料目录，因此不扩展推断其内容。 | 未识别 | 未归档；无根 README | [API](https://api.github.com/repos/anthropics/model-cards) · [内容目录](https://github.com/anthropics/model-cards/tree/main/claude-opus-4-5-20251101) |
| [`original_performance_takehome`](https://github.com/anthropics/original_performance_takehome) | Anthropic 早期性能工程 take-home 的公开版本，可尝试优化模拟机程序并与 Claude Opus 4.5/人类成绩比较。 | Python | 未归档 | [API](https://api.github.com/repos/anthropics/original_performance_takehome) · [README](https://github.com/anthropics/original_performance_takehome#readme) |
| [`political-neutrality-eval`](https://github.com/anthropics/political-neutrality-eval) | 政治均衡性/中立性 paired-prompts 评测的构造、评分标准、指标和数据，配合 Anthropic 相关发布。 | Python | 未归档 | [API](https://api.github.com/repos/anthropics/political-neutrality-eval) · [README](https://github.com/anthropics/political-neutrality-eval#readme) |
| [`rogue-deploy-eval`](https://github.com/anthropics/rogue-deploy-eval) | 玩具评测：衡量模型在完成另一任务时篡改推理代码、绕过生成监控的能力；部分私有执行/推理依赖留作 `TO_FILL`。 | Python | **已归档** | [API](https://api.github.com/repos/anthropics/rogue-deploy-eval) · [README](https://github.com/anthropics/rogue-deploy-eval#readme) |
| [`scone-bench`](https://github.com/anthropics/scone-bench) | LLM agent 智能合约漏洞发现与利用基准，含 417 个基于历史 EVM 合约状态的本地任务；README 称不维护。 | Python | 未归档；README：Benchmark/不维护 | [API](https://api.github.com/repos/anthropics/scone-bench) · [README](https://github.com/anthropics/scone-bench#readme) |
| [`sleeper-agents-paper`](https://github.com/anthropics/sleeper-agents-paper) | “Sleeper Agents”论文使用的样本、few-shot prompts 与部分后门训练数据。 | 未识别 | **已归档** | [API](https://api.github.com/repos/anthropics/sleeper-agents-paper) · [README](https://github.com/anthropics/sleeper-agents-paper#readme) |
| [`sycophancy-to-subterfuge-paper`](https://github.com/anthropics/sycophancy-to-subterfuge-paper) | “Sycophancy to Subterfuge”奖励篡改研究的 prompts、环境描述和各阶段样本。 | 未识别 | **已归档** | [API](https://api.github.com/repos/anthropics/sycophancy-to-subterfuge-paper) · [README](https://github.com/anthropics/sycophancy-to-subterfuge-paper#readme) |
| [`toy-models-of-superposition`](https://github.com/anthropics/toy-models-of-superposition) | Anthropic “Toy Models of Superposition”论文的配套 notebooks。 | Jupyter Notebook | **已归档** | [API](https://api.github.com/repos/anthropics/toy-models-of-superposition) · [README](https://github.com/anthropics/toy-models-of-superposition#readme) |
| [`zeta-23-lean`](https://github.com/anthropics/zeta-23-lean) | 黎曼 ζ 函数超过三分之二零点为单零点且位于临界线之论文的 Lean 4/Mathlib 完整形式化；README 称无 `sorry` 的静态研究附件。 | Lean | 未归档；README：研究附件/不维护 | [API](https://api.github.com/repos/anthropics/zeta-23-lean) · [README](https://github.com/anthropics/zeta-23-lean#readme) |

## 5. Anthropic 工程工具、通用项目与其他（12）

| 仓库 | 用途 | 语言 | 状态 | 一手来源 |
|---|---|---|---|---|
| [`amulet2`](https://github.com/anthropics/amulet2) | AMulet 2.2：验证并为 AIG 表示的有符号/无符号整数乘法器生成证书的形式化工具。 | C++ | 未归档 | [API](https://api.github.com/repos/anthropics/amulet2) · [README](https://github.com/anthropics/amulet2#readme) |
| [`blobfile`](https://github.com/anthropics/blobfile) | 为本地、Google Cloud Storage 和 Azure Blob Storage 提供类似 Python `open`/`os.path`/`shutil` 的统一文件接口。 | Python | 未归档 | [API](https://api.github.com/repos/anthropics/blobfile) · [README](https://github.com/anthropics/blobfile#readme) |
| [`buffa`](https://github.com/anthropics/buffa) | 支持 protobuf editions、二进制/JSON/text、零拷贝 view 与反射的纯 Rust Protocol Buffers 实现；README 称由 Claude 和协作者编写。 | Rust | 未归档 | [API](https://api.github.com/repos/anthropics/buffa) · [README](https://github.com/anthropics/buffa#readme) |
| [`cargo-nix-plugin`](https://github.com/anthropics/cargo-nix-plugin) | 在 Nix 求值期原生解析 Cargo workspace/lock/index 的插件，用 `builtins.resolveCargoWorkspace` 取代生成并提交巨大 `Cargo.nix`。 | Rust | 未归档 | [API](https://api.github.com/repos/anthropics/cargo-nix-plugin) · [README](https://github.com/anthropics/cargo-nix-plugin#readme) |
| [`claudes-c-compiler`](https://github.com/anthropics/claudes-c-compiler) | Claude Opus 4.6 从零用 Rust 编写的实验性 C 编译器，可面向 x86、ARM、RISC-V 并生成 ELF；README 明确不建议实际使用且未验证正确性。 | Rust | 未归档；实验展示 | [API](https://api.github.com/repos/anthropics/claudes-c-compiler) · [README](https://github.com/anthropics/claudes-c-compiler#readme) |
| [`homebrew-claude`](https://github.com/anthropics/homebrew-claude) | 空仓库：API `size=0`、无描述、无根 README，无法从第一方内容可靠判断计划用途。 | 未识别 | 未归档；**空仓库** | [API](https://api.github.com/repos/anthropics/homebrew-claude) |
| [`mockturtle`](https://github.com/anthropics/mockturtle) | C++17 逻辑网络库，提供 AIG/MIG/k-LUT 等网络结构及逻辑综合、优化算法。 | C++ | 未归档 | [API](https://api.github.com/repos/anthropics/mockturtle) · [README](https://github.com/anthropics/mockturtle#readme) |
| [`OpenROAD-flow-scripts`](https://github.com/anthropics/OpenROAD-flow-scripts) | RTL 到 GDSII 的 OpenROAD 自动芯片设计流程；API `fork=false`，但仓库描述明确称其为上游 release tags 的只读镜像。 | Verilog | 未归档；只读镜像（非 GitHub fork） | [API](https://api.github.com/repos/anthropics/OpenROAD-flow-scripts) · [README](https://github.com/anthropics/OpenROAD-flow-scripts#readme) |
| [`PySvelte`](https://github.com/anthropics/PySvelte) | 将 Python 深度学习研究与 Svelte/Web 可视化连接起来的实验库；README 明确完全不受支持且部分功能需自行实现配置。 | Python | **已归档**；不受支持 | [API](https://api.github.com/repos/anthropics/PySvelte) · [README](https://github.com/anthropics/PySvelte#readme) |
| [`redis-py`](https://github.com/anthropics/redis-py) | Redis 键值存储的 Python 客户端；GitHub API 未标为 fork，根 README 是该通用客户端的说明。 | Python | 未归档；非 GitHub fork | [API](https://api.github.com/repos/anthropics/redis-py) · [README](https://github.com/anthropics/redis-py#readme) |
| [`s5cmd`](https://github.com/anthropics/s5cmd) | Anthropic 修改的 `s5cmd`：README 明确称为 fork，并增加 GCS 与 Workload Identity Federation 支持以避免长期 HMAC 密钥；API `fork=false`。 | Go | 未归档；README 称 fork（非 GitHub fork） | [API](https://api.github.com/repos/anthropics/s5cmd) · [README](https://github.com/anthropics/s5cmd#readme) |
| [`tailscale-hint-extension`](https://github.com/anthropics/tailscale-hint-extension) | Chrome 扩展：当 Tailscale MagicDNS `.local` 域名解析失败时，展示服务与 Tailscale 连通性图和排障提示。 | HTML | 未归档 | [API](https://api.github.com/repos/anthropics/tailscale-hint-extension) · [README](https://github.com/anthropics/tailscale-hint-extension#readme) |

## 6. GitHub 标记的上游 Fork（23）

以下 23 项均由 GitHub API 返回 `fork=true`。它们主要是 Anthropic 保留或修改的上游工程分支；“上游”来自仓库详情的 `parent.full_name`，不能据此假定 Anthropic fork 与上游当前完全一致。

| 仓库 | 用途 | 语言 | 状态/上游 | 一手来源 |
|---|---|---|---|---|
| [`apitools`](https://github.com/anthropics/apitools) | 已弃用的 `google-apitools`：用于构建调用 Google API 的 Python 客户端工具。 | Python | Fork：`google/apitools` | [API](https://api.github.com/repos/anthropics/apitools) · [README](https://github.com/anthropics/apitools#readme) |
| [`argo-cd`](https://github.com/anthropics/argo-cd) | Kubernetes 的声明式 GitOps 持续交付工具 Argo CD。 | 未识别 | Fork：`argoproj/argo-cd` | [API](https://api.github.com/repos/anthropics/argo-cd) · [README](https://github.com/anthropics/argo-cd#readme) |
| [`beam`](https://github.com/anthropics/beam) | Apache Beam：统一定义批处理和流式数据并行 pipeline，并由多种分布式 runner 执行。 | Java | Fork：`apache/beam` | [API](https://api.github.com/repos/anthropics/beam) · [README](https://github.com/anthropics/beam#readme) |
| [`cfaulthandler`](https://github.com/anthropics/cfaulthandler) | 类似 Python `faulthandler`，但额外打印 C 调用栈，便于调试扩展模块崩溃。 | 未识别 | Fork：`timmaxw/cfaulthandler` | [API](https://api.github.com/repos/anthropics/cfaulthandler) · [README](https://github.com/anthropics/cfaulthandler#readme) |
| [`github-mcp-server`](https://github.com/anthropics/github-mcp-server) | GitHub 官方 MCP Server，为 MCP 客户端暴露 GitHub API、搜索和自动化能力。 | Go | Fork：`github/github-mcp-server` | [API](https://api.github.com/repos/anthropics/github-mcp-server) · [README](https://github.com/anthropics/github-mcp-server#readme) |
| [`httpcore`](https://github.com/anthropics/httpcore) | 低层 Python HTTP 客户端核心；README 明确这是 Anthropic 基于 httpcore v1.0.9 的 fork。 | Python | Fork：`encode/httpcore` | [API](https://api.github.com/repos/anthropics/httpcore) · [README](https://github.com/anthropics/httpcore#readme) |
| [`hypercorn`](https://github.com/anthropics/hypercorn) | 基于 sans-io HTTP 库、支持 ASGI/WSGI、HTTP/1/2 和 WebSocket 的 Python Web 服务器。 | Python | Fork：`pgjones/hypercorn` | [API](https://api.github.com/repos/anthropics/hypercorn) · [README](https://github.com/anthropics/hypercorn#readme) |
| [`leptos-chartistry`](https://github.com/anthropics/leptos-chartistry) | Rust Leptos 框架的可扩展图表组件库 Chartistry。 | 未识别 | Fork：`feral-dot-io/leptos-chartistry` | [API](https://api.github.com/repos/anthropics/leptos-chartistry) · [README](https://github.com/anthropics/leptos-chartistry#readme) |
| [`maestro`](https://github.com/anthropics/maestro) | Netflix 的通用数据/机器学习 workflow-as-a-service 编排器。 | 未识别 | Fork：`Netflix/maestro` | [API](https://api.github.com/repos/anthropics/maestro) · [README](https://github.com/anthropics/maestro#readme) |
| [`nix-eval-jobs`](https://github.com/anthropics/nix-eval-jobs) | 并行求值 Nix attribute set 并输出流式 JSON，适合高耗时/高内存 CI 求值。 | 未识别 | Fork：`NixOS/nix-eval-jobs` | [API](https://api.github.com/repos/anthropics/nix-eval-jobs) · [README](https://github.com/anthropics/nix-eval-jobs#readme) |
| [`orjson`](https://github.com/anthropics/orjson) | 面向 Python 的高性能、严格 JSON 序列化/反序列化库，原生支持 dataclass、datetime、NumPy、UUID 等。 | Python | Fork：`ijl/orjson` | [API](https://api.github.com/repos/anthropics/orjson) · [README](https://github.com/anthropics/orjson#readme) |
| [`python-tblib`](https://github.com/anthropics/python-tblib) | Python exception/traceback 序列化库，使 traceback 可跨进程传输和重建。 | Python | Fork：`ionelmc/python-tblib` | [API](https://api.github.com/repos/anthropics/python-tblib) · [README](https://github.com/anthropics/python-tblib#readme) |
| [`rayon`](https://github.com/anthropics/rayon) | Rust 数据并行库，可把顺序迭代等计算改为并行，同时保持数据竞争安全。 | 未识别 | Fork：`rayon-rs/rayon` | [API](https://api.github.com/repos/anthropics/rayon) · [README](https://github.com/anthropics/rayon#readme) |
| [`rclone`](https://github.com/anthropics/rclone) | 跨多种云存储同步、复制和管理文件的命令行工具，即“云存储版 rsync”。 | 未识别 | Fork：`rclone/rclone` | [API](https://api.github.com/repos/anthropics/rclone) · [README](https://github.com/anthropics/rclone#readme) |
| [`riegeli-rs`](https://github.com/anthropics/riegeli-rs) | Google Riegeli/records 的纯 Rust 实现，提供可 seek、压缩、高吞吐记录存储，并与 C++ 格式字节兼容。 | 未识别 | Fork：`mikedanese/riegeli-rs` | [API](https://api.github.com/repos/anthropics/riegeli-rs) · [README](https://github.com/anthropics/riegeli-rs#readme) |
| [`sse-starlette`](https://github.com/anthropics/sse-starlette) | 为 Starlette/FastAPI 提供 Server-Sent Events 响应和流式支持。 | 未识别 | Fork：`sysid/sse-starlette` | [API](https://api.github.com/repos/anthropics/sse-starlette) · [README](https://github.com/anthropics/sse-starlette#readme) |
| [`swift-markdown`](https://github.com/anthropics/swift-markdown) | Swift 的 Markdown 解析、构建、编辑和分析包，解析器基于 GitHub Flavored Markdown。 | 未识别 | Fork：`swiftlang/swift-markdown` | [API](https://api.github.com/repos/anthropics/swift-markdown) · [README](https://github.com/anthropics/swift-markdown#readme) |
| [`swift-markdown-ui`](https://github.com/anthropics/swift-markdown-ui) | 在 SwiftUI 中显示并定制 Markdown 的 UI 库。 | 未识别 | Fork：`gonzalezreal/swift-markdown-ui` | [API](https://api.github.com/repos/anthropics/swift-markdown-ui) · [README](https://github.com/anthropics/swift-markdown-ui#readme) |
| [`terragrunt`](https://github.com/anthropics/terragrunt) | 扩展和编排 OpenTofu/Terraform 基础设施即代码的工具。 | 未识别 | Fork：`gruntwork-io/terragrunt` | [API](https://api.github.com/repos/anthropics/terragrunt) · [README](https://github.com/anthropics/terragrunt#readme) |
| [`tokio`](https://github.com/anthropics/tokio) | Rust 异步运行时，提供 I/O、网络、调度、定时器和并发基础设施。 | Rust | Fork：`tokio-rs/tokio` | [API](https://api.github.com/repos/anthropics/tokio) · [README](https://github.com/anthropics/tokio#readme) |
| [`torchtyping`](https://github.com/anthropics/torchtyping) | 为 PyTorch tensor 的 shape/dtype/name 提供类型注解和运行时检查；README 建议新项目改用 `jaxtyping`。 | Python | Fork：`patrick-kidger/torchtyping` | [API](https://api.github.com/repos/anthropics/torchtyping) · [README](https://github.com/anthropics/torchtyping#readme) |
| [`triton`](https://github.com/anthropics/triton) | 用于编写高性能深度学习 primitive 的 Triton 语言和编译器。 | C++ | Fork：`triton-lang/triton` | [API](https://api.github.com/repos/anthropics/triton) · [README](https://github.com/anthropics/triton#readme) |
| [`xls`](https://github.com/anthropics/xls) | Google XLS 高层次综合工具链，把高层功能描述生成可综合 Verilog/SystemVerilog 硬件设计。 | 未识别 | Fork：`google/xls` | [API](https://api.github.com/repos/anthropics/xls) · [README](https://github.com/anthropics/xls#readme) |

## 异常与口径说明

1. **仅代表公开面。** 102 是 2026-08-20 时 REST API 可见的公开仓库数，不应解读为 Anthropic 全部代码仓库数。
2. **3 个仓库没有根 README。** `homebrew-claude` 是 `size=0` 的空仓库；`homebrew-tap` 只根据 API 描述和 `Casks/` 目录说明；`model-cards` 只根据 API 描述和现有资料目录说明。`original_performance_takehome` 的根文件名是大小写不同的 `Readme.md`，REST README 接口可以正确找到，因此不在缺失名单。
3. **API 状态与维护声明不是一回事。** 多个仓库仍是 `archived=false`，但 README 明确称“not maintained”或“not accepting contributions”；表中同时保留两种信息，不把“未归档”写成“活跃维护”。
4. **迁移/弃用但未归档。** 两个 `anthropic-bedrock-*` 仓库已迁移到主 SDK；`anthropic-tokenizer-typescript` 对 Claude 3 起不准确。这些仓库 API 仍未归档。
5. **Fork 与镜像要看两层。** 23 个仓库由 GitHub 标为 fork；此外 `s5cmd` 的 README 自称 fork、`OpenROAD-flow-scripts` 的描述自称只读镜像、`claude-code-base-action` 和 `claude-plugins-community` 也自称镜像，但它们的 API `fork=false`。报告按 API 统计 fork，同时在单项状态中保留 README/描述的真实自述。
6. **README 与 API 描述粒度不同。** `life-sciences` 的 README 描述可安装的 MCP servers 与 skills 市场，API 描述则进一步澄清仓库长期只保留 marketplace 数据、不保存实际 MCP server 源码；表中合并了两条互补信息。
7. **“主要语言”可能反直觉。** 例如 `claude-code` 被 API 统计为 Python、`claude-agent-sdk-typescript` 被统计为 Shell；这是 GitHub 的字节统计结果，不是本文对产品技术栈的推断。
