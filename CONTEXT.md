# Magic-AI-Router — 领域术语表

## 产品结构

### Magic-AI-Router

macOS 菜单栏应用（壳）。承载两个独立产品：Magic Proxy 和 Suanpan。用户通过菜单栏图标与之交互。打包为原生 .app（LSUIElement=true，不显示 Dock 图标）。

### Magic Proxy

代理产品。通过 SSH 动态端口转发提供 HTTP→SOCKS5 代理，附带 TLS 抓包能力。与 Suanpan 完全解耦——不依赖 AI 路由，AI 路由也不依赖它。

### 本地代理（Local Proxy）

Magic Proxy 的核心服务。监听在 `:8888`，接收 HTTP/HTTPS 代理请求，通过 SOCKS5（SSH 隧道）转发到远端。浏览器和 CLI 工具指向这里。

### 系统代理（System Proxy）

macOS 全局代理设置（networksetup）。开启后系统内所有应用自动走本地代理。Magic Proxy 以事务方式管理——连接时设置、断开时恢复用户原有配置。

### 应用代理（App Proxy）

针对单个 Chromium 应用的代理（如 ChatGPT/Claude/Discord）。通过 `--proxy-server` 启动参数实现，不修改系统设置，仅影响该次启动的实例。

### 隧道（Tunnel）

一条 SSH 动态端口转发连接（`ssh -D`），包含 SSH 连接本身和它在本地创建的 SOCKS5 监听。两者共生——SSH 断开则 SOCKS5 随之失效。支持配置多条隧道，同一时间只有一条活跃（当前隧道）。切换隧道 = 关闭旧 SSH 连接，建立新 SSH 连接。

### 抓包模式（Capture Mode）

实验性功能。启动 mitmdump 作为 TLS MITM 代理（`:8080`），级联到本地代理（`:8888`）。系统代理在抓包期间指向 mitmdump 而非本地代理。

**拦截范围与记录范围不同**：所有 HTTPS 流量都经过 mitmdump 解密转发，但只有已知 AI API（OpenAI/Anthropic/DeepSeek/豆包/Qwen/MiniMax）的请求/响应被记录到 JSONL 文件。其他流量静默放行，不落盘。

当前支持 6 家 AI 的识别与抽取，未来可扩展更多模型。首次使用需在 macOS 钥匙串信任本地根 CA。

### 设置窗桥接（Settings Window Bridge）

偏好设置窗（WKWebView）内 JS 与原生 Python 之间的消息协议。单一 `bridge` 通道，消息为 `{type, payload}` JSON。协议核心在 `shellui/bridge_protocol.py`（纯 Python，可单测）；`shellui/webview_window.py` 仅为 ObjC 薄 adapter。

约定：Python 只递数据，绝不命名 JS 的 DOM 选择器、绝不手写 JS 源码（`json.dumps` 是唯一转义层）；dirty 等界面状态以 JS 为真相源，Python 侧仅为镜像，关窗拦截（`windowShouldClose_`）读镜像。

### 保存流（Save Flow）

设置界面的两阶段保存状态机（`shellui/config_ui.html` LAYER 1 的 `saveFlow`）：校验 → 保存网关配置（PUT /api/state）→ CC 同步预览（失败关闭）→ 用户确认弹窗 → 写入 Claude Code → baseline 推进 → 分支 toast。一切副作用（fetch/弹窗/toast/baseline 写回）经 deps 注入——真实现与测试桩是同一 seam 的两个 adapter，node 测试直接钉住状态机。调用方（LAYER 2 的 `saveAll`）只做表单 collect 与接线；dirty 真相源在 JS，经 `dirtyProjection` 与保存流同口径。

### 服务生命周期（LifecycleRuntime）

后台服务的单一编排点（`services/lifecycle_runtime.py`）：构造五条服务线（Suanpan 网关 / 抓包 / 系统代理 / 防睡眠 / 配置服务）并持有启停顺序契约——`start_all()`（实例锁单胜守卫 → 端口占用报告 → 配置服务 → 网关自启）与 `quit(ssh_stop)`（系统代理恢复 → SSH 停止 → 服务线 → 配置服务，SSH 停止以回调注入）。「抓包正在运行」在此持有单一投影，对 SystemProxyController（元组）与 ConfigServer（布尔）内部适配；Suanpan 保存后的 reload 链内化于模块内。app.py 经属性面（`suanpan` / `capture_ctrl` / `sys_proxy` / `capture` / `config_server`）引用子模块。

### 逐请求归属（Per-request Origin Binding）

