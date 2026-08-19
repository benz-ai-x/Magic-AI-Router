# ADR-022: TLS MITM 通讯报文捕获（需求 5）

- 状态：Proposed
- 日期：2026-07-08
- 决策者：tech-lead
- 影响范围：proxy.py（HTTP→SOCKS5 代理核心）、app.py（子进程生命周期 + 菜单栏 UI）、新增 capture 模块、打包链（build.sh / PyInstaller）、Python 运行时下界

## 上下文

Magic Proxy 当前是 HTTP→SOCKS5 **透传**代理（`proxy.py`）：浏览器设 HTTP 代理为本应用 → 经 SSH SOCKS5 隧道转发到远程出口。HTTPS 流量走 `handle_connect` + `bidirectional_relay`（CONNECT 透传），代理只看到加密 TLS 字节流。

需求 5 要求：建立连接后**捕获通讯报文**，用于人工分析 AI 调用（OpenAI / Anthropic / DeepSeek / 豆包 / Qwen / MiniMax 等）的请求 / 返回——特别是 `messages[].role=system/user` 的 prompt 明文。**用户已确认走 TLS MITM 路线**（在本地代理层解密 TLS、看明文、再经 SSH 隧道转发）。本 ADR 不决策"是否做 MITM"（用户已拍板），只决策**用哪条技术路线实现 MITM**。

MITM 必须发生在 **browser ↔ 本地代理**这一段（本地解密、看明文），解密后 re-encrypt 经 SSH SOCKS5 隧道发往真实 AI 服务器——离开本机的流量仍然是正常 TLS 到真实目的地，代理层只是"中间人"。这是一个安全敏感功能：根 CA 私钥本地生成、本地存储、本地信任，不做任何"外发证书"或"云端签发"。

## 决策

| 维度 | 选型 | 理由 |
|---|---|---|
| MITM 引擎 | **路线 B — mitmproxy 级联**（`mitmdump` 子进程 + upstream HTTP 模式 + addon 抓 AI prompt） | TLS/cert 拦截是安全关键代码，自研（路线 A）等于重写一个已被千万级审计过的 MITM 引擎；mitmproxy 处理 HTTP/1.1 + HTTP/2 + SSE + WebSocket 全套，AI 响应的 SSE 流式开箱即用；级联架构对现有 `proxy.py` **零改动**（mitmproxy 的 upstream HTTP 模式直接复用 `handle_connect` + `bidirectional_relay` 的 CONNECT 隧道） |
| mitmproxy 集成形态 | **子进程（`mitmdump`）级联**，不用 in-process 库嵌入 | 子进程匹配现有 SSH 子进程模式（`proxy.py` 已管 SSH `Popen`）；mitmproxy v12 有复杂 asyncio 内部，in-process 会与现有后台线程的 asyncio loop 冲突；子进程隔离 + addon 脚本经 `-s` 传入，接口清晰 |
| 浏览器代理入口 | 抓包模式开启时浏览器指向 **mitmproxy 端口**（默认 8080）；关闭时回退现有 proxy.py 端口（8888） | mitmproxy 在前做 MITM，proxy.py 在后做 HTTP→SOCKS5；两级端口由菜单栏 toggle 切换 |
| Python 运行时下界 | 从 ≥3.9 **上调到 ≥3.12**（打包工具链驱动，非我方代码需要） | mitmproxy ≥11.1.0（含全部 12.x）的 PyPI `requires-python` 实测 `>=3.12`（2026-07-08 verified：12.2.3 与 11.1.0 均 `>=3.12`，11.0.0 曾 `>=3.10`）；mitmproxy 被 PyInstaller 打进冻结 `.app`，故**构建解释器须 ≥3.12**（Python 版本是构建环境而非用户系统约束，上调代价可控）。**我方自有代码下界仍 3.9**（tech-lead 复核：全仓 15 个 .py 零 3.12-breaker），3.12 纯由 mitmproxy 倒逼——改走 standalone binary（自带运行时）则我方下界可回落。CVE-2025-23217 只影响 **mitmweb**（≤11.1.0，11.1.2 已修）、**mitmdump 不受影响**，与本选型无关（早前"避开 11.x CVE"表述已更正） |
| 报文导出格式 | **JSONL**（每条 capture 一行：timestamp / provider / model / messages[].role+content / SSE 响应重组片段）+ 可选导出 mitmproxy flow 文件（`.mitm`，供 mitmproxy web UI 全量检视） | JSONL 对人工分析 AI prompt 最友好（grep / jq 直接用）；HAR 不善表达 SSE 流式；mitmproxy flow 文件保兼容性兜底 |

