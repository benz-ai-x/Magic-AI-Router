# CLAUDE.md

## Agent skills

### Issue tracker

Issues live as GitHub issues in `benz-ai-x/Magic-AI-Router` (via `gh` CLI). See `docs/agents/issue-tracker.md`.

### Triage labels

Triage uses the five-label canonical vocabulary. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context repo: `CONTEXT.md` + `docs/adr/` at the root. See `docs/agents/domain.md`.

## 概述

Magic AI Router — macOS 菜单栏应用（壳），承载两个独立产品：Magic Proxy（SSH 隧道 HTTP→SOCKS5 代理 + TLS 抓包）和 Suanpan（AI 请求路由网关）。纯 Python 3 实现，可打包为原生 .app。

> **模块清单、菜单结构等易过期信息以代码为准**（`tests/test_docs_drift.py` 做防漂移守卫）；版本号见 `build.sh`。

> 用户可见名是 **Magic AI Router**（见 `build.sh` / `app.py`）；内部代码和历史文档中常称 Magic Proxy / Magic-AI-Router。

## Tech Stack

Python ≥3.9（自有代码下界；**打包工具链因 mitmproxy ≥12 需构建解释器 ≥3.12**，见 ADR-001）+ rumps（菜单栏 UI）+ asyncio（HTTP→SOCKS5 代理）+ PyObjC objc/AppKit/Foundation（WKWebView 设置窗 / CA 信任引导窗 `ca_trust.py` / 日志窗 `log_window.py`）+ Pillow（图标生成）+ mitmproxy 12.2.3（抓包模式 TLS MITM）+ FastAPI + uvicorn + httpx + pydantic（Suanpan AI 路由网关）；PyInstaller 打包 `.app`（`--windowed` + `LSUIElement=true`）；SSH 隧道经系统 `ssh` / `sshpass`（密码认证）；SSH 密码走 macOS Keychain。详见 ADR-000 + ADR-001。

## 命令

```
# 开发模式
python3 app.py

# 安装依赖
pip3 install -r requirements-dev.txt

# 测试（本机须 python3 -m：bare pytest=3.9 在 suanpan 类型注解崩溃）
python3 -m pytest tests/

# 打包 .app
bash build.sh
cp -R "dist/Magic AI Router.app" /Applications/

# 发布分发（签名 + 公证）
bash scripts/notarize.sh
```

## 架构

