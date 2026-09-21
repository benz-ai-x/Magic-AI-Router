# ADR-009: 设置界面维持 Web 架构 + 配置面端口生命周期收敛

- 状态：Accepted
- 日期：2026-09-20
- 决策者：tech-lead（用户确认「维持 Web，仅补强」+「默认关 + 手势自动开」）
- 影响范围：`services/lifecycle_runtime.py`（持有状态机 + 条件启动）、`app.py`（三入口收敛）、`shellui/menu_builder.py`（系 统 ▸ 开关）、`shellui/config_ui.html`（系统页开关行）、`mpconf/config.py`（`config_api_enabled`）、`shellui/webview_window.py`（关窗回调）
- 关联：ADR-000（配置界面选型行）；#10（URL 永不带凭证）；v0.10.1 浏览器复制回退（`GET /api/agent-instructions`）

## 上下文

用户提出「偏好设置用 Web 页还是原生表单好」的比选讨论，并指出实际痛点：
**设置窗消耗了常驻网络端口**（:9528 随应用启动监听，附带 token/host 守卫一
整面）。

调研确认的事实基线：

- v0.4.0 曾用 WKWebView + Web 配置服务**取代原生 `prefs.py` 配置窗**（已
  删）——原生表单是被实践淘汰过的路线，只是当年的决策记录随 2026-08 仓库
  清理丢失（编号 001–021 的旧 ADR 全删）。
- 现状 `config_ui.html`：2300 行单文件三层架构（纯逻辑层 584 行被 127 个
  node 测试钉住）；原生依赖面薄且闭集（7 种 bridge 消息）；数据面 16 个
  REST 端点纯 HTTP，浏览器直开可用。
- 配置面 Web 化的三条护城河：**Docker 形态的唯一 UI**（容器无原生窗口，
  `bind_host="0.0.0.0"` + 固定 token）；**AI 可操作面**（agent.md + token
  API 是 README 头牌卖点）；**迭代速度与可测性**（PyObjC AppKit 重写估
  2–4 倍代码量，UI 测试基建近乎从零造）。

## 决策一：维持 Web 架构，不迁移原生表单

三条护城河是产品级能力而非实现细节；原生方案的优势（观感、无 bridge、
无 token）要么是低频场景收益，要么已被「菜单栏管高频、Web 管全量配置」的
现有混合模式覆盖。迁移是高成本、高回归风险、附带产品能力阉割的重写。

**备选未采——WKURLSchemeHandler 零端口方案**：原生设置窗经自定义 scheme
（如 `mpconfig://`）由进程内 handler 供页，彻底去 TCP，:9528 仅剩浏览器/
agent 场景。未采原因：默认关（决策二）已覆盖端口常驻痛点的绝大部分，而
scheme handler 需要新 ObjC 适配层 + cookie/鉴权重构。**触发条件**：若未来
要求「设置窗打开期间也不占 TCP 端口」（如端口审计严格的环境），重开此案。

## 决策二：配置 API（:9528）默认不常驻，按持有者生命周期监听

持有状态机（`lifecycle_runtime.config_server_wanted` 纯函数，三输入）：

```
server_on = 设置窗开着(window_open) or config_api_enabled or 复制指令闩锁(copy_latch)
```

| 持有者 | 语义 | 释放时机 |
|---|---|---|
| 设置窗 | 打开「偏好设置」自动起服务 | 窗口真关闭（`windowWillClose`，dirty 守卫否决不算） |
| `config_api_enabled` | 磁盘持久开关，双入口（系 统 ▸ 菜单 checkbox + 设置窗系统页开关行，`prevent_sleep` 同款） | 用户显式关闭，立即释放 |
| 复制指令闩锁 | 点「复制 AI 助手指令」自动起并**会话内保持**（指令里的 curl 要能被 agent 立即使用） | 应用退出（不做超时——agent 何时回连不可知） |

要点：

- **UI 保存整替防护**：`prepare(mp=…)` 对传入 dict 整替磁盘段——新键必须
  进 UI collect 回传（`collectSystem`），否则设置窗一保存就抹掉开关状态。
  这是 `prevent_sleep`/`launch_at_login` 既有模式的沿用。
- **UI 保存线程安全**：`on_mp_saved` 在 config server 线程触发，此时设置窗
  必然开着（持有者在场），同步只会"保持运行"，不会在响应未写出时 stop。
- **启动期报告**：端口占用报告对本次不绑定的配置端口跳过（`None` 语义）。
- **Docker 豁免**：容器形态直接构造 ConfigServer 恒常驻，不经过
  LifecycleRuntime 的持有状态机——`config_api_enabled` 键对它无意义。
- `agent_instructions()` 尾注（仅 macOS 形态）与 agent.md 认证节说明服务
  按需开启；连接被拒先让用户开服务。

## 后果

- 默认态零配置面监听：`lsof -i :9528` 冷启动为空；agent/浏览器用户多一步
  「先开服务」（复制指令手势已自动化这一步）。
- `:9527`（suanpan 网关）与 `:8888`（HTTP 代理）是产品功能端口，不属配置
  面，不在本决策范围。
- 首次打开设置窗多 ~10ms 服务启动延迟（毫秒级，无感）。
