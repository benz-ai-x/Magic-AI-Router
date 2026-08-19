# Changelog

All notable changes to Magic-AI-Router are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/), adheres to [SemVer](https://semver.org/).

## [v0.4.8] — 2026-08-14 — Claude Code 同步 + 安全与文档整固

### Added
- **Claude Code 同步功能**：角色映射 UI（default/tier/subagent 独立控件）+ 后端派生；种子载荷 `{roles, order, labels, readonly}`，Python 单一真源（`/api/cc-default-roles`）。

### Fixed
- **安全（#39）**：bearer token 改走 `Authorization` 头，不再出现在 URL query。
- **代码审查修复（#40，16 项）**：明文迁移失败隔离 + keychain 移出 `-w` argv；host-key 后台线程守卫；菜单每秒重建与退出阻塞修复；进程身份识别（`_clear_app_ports` 只杀自身旧实例）；bare IPv6 解析；pgrep/osascript 转义；LaunchAgent 原子写；keychain 密码仅显式切换才删；SSE 流错误记录 + usage 载荷守卫；`count_tokens` body 归一化。
- **CC 推导语义（#42/#43）**：角色 tier 推导改双向前缀首击（对齐 router.py）；subagent 与 default 解耦（haiku tier 目标）。

### Changed
- **CC 模块清理（#44）**：删 Middle-Man 适配器 + 双份角色定义 + 死参数；`one_m` → `ctx_1m`（保留旧键只读 fallback）。

### Development
- **文档防漂移（#41）**：CLAUDE.md 全面更新 + `tests/test_docs_drift.py`（版本单一真源 / 模块清单 / 陈旧短语守卫）；ADR-000 移除陈旧现在时表述。

## [v0.4.7] — 2026-08-13 — 架构深化 + 覆盖率 100% + 安全修复（#37）

### Changed
- **架构深化（4 候选 + ADR-002）**：ServiceCoordinator 瘦身；`http_listen`/`listen` 收敛为整型端口字段 + 读时兼容旧格式；原子写收敛到 `config_store`；`claude_code_setup` 独立模块。
- **UI 信息架构优化（#36）**：导航副标题 + 面板分组对齐产品 + 状态页引导文案 + Base URL 预设提示。
- **余额配额行结构化（#34）**：`normalize_balance` 返回结构化 quotas 数据，UI 统一渲染。

### Added
- **ADR-002**：配置表示与掩码决策固化（`api_key_set` 布尔契约，真实 key 不出进程）。

### Fixed
- **安全 + 正确性（#37，15 项）**：明文密码不落盘 + keychain 日志脱敏；token 常量时间比较 + body 上限 + 负数 Content-Length 防护；SSE CRLF 兼容 + 逗号 target 校验 + auth_header 大小写；PyObjC 方法名规避单下划线 + CA 路径延迟计算。

### Development
- 测试覆盖率 94% → 100%（+134 测试，5 个 `test_cov_*.py`）；CLAUDE.md 去 cache（#38）；新供应商调研：MiniMax / Qwen / 豆包原生 Anthropic 兼容（#35）。

## [v0.4.6] — 2026-08-12 — Claude Code 自动配置 + 网关兼容层

### Added
- **AI agent 集成**：`agent.md` 产品上下文 + Claude Code 自动配置（`claude_code_setup.py`，写 `~/.claude/settings.json`）。
- **网关兼容层（`suanpan/compat.py`）**：system 数组展平 / document 块剥离 / beta tool 字段剥离 + 客户端 `CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS` 兼容变量。

### Changed
- **架构深化**：`drain_and_log` 从闭包提取为独立 async generator；middleware 提取到 `suanpan/middleware.py`；删除 MenuActionHandler 浅模块；SSH 错误结构化（`is_host_key_changed`）。

### Fixed
- `stop_all` 停止 Suanpan 网关；删除死 admin.html；config_ui 描述路径修正（`~/.zshrc` → `~/.claude/settings.json`）。

## [v0.4.5] — 2026-08-11 — 传输级重试

### Fixed
- **传输级错误自动重试一次**（`_RETRYABLE`：NetworkError/ProtocolError/ProxyError/UnsupportedProtocol），消除上游静默关闭空闲 HTTP/2 连接导致的 502；Timeout 明确不重试。

## [v0.4.4] — 2026-08-11 — 用量提取兼容 + 服务重启菜单

### Added
- 代理隧道 / AI 路由菜单新增**重启服务**；KIMI 5 小时/周额度显示 + 余额按百分比染色；关于菜单版本号追加编译时间戳。

### Fixed
- **SSE 解析兼容 `data:` 无空格前缀**（修复 KIMI token 提取）；token 提取改 **max 合并**，适配四家供应商 SSE 格式分歧。

### Development
- 版本断言从 build.sh 动态读取；README 营销口吻重写 + 修正过时事实。

## [v0.4.3] — 2026-08-10 — 架构重构 6 候选落地 + config_ui 模块化

### Changed
- **config_ui 内部模块化（#21）**：三层结构（LAYER 1 纯逻辑可 node:test / VIEWS 注册表 / 渲染层）。
- **ConfigStore 统一配置栈**：路径注册表 `PATHS` + 共享原子写原语；`bridge_protocol` 纯 Python 核心；`netloc` 收敛 host:port 解析为唯一所有者；供应商认证收敛为单一纯实现（`provider_auth.py`）。

### Fixed
- 菜单信任检查加 30s TTL 缓存（消除每秒 verify-cert 子进程）；测试套件不再触碰真实配置文件。

## [v0.4.2] — 2026-08-10 — 架构重构 + UIUX 优化 + AI 路由增强

### Changed
- **架构重构**：抽取 `AsyncRuntime` / `ConnectionCoordinator` / `ServiceCoordinator`；菜单栏重构（双状态行 + 连接控制收入子菜单）。
- 设置页面 18 项 UIUX 修复。

### Added
- **GET `/v1/models`** 端点（Anthropic 兼容格式，`provider/model` 形式）；**网关热重载**（保存后自动 reload）；端口可配置（9527/9528）；启动时自动清理端口占用。

### Removed
- `suanpan/admin.py` 孤儿代码（−336 行，管理面并入 :9528 配置面板）。

## [v0.4.0] — 2026-08-09 — Suanpan AI 路由网关 + Webview 配置 + 架构重构

### Added
- **Suanpan AI 路由网关**（`suanpan/` 子包）：将多家 LLM 后端统一为 Anthropic Messages API，按请求场景（默认 / 后台任务 / 长上下文 / 推理）与模型规则路由转发，支持内联覆盖与 `SUBAGENT-MODEL` 标签。含流式代理、SSE 用量提取、JSONL 用量日志（50MB 轮转）、token 估算、admin 控制台。
- **Webview 配置界面**：以 WKWebView 窗口（`webview_window.py`）+ Web 配置服务（`config_server.py`，`:9528`，JSON CRUD + bearer token + 余额 / 用量查询）替换原生偏好设置窗。自包含 HTML 面板（`config_ui.html`）采用侧边栏分组导航，覆盖供应商 / 路由策略 / 模型规则 / 运行状态。
- **Suanpan 运行时集成**（`suanpan_runtime.py`）：延迟导入，依赖未安装时应用正常启动并提示安装命令。
- **领域术语表**（`CONTEXT.md`）：建立单上下文仓库的领域 SSOT，统一产品结构与路由术语。
- `suanpan.example.yaml` 示例配置。

### Changed
- **架构重构**：`app.py` 瘦身为纯编排器，提取 `config.py`（配置 I/O）、`menu_builder.py`（菜单 UI + 状态图标）、`sys_proxy_controller.py`（系统代理收敛状态机）、`retry_scheduler.py`（SSH 重试退避）、`host_key_flow.py`（主机密钥信任流程）、`subprocess_monitor.py`（`SSHMonitor` / `CaptureMonitor` 共同的子进程生命周期基类）。
- 仓库更名 Magic-AI-Router，反映双产品（Magic Proxy + Suanpan）定位。

### Fixed
- Config server：路径匹配前剥离 query string；webview autoresizing mask 布局问题；bearer token 认证。
- Config UI 全量 bug 修复 + 布局重设计。
- `SSHMonitor` 封装改进与死代码清理；3 项代码审查发现修复。

### Security
- 移除 allowlist 中的 blanket `curl` 权限。

### Development
- 新增 6 个测试文件，覆盖 `config_server`、`ai_capture_addon` 深度边缘用例（+43 测试）、`suanpan/proxy`、`suanpan/router`、`suanpan_runtime`、`subprocess_monitor`。

## [v0.3.7] — 2026-07-25 — 防睡眠与登录启动

### Added
- **防睡眠模式**（`sleep_blocker.py`）：代理运行期间阻止系统进入睡眠，避免 SSH 隧道因休眠断开。
- **登录启动**（`login_item.py`）：可将应用注册为 macOS 登录项，开机自启。
- 新增 `test_login_item.py` / `test_sleep_blocker.py` / `test_config_compat.py` 覆盖新功能与配置兼容性。

## [v0.3.6] — 2026-07-13 — 全新图标与三态连接指示

### Changed
- 全新 macOS 应用图标，以“网络隧道 + 双向代理流量”取代通用网络节点图形。
- 菜单栏使用自定义隧道双向箭头图标，不再使用三节点 SF Symbol。
- 连接状态统一为三种颜色：灰色表示未连接，黄色表示连接中，绿色表示已连接。

### Fixed
- 已停止、连接失败和暂停状态不再误显示为黄色或红色，统一显示为未连接的灰色。
- 构建流程明确使用新版 `.icns`，并验证 44px Retina 菜单栏图标资源随应用打包。

## [v0.3.5] — 2026-07-13 — 安全与运行时稳定性修复

### Security
- 抓包代理强制仅监听 loopback，抓包目录/文件强制使用 `0700`/`0600` 权限。
- HTTP 代理拒绝 LAN 监听，限制请求头大小和读取时间，并过滤代理认证头。
- SSH 首次连接显示 SHA256 主机指纹，确认后使用应用专用 `known_hosts` 严格校验。

### Fixed
- 系统代理在关闭时恢复用户原有配置，不再无条件关闭。
- 修复抓包目录 `~` 展开、公证脚本 mitmdump 验证路径和子进程回收问题。
- 增加配置结构与端口校验，损坏配置会备份而不是导致应用崩溃。

### Development
- 构建统一使用 Python 3.12，新增固定版本开发依赖、macOS CI 和代理核心回归测试。

## [v0.2.0] — 2026-07-08 — 菜单栏增强：About / 实时日志窗口 / 偏好快捷键

### Added
- **About 菜单项**：点击弹窗显示当前版本号与一行应用说明（版本号与 `build.sh` 同源）。
- **实时日志窗口**（`log_window.py`）：菜单"📜 查看日志"打开原生窗口，实时滚动展示代理 / SSH 运行日志，快捷键 `Cmd+L`。
- **偏好设置快捷键**：菜单内可直接打开原生配置窗；配置窗口加宽更易读。
- **分发工具链**：`build_dmg.sh` 将 `.app` 封装为带 Applications 拖拽安装的 `.dmg`；`notarize.sh` 走 Developer ID 签名 + notarytool 公证全流程（`.app` + `.dmg`，凭证存 Keychain）。

### Changed
- 移除"启动终端"功能及 `launcher.py`（不再经 osascript 启动 iTerm2 / Terminal）。

### Fixed
- `build.sh`：`launcher.py` 已在批次1删除但 `--add-data` 仍引用，导致 PyInstaller 构建失败；替换为新模块 `log_window.py`。
- `build.sh`：PlistBuddy 修改 `Info.plist` 后破坏 ad-hoc 签名 seal，新增 `codesign --force --sign -` 重签，避免 arm64 下载后 `killed: 9`。

## [v0.1.2] — 退出死锁修复

- 首个带版本号的发布基线（退出死锁修复）。

[v0.1.2]: https://github.com/benz-ai-x/Magic-AI-Router/releases/tag/v0.1.2
[v0.2.0]: https://github.com/benz-ai-x/Magic-AI-Router/releases/tag/v0.2.0
[v0.3.5]: https://github.com/benz-ai-x/Magic-AI-Router/releases/tag/v0.3.5
[v0.3.6]: https://github.com/benz-ai-x/Magic-AI-Router/releases/tag/v0.3.6
[v0.3.7]: https://github.com/benz-ai-x/Magic-AI-Router/releases/tag/v0.3.7
[v0.4.0]: https://github.com/benz-ai-x/Magic-AI-Router/releases/tag/v0.4.0