```
app.py ── 编排器：__init__ + _on_tick + 菜单回调（子模块由 app.py 直接持有）
util.py ── resource_path（frozen 平铺 / dev 按域包子目录查找）+ truncate + build_stamp + version_display
mpconf/ ── 配置栈
  config.py ── 配置 I/O（load/save/merge/migrate）；http_listen_port 字段 + 读时兼容旧 "host:port"
  config_store.py ── 路径注册表 PATHS + 原子写管线（mkstemp+chmod 0600+os.replace）；所有托管配置文件的唯一安全写入口
  netloc.py ── host:port 解析/格式化/loopback 校验的唯一所有者
  provider_auth.py ── 供应商认证纯逻辑（resolve_api_key + build_outbound_headers）

tunnel/ ── SSH 隧道核心
  proxy.py ── asyncio HTTP→SOCKS5 代理（明文 HTTP 逐请求归属：跨 origin 安全重连，绝不静默误投）+ SSHMonitor + ProxyRuntime
  http_framer.py ── 明文 HTTP 增量 framer：起始行/头部/CL/chunked 定界，未定界即安全关闭
  async_runtime.py ── daemon 线程 + asyncio 循环 + 代际计数停止（ProxyRuntime/Suanpan 共用）
  connection_coordinator.py ── SSH 连接/重试/host-key 流程编排
  subprocess_monitor.py ── 子进程生命周期基类（SSHMonitor/CaptureMonitor 继承）
  retry_scheduler.py ── SSH 重试退避调度
  host_key.py ── SSH known_hosts 管理
  host_key_flow.py ── SSH 主机密钥信任流程

shellui/ ── 界面
  menu_builder.py ── 菜单栏 UI 构建 + 状态图标
  webview_window.py ── WKWebView 窗口（ObjC 薄 adapter）
  log_window.py ── 日志窗
  bridge_protocol.py ── 设置窗 JS↔Python 消息协议（纯 Python 核心，可单测）
  config_ui.html ── 自包含 Web 配置面板（三层架构：LAYER 1 纯逻辑可 node:test / VIEWS 注册表 / 渲染层）

capture/ ── 抓包
  capture.py ── mitmdump 子进程管理（抓包模式）
  capture_controller.py ── 抓包启停 + 信任缓存控制
  capture_store.py ── 抓包目录/文件管理（跨进程共享）
  resources.py ── 资源契约：resolve_capture_resources（mitmdump 三级链 + addon 校验 + 目录 preflight）+ smoke_capture_boot/SMOKE_*（启动冒烟判据单一归宿）
  ai_capture_addon.py ── mitmproxy addon：6 家 AI 请求抽取落 JSONL
  ca_trust.py ── 根 CA 信任检测 + 引导窗
  mitmdump_entry.py ── 抓包构建入口（mitmproxy mitmdump 包装）
  chromium_proxy.py ── Chromium 启动代理配置

sysctl/ ── 系统集成
  system_proxy.py ── networksetup 包装：事务式 + 崩溃恢复
  sys_proxy_controller.py ── 系统代理收敛状态机
  sleep_blocker.py ── 防睡眠
  login_item.py ── 登录启动 LaunchAgent（bundle ID 见 build.sh）
  port_check.py ── 端口占用检测（占用仅是线索；SIGTERM→SIGKILL 升级在此）
  instance_owner.py ── 实例所有权锁：pid+启动时间双匹配抗 PID 复用；O_EXCL 原子创建/陈旧接管/release
  keychain.py ── macOS Keychain 读写

services/ ── 服务
  config_server.py ── Web 配置服务 :9528（JSON CRUD + bearer token + body 上限）
  suanpan_runtime.py ── Suanpan 网关线程化运行时（延迟导入）
  claude_code_setup.py ── Claude Code 自动配置（写 ~/.claude/settings.json，经 config_store.atomic_write；env 契约见 ADR-003）
  lifecycle_runtime.py ── 服务生命周期编排：start_all/quit 顺序契约、tick/sync_sleep/stop_all、capture_state 单投影
  balance_usage.py ── 余额 API + 本地用量多维聚合（CST 范围 / 缓存 / 路由来源）+ 供应商连通性探测
  stats.py ── 运行统计

suanpan/ ── AI 路由网关子包（Anthropic Messages API → 多家 LLM 后端）
  config.py ── Pydantic 配置 schema + YAML 加载/回写 + API key 掩码契约（api_key_set 布尔，真实 key 不出进程；见 ADR-002）
  main.py ── FastAPI app factory + 路由 handler
  middleware.py ── APIKey + BodyLimit 中间件
  proxy.py ── 流式代理转发 + 传输级重试
  compat.py ── 供应商请求 body 归一化（system 扁平化 / document 块剥离 / beta 字段剥离）
  usage_extractor.py ── UsageExtractor SSE 用量提取（CRLF 兼容 + max-merge 跨供应商）
  router.py ── 路由决策（内联覆盖 → SUBAGENT 标签 → 规则 → 默认）
  usage_log.py ── 追加写 JSONL + 50MB 轮转 + 内存滚动总计
  __main__.py ── `python3 -m suanpan` 独立启动入口
```

**线程模型：** 主线程跑 rumps NSRunLoop（菜单栏）。后台 daemon 线程跑：asyncio 事件循环（代理服务 ProxyRuntime）、Suanpan 网关（uvicorn）、config server（http.server）。

**菜单结构：** 状态行 → 代理隧道 ▸（含连接控制）→ AI 路由 ▸ → 抓包 ▸ → 页脚

**偏好设置：** 菜单「偏好设置…」打开 WKWebView 窗口（`http://127.0.0.1:9528/`）。侧边栏分组：代理（隧道 / 网络设置）+ AI 路由（供应商 / Claude Code 同步 / 运行统计 / 余额速览）+ 系统（系统选项）。

## 配置

### Magic Proxy — `~/.magic-proxy.json`

支持多隧道，`auth_type` 为 `key`（默认）或 `password`（需 `sshpass`）。密码走 macOS Keychain。监听地址存 `http_listen_port`（整型端口；旧 `"host:port"` 字符串读时兼容，见 ADR-002）。

### Suanpan AI 路由 — `~/.suanpan.yaml`

参考 `suanpan.example.yaml`。首次启动时自动创建最小默认配置。监听地址存 `listen_port`（整型端口；旧 `"host:port"` 字符串读时兼容，见 ADR-002）。

## 注意事项

- 菜单栏状态图标用 `MenubarIcon.png` 染色（绿=已连接 / 黄=连接中 / 灰=未连接）
- PyObjC 方法名不能以单下划线开头（会被当成 ObjC selector）
- 打包后的 .app 设置 LSUIElement=true，不显示 Dock 图标
- Suanpan 网关依赖为延迟导入——未安装时 app 正常启动，网关功能不可用并提示安装命令
- Config server（:9528）和 AI 路由网关（:9527）是两个独立端口，不可合并
- 测试口径：`python3 -m pytest --cov`（omit 清单见 `.coveragerc`）+ `node --test tests/js/`；覆盖率数字以运行为准，不在此缓存
