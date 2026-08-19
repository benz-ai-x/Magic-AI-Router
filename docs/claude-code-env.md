# Claude Code 接入 Suanpan 网关（env 配置）

把 Claude Code 指向本地 Suanpan 网关（`127.0.0.1:9527`），由网关按路由策略或内联覆盖转发到各家供应商。

前提：菜单栏「AI 路由 ▸ 启动路由」已启动网关；供应商已在 `~/.suanpan.yaml` 配好。

## 最小配置

写入 `~/.claude/settings.json` 的 `env` 段（改完重启 Claude Code）：

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9527",
    "ANTHROPIC_AUTH_TOKEN": "suanpan-local",
    "ANTHROPIC_MODEL": "deepseek/deepseek-v4-flash",
    "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek/deepseek-v4-flash"
  }
}
```

- `ANTHROPIC_BASE_URL` — 网关地址。Claude Code 会请求 `{BASE_URL}/v1/messages`。
- `ANTHROPIC_AUTH_TOKEN` — 网关未设 `api_key` 时**任意非空值**即可（无鉴权门）；若网关在 `~/.suanpan.yaml` 设了 `api_key`，这里必须填同一个值。⚠️ 网关暴露到非本机前必须先设 `api_key`。
- `ANTHROPIC_MODEL` / `CLAUDE_CODE_SUBAGENT_MODEL` — 见下「模型写法」。

## 模型写法（两种语义）

**1. `provider/model` —— 内联覆盖（推荐）**

model 字段里带 `/` 即触发网关的内联覆盖（路由优先级最高），精确指定后端，绕过所有场景路由：

```json
"ANTHROPIC_MODEL": "kimi/k3",
"CLAUDE_CODE_SUBAGENT_MODEL": "deepseek/deepseek-v4-flash"
```

主任务用强模型、子代理用便宜模型，各写各的。`.claude/agents/*.md` 里 subagent 的 `model:` 字段同理。

**2. 普通模型名 —— 交给场景路由**

写不带 `/` 的名字（如 `claude-sonnet-4-5`），网关按「路由策略」判定：长上下文 / 后台任务（名字含 haiku）/ 模型规则 / thinking → 命中哪个走哪个，都不命中走默认路由。适合「一套配置到处用」。

**上下文变体**：供应商的变体后缀（如 kimi 的 `k3[1M]` 写法）会随 model 原样透传，例如 `kimi/k3[1M]`。走内联覆盖时用供应商认识的完整写法；走场景路由时路由目标在「路由策略」页选。

## 验证

1. 菜单栏确认「AI 路由」已启动（:9527）。
2. Claude Code 里 `/status` 查看 Base URL 是否为 `127.0.0.1:9527`。
3. 网关 admin 控制台 `http://127.0.0.1:9527/admin/` 看请求是否到达、命中了哪条路由（usage.jsonl 的 scenario 字段：`inline` / `subagent` / `rule` / `default` 等）。

## 常见搭配

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9527",
    "ANTHROPIC_AUTH_TOKEN": "suanpan-local",
    "ANTHROPIC_MODEL": "glm-pro/glm-5.2",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "deepseek/deepseek-v4-flash",
    "CLAUDE_CODE_SUBAGENT_MODEL": "deepseek/deepseek-v4-flash",
    "API_TIMEOUT_MS": "3000000"
  }
}
```

`API_TIMEOUT_MS` 建议调大：长上下文/思考链请求耗时长，网关 `request_timeout_s` 默认 3600s，客户端侧也别太短。

## 自动配置

偏好设置 → 模型规则 → **⚙️ 自动配置 Claude Code** 按钮会自动写入以下环境变量到 `~/.claude/settings.json`：

| 变量 | 值 | 作用 |
|------|-----|------|
| `ANTHROPIC_BASE_URL` | `http://127.0.0.1:9527` | 指向网关 |
| `ANTHROPIC_AUTH_TOKEN` | `mage-router` | 占位 token（网关不校验，自动剥除） |
| `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` | `1` | 阻止 Claude Code 发送供应商不认的 beta 字段（`defer_loading` 等） |

同时移除已有的模型直连变量（`ANTHROPIC_MODEL` / `CLAUDE_CODE_SUBAGENT_MODEL` / `ANTHROPIC_DEFAULT_*_MODEL`），改由网关路由规则接管。原配置备份到 `settings.json.bak`。

> **双重兼容保障**：客户端侧 `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1` 阻止发送 beta 字段；网关侧 `suanpan/compat.py` 对所有经过的请求再做一次 body 标准化（`system` 数组拍平、`document` 块剥除、beta tool 字段剥除）。详见 [`docs/claude-code-compatibility.md`](claude-code-compatibility.md)。

> 说明：当前 `~/.claude/settings.json` 里若已有直连供应商的 `ANTHROPIC_*` 配置（如直连 kimi），两套 env 互斥——改 BASE_URL 前注意备份原值。
