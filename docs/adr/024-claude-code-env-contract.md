# ADR-024: Claude Code 环境变量契约

- 状态：Accepted
- 日期：2026-08-14
- 决策者：tech-lead（用户确认）
- 影响范围：claude_code_setup.py、config_server.py、config_ui.html

## 上下文

`claude_code_setup.py` 把 Claude Code 指向 Suanpan 网关并写入一组模型映射环境变量。整套「角色 → 环境变量」行为契约此前只在代码里，CONTEXT.md / CLAUDE.md / ADR 均无记载（ADR-023 只规定了 `claude_settings` PATHS 条目与原子写契约，未规定写什么）。#42/#43/#44 又相继钉死了推导语义、subagent 意图与元数据载荷形状——若不落文档，未来架构审查会重新争议这些已决问题。

## 决策

### 决策 1：写入 `~/.claude/settings.json` 的 `env` 变量集

| 环境变量 | 语义 | 来源角色 |
|---|---|---|
| `ANTHROPIC_BASE_URL` | 网关地址 `http://<suanpan_listen>` | 固定 |
| `ANTHROPIC_AUTH_TOKEN` | 占位 token `mage-router` | 固定 |
| `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` | `"1"`（禁用实验 beta，兼容供应商 API） | 固定 |
| `ANTHROPIC_MODEL` | 默认兜底模型 | `default` 角色 |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` / `ANTHROPIC_DEFAULT_SONNET_MODEL` / `ANTHROPIC_DEFAULT_FABLE_MODEL` / `ANTHROPIC_DEFAULT_HAIKU_MODEL` | 各 tier 模型 | `opus`/`sonnet`/`fable`/`haiku` 角色 |
| 上述四个 tier 的 `*_MODEL_NAME` | `/model` 菜单显示名（自定义 name，缺省=模型） | 同角色 |
| `CLAUDE_CODE_SUBAGENT_MODEL` | 子代理模型 | `subagent` 角色（无 `_NAME` 变体） |

- **`[1M]` 后缀**：模型值末尾可加 `[1M]`，由角色的 `ctx_1m` 布尔开关控制（#44 由 `one_m` 更名而来，旧键在 `_roles_to_env` 保留只读 fallback）。1M 只是给 Claude Code 的上下文能力声明，不改路由。
- **wipe-set 派生自 `_ROLES`**：写入前只删除本模块拥有的模型键，永不动用户自己设的其他 `ANTHROPIC_DEFAULT_*_MODEL`。
- **幂等**：BASE_URL / beta 开关 / 全部模型映射均已是目标值时返回 `action: "already"`，不重复写。
- **写入路径**：经 `config_store.atomic_write`（0600 + `.bak` 备份），见 ADR-023。

### 决策 2：角色推导语义（`default_roles()`，种子来源）

- 路由规则 → tier 角色映射按 **suanpan/router.py 同款首击前缀语义**（#42）：tier 前缀 T 命中规则顺序中第一条 `T.startswith(match_prefix)` 或 `match_prefix.startswith(T)`；未命中回落到 `router.default`。
- **subagent = haiku tier 目标**（#43，CONTEXT.md「子代理用便宜模型」设计意图）；无 haiku 规则时回落 default。
- `default` 角色 = `router.default`。

### 决策 3：UI 种子载荷（`GET /api/cc-default-roles`）

返回 `{roles, order, labels, readonly}`——`order`/`labels`/`readonly` 派生自 Python `_ROLES`（Python 是角色集单一真源，#44）；`default` 由独立控件渲染，不入 `order`。JS 端不再平行编码角色清单。

## 否决

- **URL query string 传 bearer token**：token 只在 `Authorization: Bearer` 头（#39，常量时间比较）。
- **掩码字符串回传 UI**：`api_key_set` 布尔契约，见 ADR-023。
- **subagent 抄 `router.default`**：丢掉「子代理用便宜模型」区分（#43 已否决并修）。

## 测试

`tests/test_docs_drift.py::TestClaudeCodeEnvContractDocumented` 交叉校验：`_ROLES` 产出的每个 env 变量名与契约术语（`ctx_1m`/`[1M]`/BASE_URL 等）都必须出现在本 ADR——文档与代码双向锁死。
