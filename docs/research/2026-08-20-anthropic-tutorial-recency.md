# Anthropic 官方教程与示例仓库：2026 年更新核验

检索日期：2026-08-20（Asia/Shanghai）

## 结论

如果“2026 年之后有更新”理解为“2026-01-01 至检索日，默认分支有新提交”，15 个候选仓库中有 11 个符合；但只有 `claude-cookbooks` 和 `claude-quickstarts` 同时表现出持续维护和实质内容扩充。其余不少仓库虽然在 2026 年新发布或更新，却在 README 中明确标为“不维护”，本质是活动教材、教程快照或参考实现。

优先级建议：

1. 当前 API/Agent 开发主线：[`claude-cookbooks`](https://github.com/anthropics/claude-cookbooks)、[`claude-quickstarts`](https://github.com/anthropics/claude-quickstarts)。
2. Agent SDK 入门与局部示例：[`agent-sdk-workshop`](https://github.com/anthropics/agent-sdk-workshop)、[`claude-agent-sdk-demos`](https://github.com/anthropics/claude-agent-sdk-demos)。
3. 2026 专题参考：`cwc-*`、`code-migration-kit-with-claude-code`、`launch-your-agent`、`defending-code-reference-harness`、`oncall-kit`、`html-effectiveness`；内容新，但多数不是持续维护项目。
4. 不再作为 2026 主线：`courses`、`prompt-eng-interactive-tutorial`、`claude-code-monitoring-guide`；`anthropic-retrieval-demo` 已归档。

## 口径

- 只使用 Anthropic GitHub 组织的仓库元数据、README、默认分支 commit 和 GitHub REST API。
- 提交数限定 `2026-01-01T00:00:00Z` 至 `2026-08-20T23:59:59Z`，包含 merge commit；日期使用 commit 的 committer UTC 日期。
- 不以仓库 `updated_at` 或 `pushed_at` 判定教程更新：Issue、非默认分支 push 等活动也会改变这些字段。例如 `prompt-eng-interactive-tutorial` 的元数据在 2026 年发生变化，但默认分支最新 commit 仍停在 2024 年。
- “实质更新”指新增或改写教程、示例、功能路径或安全/运行架构；链接、拼写、渲染、依赖等单独维护不算实质教程更新。
- “活跃维护”与“2026 新鲜”分开判断。README 明示 `not maintained` 的仓库，即使刚发布也归为教程快照/参考实现。

## 全量核验表

| 仓库 | 默认分支 | 最新默认分支 commit | 2026 commits | 2026 实质更新 | 维护性质 | 适用对象 | 现在是否推荐 |
|---|---|---|---:|---|---|---|---|
| [`courses`](https://github.com/anthropics/courses/blob/master/README.md) | `master` | [2025-11-13 `f4dbb137`](https://github.com/anthropics/courses/commit/f4dbb137d7b02dddaf3cc73e32e20a702d3b5e77) | [0](https://api.github.com/repos/anthropics/courses/commits?sha=master&since=2026-01-01T00%3A00%3A00Z&until=2026-08-20T23%3A59%3A59Z&per_page=100) | 否 | 非归档，但 2026 停更 | API、提示、评测、工具调用初学者 | 条件推荐：只补基础概念，模型与 SDK 示例需对照现行文档 |
| [`prompt-eng-interactive-tutorial`](https://github.com/anthropics/prompt-eng-interactive-tutorial/blob/master/README.md) | `master` | [2024-04-08 `0d277542`](https://github.com/anthropics/prompt-eng-interactive-tutorial/commit/0d277542e927652da25b0014c9b346723af55881) | [0](https://api.github.com/repos/anthropics/prompt-eng-interactive-tutorial/commits?sha=master&since=2026-01-01T00%3A00%3A00Z&until=2026-08-20T23%3A59%3A59Z&per_page=100) | 否 | 实质停更 | 想通过练习掌握提示结构的人 | 不推荐作为现行主线；练习框架仍可借鉴 |
| [`claude-cookbooks`](https://github.com/anthropics/claude-cookbooks/blob/main/README.md) | `main` | [2026-08-14 `35f2eec7`](https://github.com/anthropics/claude-cookbooks/commit/35f2eec7e44897c537e44441b7dff2f0ecbfb804) | 155（[第 1 页](https://api.github.com/repos/anthropics/claude-cookbooks/commits?sha=main&since=2026-01-01T00%3A00%3A00Z&until=2026-08-20T23%3A59%3A59Z&per_page=100&page=1)＋[第 2 页](https://api.github.com/repos/anthropics/claude-cookbooks/commits?sha=main&since=2026-01-01T00%3A00%3A00Z&until=2026-08-20T23%3A59%3A59Z&per_page=100&page=2)） | 是，大量 | **持续活跃维护** | 已有 API 基础、按场景查实现模式的开发者 | **强推荐** |
| [`claude-quickstarts`](https://github.com/anthropics/claude-quickstarts/blob/main/README.md) | `main` | [2026-08-19 `1b83e0f9`](https://github.com/anthropics/claude-quickstarts/commit/1b83e0f975499949186edaa64e0e9ceb34ec7453) | [10](https://api.github.com/repos/anthropics/claude-quickstarts/commits?sha=main&since=2026-01-01T00%3A00%3A00Z&until=2026-08-20T23%3A59%3A59Z&per_page=100) | 是，多数为完整新 quickstart | **持续活跃维护** | 想直接运行、部署和改造完整应用的开发者 | **强推荐** |
| [`agent-sdk-workshop`](https://github.com/anthropics/agent-sdk-workshop/blob/main/README.md) | `main` | [2026-03-04 `a273fbe2`](https://github.com/anthropics/agent-sdk-workshop/commit/a273fbe2b3776d84cf29f025927d71dcc0982c9f) | [1](https://api.github.com/repos/anthropics/agent-sdk-workshop/commits?sha=main&since=2026-01-01T00%3A00%3A00Z&until=2026-08-20T23%3A59%3A59Z&per_page=100) | 是，2026 新发布 | 单次课程快照，尚无持续维护证据 | 不想先写大量代码、要学习 tools/subagents/hooks/memory 的 Agent 初学者 | 推荐入门；API 细节需再核对 SDK 文档 |
| [`claude-agent-sdk-demos`](https://github.com/anthropics/claude-agent-sdk-demos/blob/main/README.md) | `main` | [2026-03-13 `826b2685`](https://github.com/anthropics/claude-agent-sdk-demos/commit/826b268506a5f3707623c9e6140b200befcbebae) | [14](https://api.github.com/repos/anthropics/claude-agent-sdk-demos/commits?sha=main&since=2026-01-01T00%3A00%3A00Z&until=2026-08-20T23%3A59%3A59Z&per_page=100) | 是 | 间歇更新；README 明示仅供本地演示、不可上生产 | 想看完整 Agent SDK 小应用的开发者 | 推荐学习，**不推荐直接部署** |
| [`cwc-workshops`](https://github.com/anthropics/cwc-workshops/blob/main/README.md) | `main` | [2026-06-26 `059482de`](https://github.com/anthropics/cwc-workshops/commit/059482de4cd4f20aa9771f1d1424c870a65eccbf) | [28](https://api.github.com/repos/anthropics/cwc-workshops/commits?sha=main&since=2026-01-01T00%3A00%3A00Z&until=2026-08-20T23%3A59%3A59Z&per_page=100) | 是，大量新 workshop | **活动教材快照；明确不维护** | 想学 Managed Agents、多 Agent、评测驱动开发的人 | 强烈建议选读，但不要假定示例会跟进 API 变化 |
| [`cwc-long-running-agents`](https://github.com/anthropics/cwc-long-running-agents/blob/main/README.md) | `main` | [2026-05-13 `ad107a97`](https://github.com/anthropics/cwc-long-running-agents/commit/ad107a974bced5244f74dd283dbf2bfd3baee3a1) | [3](https://api.github.com/repos/anthropics/cwc-long-running-agents/commits?sha=main&since=2026-01-01T00%3A00%3A00Z&until=2026-08-20T23%3A59%3A59Z&per_page=100) | 是，2026 新发布并补一轮内容 | **活动教材快照；明确不维护** | 研究 `/goal`、evaluator、handoff、hooks 长任务 harness 的进阶用户 | 推荐作模式参考，不是 turnkey harness |
| [`code-migration-kit-with-claude-code`](https://github.com/anthropics/code-migration-kit-with-claude-code/blob/main/README.md) | `main` | [2026-07-08 `cf91c9d5`](https://github.com/anthropics/code-migration-kit-with-claude-code/commit/cf91c9d5068d9aaf95a36164169f08c3e636c909) | [1](https://api.github.com/repos/anthropics/code-migration-kit-with-claude-code/commits?sha=main&since=2026-01-01T00%3A00%3A00Z&until=2026-08-20T23%3A59%3A59Z&per_page=100) | 是，2026 新发布 | **参考代码；明确不主动维护** | 做大规模、结构保持型语言迁移的团队 | 专题强推荐；重设计型迁移不能照搬 |
| [`claude-code-monitoring-guide`](https://github.com/anthropics/claude-code-monitoring-guide/blob/main/README.md) | `main` | [2025-07-29 `02777441`](https://github.com/anthropics/claude-code-monitoring-guide/commit/02777441f2a3fa38a187b57872ca9dc5e0411b48) | [0](https://api.github.com/repos/anthropics/claude-code-monitoring-guide/commits?sha=main&since=2026-01-01T00%3A00%3A00Z&until=2026-08-20T23%3A59%3A59Z&per_page=100) | 否 | 2025 单次导入，未继续维护 | 做 Claude Code 遥测、成本与 ROI 分析的个人或团队 | 仅推荐指标框架；配置、字段和价格必须重新核验 |
| [`launch-your-agent`](https://github.com/anthropics/launch-your-agent/blob/main/README.md) | `main` | [2026-07-07 `c9e0f137`](https://github.com/anthropics/launch-your-agent/commit/c9e0f1378a252bd42deb7e9eb02ac0cbd07160bc) | [5](https://api.github.com/repos/anthropics/launch-your-agent/commits?sha=main&since=2026-01-01T00%3A00%3A00Z&until=2026-08-20T23%3A59%3A59Z&per_page=100) | 是，2026 新发布并加固 | **教育性 Skill 快照；明确不维护** | 想从需求访谈走到 Managed Agent 发布、评测和调度的技术创始人 | 推荐体验 CMA 全流程；注意 token 成本和 API 漂移 |
| [`defending-code-reference-harness`](https://github.com/anthropics/defending-code-reference-harness/blob/main/README.md) | `main` | [2026-08-06 `d3bea6b5`](https://github.com/anthropics/defending-code-reference-harness/commit/d3bea6b5793b5f3d59a75ebe69a58efa88383145) | [29](https://api.github.com/repos/anthropics/defending-code-reference-harness/commits?sha=main&since=2026-01-01T00%3A00%3A00Z&until=2026-08-20T23%3A59%3A59Z&per_page=100) | 是，大量 | **新且经过多轮加固，但 README 明确不维护** | 构建漏洞发现、验证、分流、修补流水线的安全团队 | 专题强推荐；必须按文档做 sandbox，不能当通用产品 |
| [`oncall-kit`](https://github.com/anthropics/oncall-kit/blob/main/README.md) | `main` | [2026-08-06 `c03282cd`](https://github.com/anthropics/oncall-kit/commit/c03282cd5381a5a2e12e32bfb3c8957d52ce01f1) | [1](https://api.github.com/repos/anthropics/oncall-kit/commits?sha=main&since=2026-01-01T00%3A00%3A00Z&until=2026-08-20T23%3A59%3A59Z&per_page=100) | 是，2026 v1.0 新发布 | **参考实现；明确不维护** | 想让 Claude 做只读事故调查、交接和经验沉淀的 SRE 团队 | 推荐作流程蓝本；保留所有人类决策闸门 |
| [`html-effectiveness`](https://github.com/anthropics/html-effectiveness/blob/main/README.md) | `main` | [2026-05-15 `58c305be`](https://github.com/anthropics/html-effectiveness/commit/58c305be97f47b26b678f2c07dec01d4242268ec) | [8](https://api.github.com/repos/anthropics/html-effectiveness/commits?sha=main&since=2026-01-01T00%3A00%3A00Z&until=2026-08-20T23%3A59%3A59Z&per_page=100) | 是，2026 新发布并扩展示例 | 新案例集，暂无持续更新证据 | 想用单文件 HTML 做审查、原型、报告和交互工具的人 | 推荐作灵感与可复制样例，不是系统课程 |
| [`anthropic-retrieval-demo`](https://github.com/anthropics/anthropic-retrieval-demo/blob/main/README.md) | `main` | [2023-09-09 `a032154b`](https://github.com/anthropics/anthropic-retrieval-demo/commit/a032154b93fdb82304040115468a2e6fe89ae00a) | [0](https://api.github.com/repos/anthropics/anthropic-retrieval-demo/commits?sha=main&since=2026-01-01T00%3A00%3A00Z&until=2026-08-20T23%3A59%3A59Z&per_page=100) | 否 | **已归档**，仅 2023 初始提交 | 研究早期 agentic retrieval 历史思路的人 | 不推荐用于新项目 |

## 2026 年实质更新主题

### 持续维护的主线仓库

#### `claude-cookbooks`

155 条提交不只是格式维护。代表性内容包括全库模型引用升级至 Claude 4.6（[`944b94a0`](https://github.com/anthropics/claude-cookbooks/commit/944b94a0ebc6025e89aaf90136e120a72068b077)）、Opus 4.6 server-side compaction（[`7cb72a9c`](https://github.com/anthropics/claude-cookbooks/commit/7cb72a9c879e3b95f58d30a3d7483906e9ad548e)）、automatic prompt caching（[`419ce35f`](https://github.com/anthropics/claude-cookbooks/commit/419ce35fa8457347ff8bf61bffed791f8617b8c1)）、context engineering（[`ee3dfe1e`](https://github.com/anthropics/claude-cookbooks/commit/ee3dfe1e245e90ed0fee452b1a8c9ab9e33927f4)）、Managed Agents 系列（[`fa27d432`](https://github.com/anthropics/claude-cookbooks/commit/fa27d432a1899b26648a10244ff90ba2445d463b)）及 Agent SDK 多 Agent/部署配方（[`e22e6830`](https://github.com/anthropics/claude-cookbooks/commit/e22e683065954c07fae8bc4bc5fccf34d6595297)、[`2ec3c494`](https://github.com/anthropics/claude-cookbooks/commit/2ec3c494e3ebc2ebcde5f9f243fbffd71f33b199)）。最新几条是 MDX/渲染修复，但不能据此误判全年仅做维护。

#### `claude-quickstarts`

10 条提交数量不大，但主要是完整应用：Computer Use 最佳实践（[`b03d42cc`](https://github.com/anthropics/claude-quickstarts/commit/b03d42cc109ef2a61c65305ac2fb8b293bbdac71)）、Managed Agents + Vercel Chat SDK（[`370e18d4`](https://github.com/anthropics/claude-quickstarts/commit/370e18d4a20ff5fd4bc1a6bf11d5105b2383977c)）、CopilotKit/AG-UI（[`bda1ad49`](https://github.com/anthropics/claude-quickstarts/commit/bda1ad4991e3990b39cf660addad68be95c666ff)）、assistant-ui（[`8a49be4c`](https://github.com/anthropics/claude-quickstarts/commit/8a49be4c995d45a09d9a1ad197e35c0727f1ffd8)）以及 Docker 自托管 sandbox（[`1b83e0f9`](https://github.com/anthropics/claude-quickstarts/commit/1b83e0f975499949186edaa64e0e9ceb34ec7453)）。

### Agent SDK 与 Code with Claude 教材

- `agent-sdk-workshop` 只有一条初始提交，但仓库本身就是 2026 新课程：四阶段引导 demo，加 tools、subagents、hooks/memory，以及六类 breakout。它是“新发布”，不是“持续更新”。
- `claude-agent-sdk-demos` 的 14 条提交包含 resume generator（[`b54ab972`](https://github.com/anthropics/claude-agent-sdk-demos/commit/b54ab972)）、V2 Session API 修正（[`c48f7db7`](https://github.com/anthropics/claude-agent-sdk-demos/commit/c48f7db7)）和 AskUserQuestion HTML previews（[`7e1930ff`](https://github.com/anthropics/claude-agent-sdk-demos/commit/7e1930ff62f4a02382cae9b969cb02496233106e)），兼有链接维护，属于实质更新。
- `cwc-workshops` 从初始活动教材（[`65e36d3b`](https://github.com/anthropics/cwc-workshops/commit/65e36d3b)）扩展到 Deal Desk、评测驱动 Agent、Agent Battle 和 SEC filings Research Desk（[`059482de`](https://github.com/anthropics/cwc-workshops/commit/059482de4cd4f20aa9771f1d1424c870a65eccbf)）。内容很新，但 README 明说 `Not maintained`。
- `cwc-long-running-agents` 初始发布 harness primitives 后，增加 `/goal` 与自定义 loop 的对比和运行章节（[`06e68234`](https://github.com/anthropics/cwc-long-running-agents/commit/06e682341524116c00462c0c80b15d82133fbfb7)）。它明确是可阅读、可摘取的 ingredients，而非成品 harness。

### 2026 专题快照与参考实现

- `code-migration-kit-with-claude-code` 以一条提交完整发布六阶段迁移流程、依赖图、规则书、并行翻译队列和行为一致性裁判。README 明示参考代码且不主动维护。
- `launch-your-agent` 在初始导入后做过 overview token 效率和 Skill 加固（[`8b4d6f47`](https://github.com/anthropics/launch-your-agent/commit/8b4d6f472d66b0ae76962c81a66d719e00feea9c)），适合体验 Managed Agent 全生命周期；README 同时说明它是教育性、token 开销更高且不维护的 Skill。
- `defending-code-reference-harness` 是专题仓库中更新最充分的：增加 detection & response track（[`972aa15c`](https://github.com/anthropics/defending-code-reference-harness/commit/972aa15c)）、Bedrock/Vertex provider 支持（[`4c43dc0d`](https://github.com/anthropics/defending-code-reference-harness/commit/4c43dc0d)）、不可信数据隔离（[`71827ce2`](https://github.com/anthropics/defending-code-reference-harness/commit/71827ce2)）、容器 fail-fast 和 Bedrock 启动加固。它很新，却仍明确声明不维护；生产采用者必须自己拥有维护能力。
- `oncall-kit` 是单次 v1.0 快照，重点是证据可追溯、shadow period、holdout 验证和“Claude 调查/建议，人类决定/部署”的硬边界。
- `html-effectiveness` 先发布案例集，再增加流程图、研究/学习页面（[`592a7710`](https://github.com/anthropics/html-effectiveness/commit/592a7710)）、三种自定义编辑界面（[`5d64f689`](https://github.com/anthropics/html-effectiveness/commit/5d64f6891b8eacb1b6b2ff281a258d0359385724)）和项目文档。它是案例画廊，不是 Claude API 课程。

## 没有 2026 更新的仓库

- `courses`：README 仍以 Claude 3 Haiku 等旧课程设置为主。课程结构有价值，但不应单独承担现行 API 教学。
- `prompt-eng-interactive-tutorial`：最后提交只是 2024 年给 README 增加 Google Sheets 链接；模型示例仍是 Claude 3 系列。
- `claude-code-monitoring-guide`：只有 2025 年从个人仓库导入的一次提交。ROI 指标框架仍可用，但 Prometheus/OpenTelemetry 字段、套餐和价格具有时效性。
- `anthropic-retrieval-demo`：已归档，README 标为 experimental，代码仍使用 `claude-2.0` 和旧 completion 风格；仅适合历史研究。

## 实用选择

- 想学最新 Claude API 能力：先看 `claude-cookbooks`，再以官方在线文档校验接口。
- 想直接跑起来：选 `claude-quickstarts`。
- 第一次用 Agent SDK：先做 `agent-sdk-workshop`，再按需求查看 `claude-agent-sdk-demos`；后者只限本地学习。
- 想研究 Claude Code 长任务和大规模迁移：选 `cwc-long-running-agents` 与 `code-migration-kit-with-claude-code`，把它们当设计模式，不当稳定依赖。
- 做 Managed Agents：以 `claude-quickstarts`/`claude-cookbooks` 为可持续入口，`cwc-workshops` 和 `launch-your-agent` 用来快速理解完整工作流。
- 做安全或 SRE：`defending-code-reference-harness`、`oncall-kit` 很新，但都是自维护参考实现，不能省略 sandbox、人类审批和本地验证。
