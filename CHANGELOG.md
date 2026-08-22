# Changelog

All notable changes to Magic-AI-Router are documented here.
Format based on [Keep a Changelog](https://keepachangelog.com/), adheres to [SemVer](https://semver.org/).

## [v0.6.1] — 2026-08-22 — 套餐三窗口配额 + 文档整固

### Added
- **余额速览三窗口配额（#41）**：GLM/Kimi 等套餐类供应商统一展示 5小时/每周/每月。月度「两者结合」——API 有月度（Kimi totalQuota）用供应商口径，否则聚合本网关 usage.jsonl（UI 标注「每月（网关）· N 次」，无本地数据补 0 行）；GLM Max 的 TIME_LIMIT 工具时长配额标注「每月·工具」，不抑制本地 token 行。GLM 配额显示 nextResetTime 重置时间，所有 reset 统一转 CST。`fetch_usage` 新增 `month` 范围（CST 自然月），`/api/usage?range=month` 随之可用。

### Fixed
- **agent.md 六处陈旧**：`listen_port` 整型示例（旧 `listen` 读时兼容）、usage 日志真实路径、Docker 取 token 路径（`suanpan.sh config-ui`）、登录页行为、`range=month`、菜单顺序对齐代码装配序；CLAUDE.md 测试口径改 `node --test tests/js/*.test.mjs`（node ≥26 目录模式失效）。

### Development
- README 补「方式四：Linux / 无 GUI（Docker）」部署说明。

## [v0.6.0] — 2026-08-21 — Docker 版：Linux 无 GUI 部署 + Web 管理

### Added
- **Suanpan 网关 Docker 版（#22 / PR #35）**：python:3.12-slim 镜像 + compose + `docker/suanpan.sh` 管理脚本（up/down/status/logs/sync/config-ui）；Linux 无 GUI 跑网关（:9527），`sync-claude-code` 一键写入 Claude Code 配置；PyObjC Security stub 让 macOS 专属 import 链在 Linux 容器零修改通过；首启引导默认配置落 /data 卷。
- **Docker 配置页面 :9528（PR #37）**：复用 config_server 的完整 Web 管理界面（供应商/路由/统计/余额），token 与 sync 同源零新 secret；Linux 无 Keychain 经守卫走 SP-only 保存。
- **登录页（PR #39）**：裸 GET `/` 无凭证返回自包含登录页——浏览器打开 :9528 输 token 即入；`/api/*` 的 401 保持纯 JSON，macOS 桥接带 Bearer 零回归。

### Fixed
- **配置页保存后网关热重载（PR #40）**：GatewayRunner 线程化 uvicorn + `on_sp_saved` 回调接线——Docker 版保存配置即时生效，无需重启容器。

### Development
- 新增 docker-deploy.md 部署文档，两轮 writing-for-agents 收紧（PR #36/#38）。

## [v0.5.0] — 2026-08-21 — 安全·事务·韧性：16 issue 加固收口

### Added
- **实例所有权 InstanceOwnership（#3 / PR #18）**：pid+启动时间双匹配抗 PID 复用，取代 basename 误杀；O_EXCL 原子创建/陈旧接管/release。
- **AuthenticatedHttpClient（#4 / PR #21）**：认证出站统一 adapter——跨 origin 重定向一律拒绝、HTTPS→HTTP 降级必拒、1MB 响应上限；凭证不出原始 origin。
- **ConfigStateStore（#6 / PR #23）**：配置持久化事务边界——load 四态 / prepare 全量校验 / commit（journal+MP+SP+Keychain 次序）/ recover 幂等重放，收编 PUT/首创建/启动恢复全部路径。
- **RetryPolicy（#7 / PR #24）**：流式代理有界重试——pre-send 证明或幂等才重试；非幂等 POST 送达后不明即不重放。
- **Provider/Tunnel 稳定持久 id（#8 / PR #26）**：凭证与可编辑字段解耦；live PUT 掩码恢复 + re-pin 串线守卫。
- **本地客户端 token（#9 / PR #34）**：sync 与 config-ui 同源同值，落配置卷跨容器重建稳定。
- **header-only token（#10 / PR #25）**：token 只进 Authorization 头与 `cfgsess` HttpOnly SameSite=Strict 会话 cookie；query-string 认证删除。
- **UsageExtractor 有界线性增量 scanner（#13 / PR #30）**；**UsageSink 吞错韧性 + ProviderPrewarmer 有界预热（#15 / PR #31）**。
- **发布工程单一契约（#14）**：资源清单 + requirements-lock 锁定依赖 + CI 门禁（lock 漂移校验）。

### Fixed
- **抓包资源契约（#2 / PR #17）**：`resolve_capture_resources` 收编 mitmdump 三级链 + addon 校验 + 目录 preflight；frozen/dev 双态冒烟判据单一归宿。
- **明文 HTTP 逐请求归属（#5 / PR #19）**：跨 origin 安全重连状态机——keep-alive 连接绝不静默误投他站；1xx 接续、拒绝先于转发。
- **AsyncRuntime 锁下状态机（#12 / PR #28）**：竞争窗口与 coroutine 泄漏封死；start() 保 bool 契约（拒绝返回 False 不抛异常）。
- **抓包默认流式 + 单一聚合预算 + store 收敛（#11 / PR #27）**。
- **HTTP framing 加固收尾（#20 / PR #32）**。

### Changed
- **文档收口（#16 / PR #33）**：漂移修正 + 历史标记 + 高风险守卫；CLAUDE.md 按 writing-for-agents 杠杆修整。

### Development
- lifecycle_runtime 测试覆盖率 86%→100%。

## [v0.4.11] — 2026-08-19 — 仓库重建 + 全域包架构

> 仓库历史自 2026-08-19 重建起算（初始提交 VERSION=0.4.9，bundle ID 更换为 `com.benzai.magic-ai-router`，remote 迁往 benz-ai-x）；此前条目中的 issue 编号指向重建前仓库。

### Changed
- **根目录全域包整理**：38 个模块归入 6 个域包（mpconf/tunnel/shellui/capture/sysctl/services），根目录 54→14→9 项，可重建产物清除。
- **架构候选落地**：saveAll 状态机下沉为 LAYER 1 深模块 saveFlow；ConfigServer 回调改 server 实例注入（并行测试不再串话）；ServiceCoordinator 升格 LifecycleRuntime（start_all/quit 顺序契约）。

### Development
- ADR 重编号为连续序列（022-025 → 001-004），全量引用同步；CI 路径同步 scripts/ 迁移，sit 用例 import 补域包迁移。

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
