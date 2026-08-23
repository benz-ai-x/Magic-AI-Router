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

单一归属原则（每域一个归宿模块；逐模块清单是防漂移守卫 `tests/test_docs_drift.py` 钉住的契约面）：

```
app.py ── 编排器：__init__ + _on_tick + 菜单回调（子模块由 app.py 直接持有）
util.py ── resource_path（frozen 平铺 / dev 按域包子目录查找）+ 版本戳

mpconf/ ── 配置栈
  config.py ── 配置 I/O + merge/migrate（http_listen_port 读时兼容旧串）
  config_store.py ── PATHS 注册表 + 原子写管线（唯一安全写入口）
  netloc.py ── host:port 解析/格式化/loopback 校验唯一所有者
  provider_auth.py ── 供应商认证纯逻辑 + PROVIDER_REGISTRY 注册表 +
    restore_masked_key（掩码 keep 语义）
  config_state.py ── ConfigStateStore 事务边界：load 四态 / prepare
    全量校验（含 schema + 端口冲突）/ commit（journal+MP+SP+Keychain+
    回调次序）/ recover 幂等重放 / update_mp 菜单写径
  local_token.py ── 本地客户端 token（掩码布尔契约，明文不出 UI）

tunnel/ ── SSH 隧道核心
  proxy.py ── asyncio HTTP→SOCKS5 代理（明文逐请求归属）+ SSHMonitor
  async_runtime.py ── daemon 线程 + asyncio 循环 + 代际停止
  http_framer.py ── 明文 HTTP 增量定界（未定界即安全关闭）
  connection_coordinator.py ── 连接/重试编排（持 _lifecycle_lock）
  subprocess_monitor.py ── 子进程生命周期基类（状态全集声明）
  retry_scheduler.py · host_key.py · host_key_flow.py ── 重试退避 /
    known_hosts 管理 / 主机密钥信任流程
  ssh_launch.py ── SSH 调用策略单一归宿：argv 构建 + probe() +
    stderr→中文失败分类

shellui/ ── 界面
  menu_builder.py ── 菜单栏 UI + 状态图标
  webview_window.py ── WKWebView 窗口（ObjC 薄 adapter）
  log_window.py ── 日志窗
  bridge_protocol.py ── 设置窗 JS↔Python 协议（纯 Python 可单测）
  config_ui.html ── 自包含 Web 配置面板（三层架构）

capture/ ── 抓包
  capture.py ── mitmdump 子进程管理
  capture_controller.py ── 抓包启停 + 信任缓存控制
  capture_store.py ── 抓包目录/文件管理 + 命名知识唯一所有者
    （含 cleanup_expired_captures 保留策略）
  resources.py ── 资源契约三级链 + 冒烟判据单一归宿
  ai_capture_addon.py ── mitmproxy addon：6 家 AI 请求抽取落 JSONL
  ca_trust.py ── 根 CA 信任检测 + 引导窗
  mitmdump_entry.py ── frozen mitmdump 构建入口
  chromium_proxy.py ── Chromium 启动代理配置

sysctl/ ── 系统集成
  system_proxy.py ── networksetup 事务式 + 崩溃恢复
  sys_proxy_controller.py ── 系统代理收敛状态机
  sleep_blocker.py ── 防睡眠
  login_item.py ── 登录启动 LaunchAgent
  port_check.py ── 端口占用检测（SIGTERM→SIGKILL 升级）
  instance_owner.py ── 实例所有权锁：pid+启动时间双匹配抗 PID 复用
  keychain.py ── macOS Keychain 读写（Security 框架可选导入）

services/ ── 服务
  config_server.py ── Web 配置服务 :9528（JSON CRUD + bearer token）
  suanpan_runtime.py ── Suanpan 网关线程化运行时（延迟导入）
  claude_code_setup.py ── Claude Code 自动配置（写 ~/.claude/settings.json）
  lifecycle_runtime.py ── 服务生命周期编排：start_all/quit 顺序契约 +
    capture_state 单投影 + _on_sp_saved 双形态
  authenticated_http.py ── 认证出站：跨 origin 拒 / 降级必拒 / 1MB 上限
  balance_usage.py ── 余额 API + 本地用量多维聚合（CST 范围）
  stats.py ── 运行统计

suanpan/ ── AI 路由网关子包（Anthropic Messages API → 多家 LLM 后端）
  config.py ── Pydantic schema + 掩码契约 + null 节归一 + 文法消费
  main.py ── FastAPI app factory + 路由 handler
  middleware.py ── APIKey（常量时间比较）+ BodyLimit 中间件
  proxy.py ── 流式代理转发 + RetryPolicy + count_tokens aread
  compat.py ── 供应商 body 归一化（anthropic_native 旗标驱动）
  usage_extractor.py ── SSE 用量提取
  router.py ── 路由决策 + parse_route_target 文法所有者 + fallback_from 可感知
  usage_log.py ── 追加写 JSONL + 轮转
  prewarmer.py ── 启动预热 best-effort adapter
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

**硬约束（违反即出错）**
- PyObjC 方法名不能以单下划线开头（会被当成 ObjC selector）
- Config server（:9528）和 AI 路由网关（:9527）是两个独立端口，不可合并
- 测试口径：`python3 -m pytest --cov`（omit 清单见 `.coveragerc`）+ `node --test tests/js/*.test.mjs`（glob 形式——node ≥26 目录模式报 MODULE_NOT_FOUND）；覆盖率数字以运行为准，不在此缓存

**形态事实（环境即真源，改代码即改）**
- 菜单栏状态图标用 `MenubarIcon.png` 染色（绿=已连接 / 黄=连接中 / 灰=未连接）
- 打包后的 .app 设置 LSUIElement=true，不显示 Dock 图标
- Suanpan 网关依赖为延迟导入——未安装时 app 正常启动，网关功能不可用并提示安装命令
