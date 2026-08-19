# Magic AI Router

**住在你菜单栏里的网络指挥官。** 一条 SSH 隧道、一个 TLS 抓包器、一个 AI 请求路由网关——收进一枚状态图标，点一下就全在手心。

面向 Apple Silicon（arm64）Mac · 原生 `.app` · 经 Apple 公证，双击即装

---

## 为什么是 Magic AI Router

你不需要三个工具、五个终端、一堆配置文件。你只需要菜单栏上那一枚 ⚫。

- **一条隧道，全局畅通。** 浏览器走 HTTP，底层自动换成 SOCKS5，穿过 SSH 隧道抵达远端——你的流量从此有了自己的专用车道。
- **看见 AI 在说什么。** TLS 抓包把 Claude / GPT 类请求解密落成 JSONL，每一次调用、每一段响应，白纸黑字。
- **一个入口，多家大模型。** Suanpan 路由网关把 Claude Code 的请求按规则分发到 DeepSeek / GLM / Kimi…… 换模型不改代码，点一下的事。

而这一切，**常驻菜单栏，不占 Dock，不吵不闹**。

---

## 它怎么工作

```
浏览器 ──HTTP:8888──▶ SOCKS5:1080 ──SSH 隧道──▶ 远端服务器
```

AI 请求可选经 Suanpan 网关智能分发：

```
Claude Code ──POST :9527/v1/messages──▶ Suanpan 路由 ──▶ DeepSeek / GLM / Kimi …
```

---

## 三分钟上手

### 方式一：下载安装（推荐给大多数人）

1. 从 [Releases](../../releases) 下载最新 **`.dmg`**
2. 拖进 `Applications`——已公证，Gatekeeper 不会拦
3. 启动，菜单栏出现 ⚫

### 方式二：从源码跑

```bash
git clone git@github.com:benz-ai-x/Magic-AI-Router.git
cd Magic-AI-Router
pip3 install -r requirements-dev.txt
python3 app.py
```

### 方式三：自己打包 .app

```bash
git clone git@github.com:benz-ai-x/Magic-AI-Router.git
cd Magic-AI-Router
bash build.sh
cp -R "dist/Magic AI Router.app" /Applications/
```

---

## 用起来的样子

1. 启动应用（菜单栏出现 ⚫ 图标）
2. 点菜单「**偏好设置…**」打开原生配置面板
3. 「代理 → 隧道」填入 SSH 信息（密钥 / 密码都行）
4. 点菜单「**重新连接**」
5. 浏览器 HTTP 代理指向 `127.0.0.1:8888`——**通了**

**密码认证？** 密码进 macOS 钥匙串，绝不写进配置文件。只需装个 `sshpass`：

```bash
brew install hudochenkov/sshpass/sshpass
```

**多条隧道？** 配多少条都行，菜单「代理隧道」一键切换。首次连新服务器会亮出 SSH 指纹请你过目，确认后固定保存——中间人想混进来？没门。

---

## 三件趁手兵器

### 🔍 抓包模式

菜单「抓包」一开，浏览器经 mitmproxy 解密，AI API 的请求/响应实时落进 `~/.magic-proxy-captures/<日期>.jsonl`。第一次用，在钥匙串里信任一下本地根 CA 即可。

> 你的 AI 到底发了什么、花了多少 token、模型回了什么——不再是黑盒。

### 🧮 AI 路由（Suanpan 算盘）

菜单「AI 路由 → 启动路由」拉起网关（`:9527`），Claude Code 设一行环境变量就接入：

```bash
export ANTHROPIC_BASE_URL=http://127.0.0.1:9527
```

在「偏好设置 → AI 路由」里排兵布阵：

- **供应商** — DeepSeek / GLM / Kimi……（兼容 Anthropic Messages 协议即可接入）
- **模型规则** — 按模型名前缀匹配转发：`claude-sonnet → deepseek/v4-flash`，精准分流
- **运行统计** — 缓存命中率、每日趋势、供应商与路由来源分组，实时可见
- **余额速览** — 各家余额与套餐配额集中查看

> 今天用 GLM 写代码，明天切 Kimi 跑长文——**改的是规则，不是你的工作流。**

---

## 🤖 让 AI Agent 帮你配置

Magic AI Router 内置了 AI Agent 可读的产品上下文（`agent.md`）。应用运行时，打开偏好设置面板，点侧边栏底部的 **「📋 复制 AI 助手指令」**，粘贴到 Claude Code / ChatGPT 等任意 AI 助手——它会自动了解产品功能、读取你的当前配置、帮你完成设置。

Agent 也可以直接访问：

```
http://127.0.0.1:9528/agent.md         # 产品文档 + API 说明（无需 token）
http://127.0.0.1:9528/api/state        # 当前配置（需 token）
```

---

## 配置面板

菜单「偏好设置…」（⌘,）打开原生窗口，侧边栏分组一目了然：

| 分组 | 页面 | 你能做什么 |
|------|------|-----------|
| 代理 | 隧道 | SSH 连接 master-detail，增删改一气呵成 |
| 代理 | 网络设置 | SOCKS5/HTTP 端口、抓包目录、保留天数 |
| 系统 | 系统选项 | 防睡眠、登录启动、设为系统代理 |
| AI 路由 | 供应商 | 后端接入与凭证（API Key / 环境变量 / 认证头） |
| AI 路由 | Claude Code 同步 | 角色模型映射与默认兜底，同步写入 Claude Code |
| AI 路由 | 运行统计 | 今日 / 近 7 天 / 全部用量、缓存命中率与路由来源 |
| AI 路由 | 余额速览 | 供应商余额与套餐配额，独立刷新 |

⌘S 保存。隧道变更点菜单「重新连接」生效。

---

## 状态指示

| 图标 | 含义 |
|------|------|
| 🟢 | 已连接，畅通 |
| 🟡 | 连接中，稍候 |
| ⚫ | 未连接（已停止 / 失败 / 已暂停） |

---

<div align="center">

**Magic AI Router** · 让网络听你的

[下载最新版](../../releases) · [提 Issue](../../issues)

</div>
