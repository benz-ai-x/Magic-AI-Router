# Magic AI Router

**The local AI network stack in your menu bar: route Claude Code to any model, ride your own tunnel, and actually *see* what your AI apps send.**

[English](README.md) · [简体中文](README.zh-CN.md)

[![Release](https://img.shields.io/github/v/release/benz-ai-x/Magic-AI-Router)](../../releases)
[![Downloads](https://img.shields.io/github/downloads/benz-ai-x/Magic-AI-Router/total)](../../releases)
[![Stars](https://img.shields.io/github/stars/benz-ai-x/Magic-AI-Router?style=social)](../../stargazers)
![Platform](https://img.shields.io/badge/platform-macOS%20%28Apple%20Silicon%29-blue)
![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)
[![Tests](https://img.shields.io/badge/tests-1700%2B%20%C2%B7%20ADRs%20%C2%B7%20zero%20telemetry-brightgreen)](CONTEXT.md)

![Menu bar](assets/docs/menu-bar-v091.png)

Routing tools forward requests. Proxies move bytes. **Magic AI Router closes the loop**: one native macOS app holds all three layers — access (your own SSH tunnels), routing (an Anthropic-compatible local gateway), and observability (TLS decryption of AI traffic). Change a routing rule, and watch the *real* cost, latency, and responses change in the same app. Keys never leave your machine.

| Layer | What you get |
|---|---|
| 🧮 **Route** | Local gateway (`:9527`) speaking the **Anthropic Messages API** — point Claude Code at it, route to **GLM / DeepSeek / Kimi / Qwen / Anthropic** by model-prefix rule |
| 🔗 **Access** | SSH SOCKS5 proxy (`:8888`) through your own server + **multi-tunnel `ssh -L` port forwarding** running in parallel, all from the menu bar |
| 🔍 **Verify** | TLS capture decrypts AI calls (OpenAI, Anthropic, DeepSeek, Doubao, Qwen, MiniMax) to readable JSONL + per-provider usage, cache-hit-rate and balance stats |

## 60-second start

The #1 reason people install this: **keep the Claude Code workflow, swap the backend** (and the bill).

```bash
# 1. Launch the app → Preferences → AI Routing → Providers
#    add GLM  https://open.bigmodel.cn/api/anthropic  + your API key
#    set router.default = GLM/glm-5.2
# 2. Point Claude Code at the gateway:
export ANTHROPIC_BASE_URL=http://127.0.0.1:9527
claude   # every request now routes per your rules
```

Mix models per tier — strong for main threads, cheap for subagents:

```yaml
rules:
  - match_prefix: claude-opus     # heavy lifting
    route_to: GLM/glm-5.2
  - match_prefix: claude-sonnet   # daily work
    route_to: DeepSeek/deepseek-v4-pro
  - match_prefix: claude-haiku    # subagents → cheap
    route_to: KIMI/k3
```

Type `provider/model` (e.g. `DeepSeek/deepseek-chat`) anywhere a model is expected to bypass all rules.

## Why not just …?

Curators and comparison shoppers, this section is for you:

| | **Magic AI Router** | claude-code-router / LiteLLM | OpenRouter (SaaS) | 手动 ssh + 配置文件 |
|---|---|---|---|---|
| Where it runs | **Local-first** (menu bar / Docker) | Local or self-host server | Their cloud | Your terminal |
| Keys & prompts | **Never leave your machine** | Yours (self-host) | Sent to service | Yours |
| Sees actual AI traffic (TLS plaintext) | ✅ built in | ❌ | ❌ | needs mitmproxy setup |
| SSH access layer (SOCKS5 + `-L` forwards, parallel) | ✅ built in | ❌ | ❌ | ✅ but manual |
| Cost / cache / usage closed-loop per rule | ✅ same app | partial | dashboard | ❌ |
| macOS native UX (menu bar, settings window) | ✅ | CLI/config | web | ❌ |
| Configurable by the AI agent itself | ✅ `agent.md` + token API | ❌ | ❌ | ❌ |

**The differentiator is the loop, not any single layer** — routers that can't see traffic are flying blind; proxies that can see traffic can't route.

![Settings — tunnel & port forwarding](assets/docs/settings-tunnel-v091.png)

## Install

**macOS (recommended):** grab the signed + notarized `.dmg` from [Releases](../../releases), drag to `Applications`. That's it — ⚫ appears in the menu bar.

**From source / build your own `.app`:**

```bash
git clone https://github.com/benz-ai-x/Magic-AI-Router.git
cd Magic-AI-Router
pip3 install -r requirements-dev.txt && python3 app.py     # run
# bash build.sh                                            # or package
```

**Linux / headless — Docker (gateway + web config):**

```bash
bash docker/suanpan.sh up      # gateway :9527 + web config :9528
bash docker/suanpan.sh sync    # writes ~/.claude/settings.json for Claude Code
```

First run on macOS: Preferences → **Proxy → Tunnel** → fill SSH details → menu **代 理 ▸ 连接代理** → browser proxy → `127.0.0.1:8888`. Pick another tunnel under **端口映射 ▸** to forward a remote port to localhost. Password auth needs `sshpass` once (`brew install hudochenkov/sshpass/sshpass`).

## What's inside

### 🧮 Suanpan (算盘) — LLM routing gateway

- **Providers** — any Anthropic-Messages-compatible endpoint; API key inline, env var, or custom auth header
- **Routing** — prefix rules → default route; inline `provider/model` override; `<SUBAGENT-MODEL>` subagent tag; explicit misroutes fall through loudly (`x-suanpan-fallback` header), never silently
- **Prompt-caching aware** — `anthropic_native` providers keep `cache_control` intact so upstream prompt caches stay effective; stats track hit rate
- **Streaming** — full SSE passthrough with usage extraction; safe retries only (non-idempotent requests never replayed)
- **Usage & balance** — JSONL usage log, today/7d/month/all by provider and route source; balance/quota panels
- **Claude Code sync** — map roles (main/subagent/plan…) to models, one click writes `~/.claude/settings.json`

### 🔗 Magic Proxy — SSH tunnels + port forwarding

- asyncio HTTP→SOCKS5 proxy with per-request origin binding (keep-alive safe, CONNECT, chunked)
- **v0.9 multi-active**: one proxy tunnel (`-D`) + any number of forward-only tunnels (`-L`) in parallel — server A as your proxy, server B mapping ports to `127.0.0.1`; independent retry, host-key handling, `forward_autostart` on launch
- Per-rule one-click SSH reachability test (works on unsaved values)
- Key auth or password (via `sshpass`; password only in macOS Keychain, piped to ssh — never in `argv`/`ps`)
- TOFU host-key pinning; retry backoff capped at 60s and never gives up; wake-triggered reconnect (~5s); transactional system-proxy management; per-app `--proxy-server` launches

### 🔍 AI Capture — TLS recorder for AI APIs

- One click starts a bundled mitmdump cascaded into the proxy; only known AI APIs are logged to `~/.magic-proxy-captures/<date>.jsonl`, everything else passes untouched
- Guided root-CA trust; configurable retention

## Trust & engineering

Security-sensitive tooling earns trust in the open:

- **Zero telemetry** — nothing phones home; every endpoint binds to loopback
- SSH passwords in the macOS **Keychain**, piped to `ssh` (never in `argv`/`ps`/files); `StrictHostKeyChecking=yes` with a dedicated `known_hosts`
- Constant-time key comparison; credential-bearing outbound calls refuse cross-origin redirects and HTTPS→HTTP downgrades; 1MB response cap
- Config writes atomic (`0600`) with a crash-recovery journal; masked keys never leave the UI
- **1700+ tests**, architecture decision records ([`docs/adr/`](docs/adr/)), a drift-guarded domain glossary ([`CONTEXT.md`](CONTEXT.md)) — the discipline is in the repo, not just the claim

## 🤖 Agent-operable

Open Preferences → **“Copy AI assistant instructions”** and paste into Claude Code: the agent reads `http://127.0.0.1:9528/agent.md` (no token), then configures the app for you through a token-guarded local API. **The AI network tool your AI can run.** (The same copy action is available on the settings sidebar — including the browser panel, where it falls back to the token-guarded `/api/agent-instructions` endpoint.)

## FAQ

**Does Claude Code really work with DeepSeek / GLM / Kimi?**
Yes — the gateway is fully Anthropic-Messages-compatible (SSE streaming, tool use, prompt-caching markers preserved). Claude Code only changes `ANTHROPIC_BASE_URL`. No patches, no hacks.

**Is it free? Where do keys live?**
MIT-licensed; it routes to *your* provider accounts. Keys sit in `~/.suanpan.yaml` (`0600`), masked in every UI surface, plaintext never leaves the process. Zero telemetry.

**How is this different from a plain proxy — or a plain router?**
A proxy moves bytes but can't route; a router forwards but can't see traffic. This app does both and closes the loop: per-rule cost, cache-hit and response visibility in the same UI.

**Can I forward a remote port without exposing it?**
Per-tunnel `ssh -L` binds to `127.0.0.1` only, runs in parallel with the SOCKS5 tunnel, reconnects independently.

**Linux / Windows?**
Linux: the Suanpan gateway ships as a Docker image. Menu-bar shell, SSH tunnels and capture are macOS-only.

## For curators (one-liner)

> **Magic AI Router** — open-source macOS menu-bar app that routes Claude Code (Anthropic Messages API) to GLM/DeepSeek/Kimi/Qwen through a local-first gateway, bundled with SSH tunnel/port-forwarding management and TLS-level AI traffic observability. MIT.

## Documentation

[`CHANGELOG.md`](CHANGELOG.md) · [`docs/docker-deploy.md`](docs/docker-deploy.md) · [`docs/adr/`](docs/adr/) (6 ADRs) · [`CONTEXT.md`](CONTEXT.md) (domain glossary)

## License

[MIT](LICENSE) — Copyright (c) 2026 benz-ai-x

---

<div align="center">

**Route it. Tunnel it. See it.**

⭐ Star to follow releases · [Discussions](../../discussions) · [Issues](../../issues)

</div>