## 数据流（MITM 与 SSH 隧道的关系）

抓包模式开启时的数据流（**MITM 只发生在 localhost 的 mitmproxy 层**，离开本机的流量经 SSH 隧道正常 TLS 到真实 AI 服务器）：

```
 ┌─────────┐  HTTP 代理 (CONNECT)   ┌──────────────┐  upstream HTTP 代理 (CONNECT)   ┌──────────────┐  SOCKS5   ┌───────────┐        ┌──────────────┐
 │ 浏览器    │ ─────────────────────> │ mitmproxy    │ ──────────────────────────────> │ proxy.py     │ ────────> │ SSH 隧道   │ ──────> │ 真实 AI 服务器 │
 │ /AI SDK  │  ① browser↔mitm:        │ (:8080)      │  ③ mitm↔proxy.py:               │ (:8888)      │           │ (:1080)   │        │ api.openai   │
 └─────────┘  动态证书 MITM,           └──────┬───────┘  CONNECT 隧道,                  └──────────────┘           └───────────┘        │ .com 等      │
              ② mitm 看到 HTTP 明文           │ addon     ④ mitm 经此隧道与真实                 (现有 proxy.py, 零改动)                          └──────────────┘
              (request/response/SSE)          │ 抓 prompt  服务器做真实 TLS 握手
                                               ▼
                                        ┌──────────────┐
                                        │ JSONL 导出    │  ← addon 落盘 / 经 IPC 回传 UI
                                        └──────────────┘
```

**两段 TLS（关键确认）**：
- **① browser ↔ mitmproxy**：mitmproxy 用动态签发的叶子证书（由本地根 CA 签）做 TLS 终止 → **此处看到 HTTP 明文**（request headers / body / SSE 流式响应）
- ③④ mitmproxy ↔ 真实服务器：mitmproxy 经 proxy.py 的 CONNECT 隧道（proxy.py 再经 SSH SOCKS5）与真实 AI 服务器做**正常 TLS 握手**（真实证书验证），离开本机的流量是端到端正常 TLS

**proxy.py 零改动**：mitmproxy 的 upstream HTTP 模式把 proxy.py 当一个标准 HTTP CONNECT 代理——`handle_connect` 先读 CONNECT 头、`socks5_connect` 经 SSH 隧道连到目标、回 `200`、再 `bidirectional_relay` 透传字节。mitmproxy 在这个隧道里自己做 TLS，proxy.py 不感知 TLS 内容。现有透传逻辑一字不改。

## 备选方案（路线对比）

### 路线 A — 自研 MITM 引擎（否决）

`cryptography` 生成根 CA + 按域名动态签叶子证书；`asyncio` + `ssl` 做双向 TLS；`h11` 解析明文 HTTP/1.1；手写 SSE 重组 + JSON body 提取。

