# Magic Stack — 菜单栏里的 AI 网络栈：把 Claude Code 路由到任意大模型

**本地优先的 AI 网络栈：讲 Anthropic / OpenAI 协议的大模型网关 + 你自己的 SSH 隧道 + TLS 层 AI 流量抓包，装进一个原生 macOS 应用。**

[English](README.md) · [简体中文](README.zh-CN.md)

[![Release](https://img.shields.io/github/v/release/benz-ai-x/magic-stack)](../../releases)
[![Downloads](https://img.shields.io/github/downloads/benz-ai-x/magic-stack/total)](../../releases)
[![Stars](https://img.shields.io/github/stars/benz-ai-x/magic-stack?style=social)](../../stargazers)
![Platform](https://img.shields.io/badge/platform-macOS%20%28Apple%20Silicon%29-blue)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
[![Tests](https://img.shields.io/badge/tests-2000%2B%20%C2%B7%20ADRs%20%C2%B7%20zero%20telemetry-brightgreen)](CONTEXT.md)

![Magic Stack macOS 菜单栏——AI 路由网关、SSH SOCKS5 代理、端口转发](assets/docs/menu-bar-v091.png)

## 它解决什么痛点

| 痛点 | 用 Magic Stack |
|---|---|
| **Claude Code 被锁死在一家供应商、一张账单上** | 设一次 `ANTHROPIC_BASE_URL`，按模型前缀规则路由到 **GLM / DeepSeek / Kimi / Qwen / OpenAI / Anthropic**——工作流不变，后端更便宜或更快 |
| **每个 Agent 各配各的 Key，散落各处** | **一个 Key 配好全部 Agent**：内置引擎自动配置 Claude Code、Codex、OpenCode、ZCode，它们只持有本地网关凭证 |
| **看不见 Agent 发了什么、花了多少** | TLS 抓包解密 AI 调用（OpenAI / Anthropic / DeepSeek / 豆包 / Qwen / MiniMax）落可读 JSONL，分供应商用量、缓存命中率与余额一屏看清 |
| **流量要走自己的服务器** | SSH SOCKS5 代理 + 多隧道并行 `ssh -L` 端口转发 + NFSv4 挂载，全部菜单栏一键启停 |

密钥永不出你的机器。零遥测。MIT。

## 60 秒把 Claude Code 接到 GLM

```bash
# 1. 应用 → 偏好设置 → AI 路由 → 供应商
#    添加 GLM  https://open.bigmodel.cn/api/anthropic  + 你的 API Key
#    设 router.default = GLM/glm-5.2
# 2. 把 Claude Code 指向网关：
export ANTHROPIC_BASE_URL=http://127.0.0.1:9527
claude   # 此后每个请求都按你的规则路由
```

按档位混搭模型——主线程用强模型，子代理用便宜模型：

```yaml
rules:
  - match_prefix: claude-opus     # 重活
    route_to: GLM/glm-5.2
  - match_prefix: claude-sonnet   # 日常
    route_to: DeepSeek/deepseek-v4-pro
  - match_prefix: claude-haiku    # 子代理 → 便宜
    route_to: KIMI/k3
```

## 一个应用，三层闭环

| 层 | 你得到什么 |
|---|---|
| 🧮 **路由** — 大模型网关（`:9527`） | 三协议入站（Anthropic Messages / OpenAI Chat / Responses），前缀路由，同协议直通、失配自动转换（含 SSE 流式），提示词缓存标记保留，用量与余额统计 |
| 🔗 **接入** — SSH 隧道（`:8888`） | 经你自己服务器的 SOCKS5 代理、多隧道并行 `-L` 转发，密钥或 Keychain 密码认证，自动重试与唤醒重连，一台服务器一页配完（连接 / 转发 / NFS 挂载 / 服务检测） |
| 🔍 **验证** — AI 流量抓包 | 内置 mitmdump 级联进代理；只记录已知 AI API 到 JSONL，其余流量原样放行 |

**加一项——Agent 可自助操作：**「复制 AI 助手指令」交给 Claude Code 一个带 token 守卫的本地 API，它自己就能把应用配好。AI 网络工具，AI 自己也会用。

![设置——服务器视图：端口映射 tab](assets/docs/settings-servers-v0130.png)

![设置——服务器视图：NFS 挂载 tab](assets/docs/settings-nfs-v0130.png)

## 安装

**macOS（推荐）：** 从 [Releases](../../releases) 下载签名 + 公证的 `.dmg`，拖进 `Applications`，菜单栏出现 ⚫。

**从源码运行：**

```bash
git clone https://github.com/benz-ai-x/magic-stack.git && cd magic-stack
pip3 install -r requirements-dev.txt && python3 app.py
```

**Linux / 无头——Docker（网关 + Web 配置）：**

```bash
bash docker/suanpan.sh up    # 网关 :9527 + Web 配置 :9528
bash docker/suanpan.sh sync  # 写入 ~/.claude/settings.json
```

## 横向对比

| | **Magic Stack** | claude-code-router / LiteLLM | OpenRouter（SaaS） | 手动 ssh + 配置 |
|---|---|---|---|---|
| 运行位置 | **本地优先**（菜单栏 / Docker） | 本地或自托管 | 对方云端 | 你的终端 |
| 密钥与提示词 | **永不出本机** | 自持 | 发给服务方 | 自持 |
| 看得见真实 AI 流量（TLS 明文） | ✅ 内置 | ❌ | ❌ | 需自配 mitmproxy |
| SSH 接入层（SOCKS5 + `-L` 并行） | ✅ 内置 | ❌ | ❌ | ✅ 但全手动 |
| 分规则的成本 / 缓存 / 用量闭环 | ✅ 同一应用 | 部分 | 仪表盘 | ❌ |

## 常见问题

**Claude Code 真的能用 DeepSeek / GLM / Kimi 吗？**
能——网关完全兼容 Anthropic Messages 协议（SSE 流式、工具调用、提示词缓存标记原样保留）。只改 `ANTHROPIC_BASE_URL`，不打补丁、不玩 hack。

**和 claude-code-router、LiteLLM、OpenRouter 有什么区别？**
路由器只转发看不见流量，SaaS 拿着你的密钥。Magic Stack 本地优先**且**闭环：改一条规则，同一应用里就能看到真实的成本、延迟与返回变化。

**密钥放在哪？免费吗？**
MIT 协议；路由到的是*你自己的*供应商账户。密钥存 `~/.suanpan.yaml`（`0600`），所有界面掩码显示。零遥测，全部端口只绑回环。

**支持 Codex / OpenCode / ZCode 吗？**
支持——三协议入站；快速接入向导填一个 Key，勾选的 Agent 全部配好。

**Linux / Windows 呢？**
Suanpan 网关有 Docker 镜像；菜单栏外壳、SSH 隧道与抓包仅 macOS。

## 信任与工程

- SSH 密码只存 macOS **Keychain**、经管道喂给 `ssh`——绝不进 `argv`/`ps`；`StrictHostKeyChecking=yes` + 专用 `known_hosts`
- 常量时间密钥比较；带凭证的出站请求拒绝跨源重定向与降级
- 配置原子写入（`0600`）+ 崩溃恢复日志
- **2000+ 测试**、10 篇 ADR（[`docs/adr/`](docs/adr/)）、防漂移领域词汇表（[`CONTEXT.md`](CONTEXT.md)）

## 给清单维护者（一句话）

> **Magic Stack** — 开源 macOS 菜单栏应用：把 Claude Code、Codex 等 Agent（Anthropic/OpenAI 协议）经本地优先网关路由到 GLM/DeepSeek/Kimi/Qwen/OpenAI，内置 SSH 隧道/端口转发管理与 TLS 层 AI 流量观测。MIT。

[`CHANGELOG.md`](CHANGELOG.md) · [`docs/docker-deploy.md`](docs/docker-deploy.md) · [`docs/adr/`](docs/adr/) · [`CONTEXT.md`](CONTEXT.md)

## 许可

[MIT](LICENSE) — Copyright (c) 2026 benz-ai-x

---

<div align="center">

**路由它。隧道它。看见它。**

⭐ Star 跟随版本 · [讨论区](../../discussions) · [问题反馈](../../issues)

</div>