明文 HTTP 代理的消息定界契约（`tunnel/http_framer.py` + `tunnel/proxy.py::handle_http`）：同一客户端 keep-alive 连接上的每条请求独立解析并验证 authority；upstream 连接只允许同 origin 复用，跨 origin 先关旧连再安全重连。body 定界支持 Content-Length 与 chunked，pipelined 字节由 StreamReader 自然缓冲留给下一轮；无法定界的消息按「本消息后关闭」安全拒绝，绝不带着未定界状态复用连接。CONNECT 隧道走独立直通路径，不经此状态机。

### 实例所有权（InstanceOwnership）

进程所有权的可验证记录（`sysctl/instance_owner.py`）：锁记录含 pid / 进程启动时间 / exe / nonce，经 O_EXCL 原子创建；启动时间入锁抵抗 PID 复用。端口占用只是发现线索、启动期一律仅告警永不发信号——活旧实例由单胜守卫拦截（app 弹窗退出），死旧实例只剩陈旧锁（acquire 接管清理）；并发启动单实例守卫由 `LifecycleRuntime.start_all` 的锁获取承担，失败方不触碰成功方的锁。basename 命令行启发式已删除。

### 资源契约（CaptureResources）

抓包模式的资源单一入口（`capture/resources.py`）：`resolve_capture_resources(cfg)` 解析并验证 mitmdump 二进制（env 覆盖 → frozen bundled → PATH 三级链）、addon 脚本（存在 + 可读）与抓包目录（可建），失败抛带可行动中文文案的 `CaptureResourcesError`。控制器只消费已验证的 `CaptureResources` 三元组，不自行拼接文件名；frozen 态资源为扁平布局（`--add-data` dest="."），addon 导入需包限定/扁平双态兼容。启动冒烟判据（宽限秒数 + 加载错误标记）以 `smoke_capture_boot`/`SMOKE_*` 为单一归宿，dev SIT 与 build.sh 打包冒烟共用。

### 配置存储（ConfigStore）

两个配置文件（`~/.magic-proxy.json` 与 `~/.suanpan.yaml`）路径的唯一权威注册表 + 共享安全写管线，位于 `mpconf/config_store.py`。所有读取方在调用时从 `PATHS` 注册表取路径——测试只需 `patch.dict(config_store.PATHS)` 单点重定向，任何测试都无法再写真实配置文件。

统一语义：原子写（mkstemp + chmod 0600 + os.replace）；Suanpan 侧另保留覆盖前 `.bak` 备份。配置内容的语义（mp 的 merge/migrate、sp 的 pydantic 校验与密钥掩码）不在此——留在 `mpconf/config.py` 与 `suanpan/config.py`。

### Suanpan（算盘）

AI 路由产品。将多家 LLM 后端统一成 Anthropic Messages API，按请求场景和模型规则路由转发。独立运行在自己的端口上，不经过 SSH 隧道。

### 供应商（Provider）

Suanpan 的 LLM 后端（如 DeepSeek、GLM、Kimi）。每个供应商有 Base URL、API Key（或环境变量）、认证头和启用状态。需兼容 Anthropic Messages 协议。

### 路由场景（Routing Scenario）

Suanpan 按请求特征划分的路由类别。当前实现两级：
- **模型规则**（rule）——按模型名前缀匹配转发
- **默认路由**（default）——未命中规则时的兜底

> 历史版本曾有 background / long_context / think 三个场景路由，已在重构中移除（见 commit `755596e`、`9b3183b`）。`RouterConfig` 仅保留 `default` 字段。

> 注意：代码 `RouteDecision.scenario` 字段含义更广——它记录"本次请求由哪条链路命中"（取值含 `inline` / `subagent` / `rule` / `default`），实为**路由来源**而非领域意义上的"路由场景"。该字段已落盘进 `usage.jsonl`，不宜改名。

### 模型规则（Model Rule）

按请求中的模型名前缀匹配并转发到指定目标（如 `claude-sonnet → deepseek/deepseek-v4-flash`）。规则按顺序检查，命中第一条后停止。

### 内联覆盖（Inline Override）

客户端通过在请求的 model 字段中包含 `provider/model`（如 `deepseek/deepseek-chat`）直接指定后端，绕过所有规则。设计目的：让 Claude Code 在不同上下文中精确选择模型（子代理用便宜模型、主任务用强模型）。

### SUBAGENT-MODEL 标签

客户端通过在 system prompt 中嵌入 `<SUBAGENT-MODEL>provider/model</SUBAGENT-MODEL>` 标签指定后端，效果同内联覆盖。优先级仅次于内联覆盖。

### 路由优先级

请求按以下顺序判定，命中第一条即停止（高 → 低）：

**内联覆盖** → **SUBAGENT-MODEL 标签** → **模型规则** → **默认路由**

前两者（转义路径）允许客户端绕过用户配置的路由；后两者是配置路由，按既定链路判定。

> 历史版本曾有自定义路由、长上下文、后台任务、推理请求四个场景，已在重构中移除（见 commit `9b3183b`、`755596e`）。