| 维度 | 路线 A（自研） | 路线 B（mitmproxy） |
|---|---|---|
| 依赖体积 | 小：cryptography + h11（~5MB） | 大：mitmproxy 全家桶（cryptography + h2 + hyperframe + kaitaistruct + ldap3 + protobuf + publicsuffix2 + tornado + wsproto + aioquic 等，~20-30MB） |
| 工作量 | ~15 人天（CA + 动态签发 + asyncio SSL 终止 + HTTP 解析 + SSE 重组 + 远端 TLS + Keychain UX + 测试） | ~12-14 人天（addon 3d + 子进程管理 1.5d + 端口切换 1d + CA/Keychain 1.5d + **PyInstaller 打包 2-3d** + 导出 2d + 测试 2d） |
| HTTP/2 | ❌ h11 只做 HTTP/1.1；AI API 普遍支持 HTTP/2，浏览器会 ALPN 协商，自研需降级到 HTTP/1.1（能用但是短板） | ✅ mitmproxy 完整支持 HTTP/1.1 + HTTP/2 + WebSocket |
| SSE 流式 | ❌ 需手写 chunk 重组，correctness 风险高 | ✅ mitmproxy 开箱即用（`flow.response.stream` / contentview 已处理 SSE） |
| 安全风险 | ⚠️ **极高**——TLS/cert 代码是安全关键，自研等于重写一个已被千万级审计的引擎；cert 生成 / 握手 / OCSP 任一处 bug 都是 CVE 级漏洞 | 低——mitmproxy 核心久经审计，addon 只做读侧提取 |
| PyInstaller 打包 | ✅ 依赖少，打包简单 | ⚠️ **主要工程风险**——mitmproxy 依赖树复杂，hidden-imports + hooks 需仔细调（官方论坛 / GitHub 多个 issue 报告打包失败） |
| 与 proxy.py 集成 | 需深度改 `handle_connect`：替换 CONNECT 透传为 TLS 终止 + 明文解析 + 远端 TLS | **零改动**——upstream HTTP 模式直接复用现有 CONNECT 隧道 |
| asyncio 兼容 | ✅ 纯 asyncio，融入现有后台线程 event loop | 子进程隔离，不碰现有 event loop（反而更干净） |

**否决理由**：① TLS/cert 拦截是安全关键代码，在一个 SSH 隧道代理工具里自研 TLS MITM 是不负责任的——mitmproxy 已被广泛审计，重写它的高风险低收益；② h11 只覆盖 HTTP/1.1，HTTP/2 / SSE / WebSocket 全要手补，工作量与正确性风险远超省下的依赖体积；③ 路线 B 对 `proxy.py` 零改动，路线 A 要侵入式改 `handle_connect`（回归风险高，proxy.py 是核心透传路径，非 AI 流量也走它）。

**路线 A 的唯一优势是依赖体积小**——但对一个本地菜单栏工具，mitmproxy 的 ~20-30MB 增量在 .app 里可接受（且用户只在"抓包模式"才启用 mitmproxy 子进程，不抓包时不启动、不占资源）。

### 路线 B 内部：in-process 库嵌入 vs 子进程级联（子决策）

| 维度 | B1 in-process 库嵌入 | B2 子进程级联（推荐） |
|---|---|---|
| asyncio 冲突 | ⚠️ mitmproxy v12 有复杂 async 内部，需与现有后台线程的 asyncio loop 共存 | ✅ 子进程独立 event loop |
| PyInstaller | ⚠️ 更难——in-process 需把 mitmproxy 全部模块正确打包 | 略好——但仍需调 hidden-imports |
| 接口 | 直接拿 flow 对象，无 IPC | addon 经文件 / local socket 回传 |
| 与 SSH 子进程模式一致性 | 不一致（SSH 是子进程，mitmproxy 变库） | ✅ 一致（都是子进程） |

**选 B2（子进程级联）**：一致性 + 隔离性 + 未来 mitmproxy 版本升级时内部 API 变化不影响 Magic Proxy 主进程。

## 影响

### 正向

- **核心代理零改动**：`proxy.py` 的 `handle_connect` / `bidirectional_relay` / `socks5_connect` 一字不改，mitmproxy 把它当 upstream CONNECT 隧道用；非抓包模式完全不受影响
- **HTTP/2 + SSE + WebSocket 开箱即用**：mitmproxy 全覆盖，AI 响应的流式 SSE 无需自研重组
- **addon 模型成熟**：mitmproxy 的 addon（`-s addon.py`）专为"捕获 / 修改 flow"设计，AI prompt 提取是几行 `flow.request.json()` / `flow.response` 的事
- **安全审计背书**：TLS 拦截代码不自研，避免 cert / 握手层 CVE 风险

