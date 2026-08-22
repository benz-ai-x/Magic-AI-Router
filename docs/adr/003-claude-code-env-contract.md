# ADR-003: Claude Code 环境变量契约

- 状态：Accepted
- 日期：2026-08-14
- 决策者：tech-lead（用户确认）
- 原编号：ADR-024（2026-08-19 仓库重建后重编号压缩间隙）
- 影响范围：claude_code_setup.py、config_server.py、config_ui.html

## 上下文

`claude_code_setup.py` 把 Claude Code 指向 Suanpan 网关并写入一组模型映射环境变量。整套「角色 → 环境变量」行为契约此前只在代码里，CONTEXT.md / CLAUDE.md / ADR 均无记载（ADR-002 只规定了 `claude_settings` PATHS 条目与原子写契约，未规定写什么）。#42/#43/#44 又相继钉死了推导语义、subagent 意图与元数据载荷形状——若不落文档，未来架构审查会重新争议这些已决问题。

## 决策

### 决策 1：写入 `~/.claude/settings.json` 的 `env` 变量集

| 环境变量 | 语义 | 来源角色 |
|---|---|---|
| `ANTHROPIC_BASE_URL` | 网关地址 `http://<suanpan_listen>` | 固定 |
| `ANTHROPIC_AUTH_TOKEN` | ~~占位 token `mage-router`~~ 本地客户端 token（2026-08-21 issue #9 增补取代，见文末「增补」节） | 固定 |
| `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` | `"1"`（禁用实验 beta，兼容供应商 API） | 固定 |
| `ANTHROPIC_MODEL` | 默认兜底模型 | `default` 角色 |
| `ANTHROPIC_DEFAULT_OPUS_MODEL` / `ANTHROPIC_DEFAULT_SONNET_MODEL` / `ANTHROPIC_DEFAULT_FABLE_MODEL` / `ANTHROPIC_DEFAULT_HAIKU_MODEL` | 各 tier 模型 | `opus`/`sonnet`/`fable`/`haiku` 角色 |
| 上述四个 tier 的 `*_MODEL_NAME` | `/model` 菜单显示名（自定义 name，缺省=模型） | 同角色 |
| `CLAUDE_CODE_SUBAGENT_MODEL` | 子代理模型 | `subagent` 角色（无 `_NAME` 变体） |

- **`[1M]` 后缀**：模型值末尾可加 `[1M]`，由角色的 `ctx_1m` 布尔开关控制（#44 由 `one_m` 更名而来，旧键在 `_roles_to_env` 保留只读 fallback）。1M 只是给 Claude Code 的上下文能力声明，不改路由。
- **wipe-set 派生自 `_ROLES`**：写入前只删除本模块拥有的模型键，永不动用户自己设的其他 `ANTHROPIC_DEFAULT_*_MODEL`。
- **幂等**：BASE_URL / beta 开关 / 全部模型映射均已是目标值时返回 `action: "already"`，不重复写。
- **写入路径**：经 `config_store.atomic_write`（0600 + `.bak` 备份），见 ADR-002。

### 决策 2：角色推导语义（`default_roles()`，种子来源）

- 路由规则 → tier 角色映射按 **suanpan/router.py 同款首击前缀语义**（#42）：tier 前缀 T 命中规则顺序中第一条 `T.startswith(match_prefix)` 或 `match_prefix.startswith(T)`；未命中回落到 `router.default`。
- **subagent = haiku tier 目标**（#43，CONTEXT.md「子代理用便宜模型」设计意图）；无 haiku 规则时回落 default。
- `default` 角色 = `router.default`。

### 决策 3：UI 种子载荷（`GET /api/cc-default-roles`）

返回 `{roles, order, labels, readonly}`——`order`/`labels`/`readonly` 派生自 Python `_ROLES`（Python 是角色集单一真源，#44）；`default` 由独立控件渲染，不入 `order`。JS 端不再平行编码角色清单。

## 否决

- **URL query string 传 bearer token**：token 只在 `Authorization: Bearer` 头（#39，常量时间比较）。
- **掩码字符串回传 UI**：`api_key_set` 布尔契约，见 ADR-002。
- **subagent 抄 `router.default`**：丢掉「子代理用便宜模型」区分（#43 已否决并修）。

## 测试

`tests/test_docs_drift.py::TestClaudeCodeEnvContractDocumented` 交叉校验：`_ROLES` 产出的每个 env 变量名与契约术语（`ctx_1m`/`[1M]`/BASE_URL 等）都必须出现在本 ADR——文档与代码双向锁死。

## 增补（2026-08-20，issue #10）：设置窗 header-only 落地

- query-string 认证路径删除；无凭证的 `/` 与 `/api/*` 一律 401。
- 桥接（webview_window `auth_headers`）构造带 Authorization 头的首导航请求；服务端随该响应种下 `cfgsess` HttpOnly SameSite=Strict 会话 cookie，刷新与后续同源 fetch 由 cookie 承载——token 不进 URL/JS/日志。
- `ConfigServer.auth_url` 删除；`url` 恒为无凭证 loopback 地址。常量时间比较、loopback bind、Host allowlist 全部保留。

## 增补（2026-08-21，issue #9，决策 A×4）：本地客户端 token

- **Token 来源**：每安装实例专用随机 token（`mpconf/local_token.py`，
  `secrets.token_hex(16)`，无业务含义），替换历史 `mage-router` 占位。
- **存储**：`~/.magic-proxy.json` 的 `local_client_token` 字段（经
  `config_store.atomic_write` 0600 + 原子替换）；明文永不回显于
  UI/日志/preview diff（`claude_code_setup._mask_old` 掩码）。
- **轮换**：设计上单活（任意时刻一个有效值）；轮换入口随 #49 删除
  （从未接线，重装/手改字段即轮换）。
- **认证关闭语义**：顶层 `api_key` 空 = 网关不校验本地客户端身份，
  但**所有出站路径**（含 keyless Provider、count_tokens）**无条件剥除**
  一切入站 Authorization/x-api-key——`mage-router`、本地 token、用户真实
  值都绝不透传上游。
- **被拒**：复用顶层 api_key（复制用户 secret、轮换与多客户端语义弱）；
  认证关闭时透传（无安全边界）。