### 负向

- **依赖体积**：mitmproxy 全家桶 ~20-30MB，.app 体积明显增大（当前 .app 较小）。缓解：抓包模式关闭时不启动 mitmproxy 子进程
- **Python 下界 3.9 → 3.12（打包工具链驱动）**：mitmproxy ≥11.1.0（含全部 12.x）的 PyPI `requires-python` 实测 `>=3.12`（更正早前误记的 3.10）。因 mitmproxy 被 PyInstaller 打进冻结 `.app`，**构建解释器须 ≥3.12**（Python 是构建环境非用户系统，上调代价可控）。**我方自有代码下界仍 3.9**——tech-lead 复核全仓 15 个 .py（9 源 + 6 tests）零 3.10/3.12-breaker，唯一版本敏感语法 `tuple[int,str]`（PEP 585）即 3.9+、asyncio 用非弃用 bootstrap；3.12 纯由 mitmproxy 工具链倒逼、非我方代码需要，若改走 standalone binary fallback（自带运行时）我方下界可回落 3.9
- **PyInstaller 打包复杂度**（主要工程风险）：mitmproxy 依赖树复杂，已知需要大量 hidden-imports + 可能需 PyInstaller hook。这是落地阶段工作量最大 + 最不可控的项，需 spike 验证
- **新增子进程**：mitmproxy 子进程与 SSH 子进程并存，app.py 需管双子进程生命周期（启动顺序 / 退出清理 / 崩溃恢复）

### 风险

| 风险 | 等级 | 缓解 |
|---|---|---|
| **PyInstaller 打包 mitmproxy 失败 / hidden-imports 漏** | ⚠️ 高 | 落地第一周先 spike：用 mitmdump 打包成 .app 验证能跑通，失败则评估是否 ship mitmproxy standalone binary（mitmproxy 官方提供独立二进制，不走 PyInstaller）作为 fallback |
| **证书 pinning 客户端拒绝 MITM** | 低（目标场景） | OpenAI / Anthropic / DeepSeek / 豆包 / Qwen / MiniMax 的 Python SDK 均用 httpx → 系统/OS 信任库，**不做 cert pinning**，信任根 CA 后即可 MITM。浏览器（Chrome/Firefox/Safari on macOS）用系统钥匙串，信任后可用。CLI 工具（Claude Code / Codex）用 Node.js 需设 `NODE_EXTRA_CA_CERTS` 环境变量。**pinning 风险场景**：iOS/Android 原生 AI App（ChatGPT app 等）可能 pin，但非本需求目标（目标是桌面 AI API 分析） |
| **根 CA 私钥安全** | 中 | 根 CA 首次生成后存本地文件（`~/.magic-proxy-ca/`），文件权限 600；私钥**绝不**进 .app bundle / 不外发。Keychain 信任是"信任公钥根证书"，不涉及私钥 |
| **mitmproxy 版本耦合** | 低 | 子进程隔离，mitmproxy 内部 API 变化不影响 Magic Proxy 主进程；addon 只用稳定的高层 flow API |

## 证书信任的 UX（首次引导）

1. Magic Proxy 首次开启抓包模式时，检测根 CA 是否已生成：未生成则用 mitmproxy 自带机制生成根 CA（`~/.mitmproxy/` 或自定义 `~/.magic-proxy-ca/`）
2. 弹出引导窗（PyObjC 原生窗，复用 `prefs.py` 模式）：告知"需信任根 CA 才能解密 HTTPS"，提供"打开钥匙串访问"按钮
3. 用 `security add-trusted-cert -d -r trustRoot -k ~/Library/Keychains/login.keychain-db <ca.pem>` 自动信任，或引导用户手动在钥匙串里设"始终信任"
4. macOS 会弹系统授权对话框（用户输密码确认）——这是 macOS 安全机制，无法绕过
5. 信任后菜单栏提示"抓包模式：就绪"；未信任则提示"CA 未信任，HTTPS 无法解密"

## 报文存储 / 导出格式建议

- **主格式 JSONL**（`~/.magic-proxy-captures/<date>.jsonl`）：每行一条 capture 事件，字段：
  ```json
  {"ts":"2026-07-08T12:34:56Z","flow_id":"...","method":"POST","url":"https://api.openai.com/v1/chat/completions",
   "provider":"openai","model":"gpt-4o","request":{"messages":[{"role":"system","content":"..."},{"role":"user","content":"..."}]},
   "response":{"sse_chunks":[...],"reassembled":"..."},"bytes_up":1234,"bytes_down":5678}
  ```
- **可选导出 mitmproxy flow 文件**（`.mitm`）：供用户在 `mitmweb` 全量检视（含非 AI 流量、二进制、WebSocket 帧）
- **HAR 不推荐**：不善表达 SSE 流式；但可作未来兼容性导出项

## 本 ADR 不覆盖的决策

- **AI provider 自动识别逻辑**（从 URL/host 推断 openai/anthropic/deepseek/...）—— 留给 addon 实现细节
- **捕获数据的 UI 呈现**（菜单栏内查看 vs 外部浏览器打开 JSONL vs 新窗口）—— 留给 product-lead 拆 task
- **捕获数据的留存策略 / 自动清理**（保留几天 / 体积上限）—— 留给后续 config 设计
- **mitmproxy addon 的 IPC 机制选型**（文件 watch / Unix domain socket / stdout 行协议）—— 留给 addon 实现细节
- **是否 ship mitmproxy 官方 standalone binary 作为 PyInstaller fallback**—— 待 spike 结论
- **NODE_EXTRA_CA_CERTS 自动注入**（让 Node.js CLI 工具信任 CA）—— 留给后续增强

## 后续工作

- [ ] tech-lead / backend-dev：**PyInstaller + mitmproxy 打包 spike**（最高优先，是路线 B 最大风险点）——验证 `mitmdump` 能否被 PyInstaller 正确打包进 .app 并运行；失败则评估 mitmproxy 官方 standalone binary fallback，结果另开子决策
- [ ] backend-dev：实现 mitmproxy addon（AI prompt 提取 → JSONL），`-s addon.py` 传入；覆盖 OpenAI / Anthropic / DeepSeek / 豆包 / Qwen / MiniMax 六家请求格式
- [ ] backend-dev：app.py 子进程管理扩展（mitmdump 与 SSH 双子进程生命周期 + 端口 toggle）
- [x] tech-lead：Python 下界上调 3.9 → **3.12** 的复核（完成 2026-07-08：全仓 15 个 .py 零 3.10/3.12-breaker，我方代码下界仍 3.9，3.12 由 mitmproxy 打包倒逼；floor 数值由 3.10 更正为 3.12——mitmproxy ≥11.1.0 `requires-python >=3.12`）——ADR-000 + CLAUDE.md 的 Python 下界同步待 Task 5 post-merge 回填
- [ ] backend-dev：根 CA 生成 + Keychain 信任引导 UX（`security add-trusted-cert`）
- [ ] tech-lead：落地后回填本 ADR「版本与查证」表实际 pin 版本

## 版本与查证

**查证基线日期**：2026-07-08

| 选型 | 选定版本 | 最新稳定版 | 与最新版差距 | 维护状态 | 信息来源（含原文摘录） |
|---|---|---|---|---|---|
| mitmproxy | **12.2.3**（spike 通过：PyInstaller 打包 mitmdump 成功，无需 standalone fallback） | 12.2.3（2026-05-12） | 取最新 | Active（v12 于 2025-04 发布，持续迭代） | [pypi.org/project/mitmproxy](https://pypi.org/project/mitmproxy/) — "Latest release. Released: May 12, 2026"；[docs.mitmproxy.org/concepts/modes](https://docs.mitmproxy.org/stable/concepts/modes/) — "In upstream mode ... mitmproxy supports both explicit HTTP and explicit HTTPS in upstream proxy mode"（**upstream 不支持 SOCKS5**，故走级联 proxy.py）；**PyPI `requires-python` 实测（2026-07-08）：12.2.3 与 11.1.0 均 `>=3.12`，11.0.0 为 `>=3.10`** → 构建下界 3.12（早前"要求 3.10+"表述已更正；[issue #5233](https://github.com/mitmproxy/mitmproxy/issues/5233) "Drop Support For Python 3.9" 只是首轮抬到 3.10，11.1.0 起再抬到 3.12） |
| h11（路线 A 依赖，记录否决依据） | — | 0.16.0（2025-04-24） | — | Active | [pypi.org/project/h11](https://pypi.org/project/h11/) — "A little HTTP/1.1 library ... bring-your-own-I/O"；**HTTP/1.1 only，无 HTTP/2**，是路线 A 被否决的关键短板之一 |
| cryptography（路线 A 依赖 / mitmproxy 传递依赖） | 待回填 | ~49.x（50.0.0-dev1 在开发中） | 待确认 | Active（Python 3.9+） | [cryptography.io/en/latest/changelog](https://cryptography.io/en/latest/changelog/) — "50.0.0-dev1"（dev）；stable 待回填 |
| PyInstaller（受影响：打包 mitmproxy） | 未锁定（本地实测 6.20.0） | 6.21.0 | 落后 1 patch | Active | 同 ADR-000；打包 mitmproxy 的 hidden-imports 需 spike 验证（spike 通过） |
| Python（构建解释器下界） | **≥3.12**（我方自有代码下界仍 3.9） | 3.14.x（开发机实测 3.14.4） | 下界取 3.12（mitmproxy 硬底），非取最新 | Active | 由 mitmproxy `requires-python >=3.12` 倒逼（见 mitmproxy 行）；我方代码 tech-lead 复核零 3.12-breaker（15 个 .py），本可 3.9，3.12 纯为把 mitmproxy 打进冻结 app |

**回填规则**：spike + 落地后由执行层回填实际 pin 版本，commit message 加 `docs(adr): backfill ADR-022 verification for [pkg]`。

**关键验证结论**（影响架构）：
- mitmproxy **upstream 模式只支持 HTTP/HTTPS 上游代理，不支持 SOCKS5 上游**（官方文档确认；SOCKS5 仅是 mitmproxy 的监听模式）。故级联架构 = mitmproxy → proxy.py（upstream HTTP）→ SSH SOCKS5，而非 mitmproxy 直接连 SSH SOCKS5。
- OpenAI / Anthropic / DeepSeek / 豆包 / Qwen / MiniMax 的 Python SDK **均不做 cert pinning**（走 httpx + 系统/OS 信任库），信任根 CA 后 MITM 即可工作（来源：[OpenAI 社区](https://community.openai.com/t/how-can-i-disable-ssl-verification-when-using-openai-api-in-python/110837) 多帖证实企业代理 TLS 拦截场景靠加根 CA 解决——反证无 pinning）。
- mitmproxy + PyInstaller 有已知打包难题（[官方论坛](https://discourse.mitmproxy.org/t/use-mitmproxy-with-pyinstaller/1436) / [pyinstaller#9017](https://github.com/pyinstaller/pyinstaller/issues/9017)），是本路线最大工程风险，须 spike。**（spike 2026-07-08 通过：PyInstaller 成功打包 mitmdump，无需 standalone binary fallback，ADR-023 不落盘。）**
- **CVE-2025-23217 与本项目无关**（更正）：该 CVE 是 mitmweb 的 API 认证绕过（[GHSA-wg33-5h85-7q5p](https://github.com/advisories/GHSA-wg33-5h85-7q5p)，仅 mitmweb ≤11.1.0、11.1.2 已修，官方明确"the mitmproxy and mitmdump tools are unaffected"）。本项目用 **mitmdump**，故该 CVE 不构成版本 / 下界驱动——决策表中早前"避开 11.x CVE"表述已更正。真正的 3.12 下界驱动是 mitmproxy 的 PyPI `requires-python >=3.12`。
