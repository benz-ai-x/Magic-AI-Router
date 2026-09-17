# ADR-005: 多活隧道模型——代理隧道 + 并行转发会话

- 状态：Accepted
- 日期：2026-09-17
- 决策者：tech-lead（用户确认）
- 影响范围：tunnel/connection_coordinator.py、tunnel/ssh_launch.py、tunnel/proxy.py、mpconf/config.py、mpconf/config_state.py、app.py、shellui/menu_builder.py、shellui/config_ui.html、services/config_server.py
- 关联：PR #94（v0.9.0/v0.9.1）；HTML 原型定稿的菜单分离设计

## 上下文

v0.8 及以前是**单活模型**：同一时间只有一条 SSH 隧道（`current_tunnel` 指定），
它同时承载 `-D` SOCKS5（供 :8888 HTTP 代理上游）与 `-L` 端口转发；切换隧道 =
停旧起新。用户实测痛点：需要「服务器 A 跑 SOCKS5 代理、服务器 B 同时跑端口
映射」——单活模型下结构性不可能。单活假设渗透在 ConnectionCoordinator（单
SSHMonitor/RetryScheduler/HostKeyFlow/paused 标量）、app.py tick 的单一
status 消费、菜单/图标/系统代理/防睡眠的投影、以及 v0.8 刚发布的「跨隧道同
本地端口合法（单活豁免）」校验语义里。

## 决策

### 决策 1：角色模型（代理隧道 + 转发会话）

- **代理隧道** = 唯一携带 `-D socks5_port` 的会话（含自己的 -L）。
  主图标/状态行/系统代理/「暂停」语义全部只反映代理会话——
  :8888 数据面的 SOCKS5 上游只有它，语义不必扩散。
  角色字段（v0.9.2 双表示）：`current_tunnel_id`（稳定 id）是唯一真相，
  `current_tunnel` 下标降为旧版本读兼容 + merge 派生投影（每次按 id
  回写）——解析序全链路同一语义：id → 悬空/缺省回退下标 → 首条。
  下标表示的实测教训有二，均已测试钉死：切换流在 restart 前改写下标
  （降级判定读错对象）；设置窗 collect 隐式把「正在查看的隧道」写成
  角色（保存后隧道变成另一连接串）——角色的 UI 写径自此只有显式
  「设为代理隧道」动作（dirty 跟踪，保存后经「重新连接」应用）。
- **转发会话** = 其余隧道的纯 `-L` 会话（`socks5_port=None`，无 -D），可任
  意多条并行。每会话独立实例化 monitor/retry/host-key 三件套（实例隔离已验
  证；单活时代共享一个 HostKeyFlow 的 generation 互吞随之消失）；唤醒事件
  触发全部活跃会话僵尸重建；端口被占时仅该会话独立退避重试。
- **切换代理角色 = 降级续跑**：旧代理隧道有 forwards 则转为转发会话继续跑，
  无则停。降级判定用 `_launched_proxy_id`（实际运行中的隧道）而非配置里的
  `current_tunnel`——切换流在 restart 之前就已把 current_tunnel 写成新值
  （本迭代实测 bug，测试钉住）。
- **`forward_autostart` 持久字段**：转发会话随应用启动自动恢复
  （`apply_autostarts` 收敛补启；代理隧道不受此字段影响）。

### 决策 2：[破坏性] 转发本地端口全局唯一

多活下任意隧道可并行运行，两条隧道抢同本地端口会让双方在
`ExitOnForwardFailure=yes` 下互顶成死循环重试。v0.8 的「跨隧道同端口合法」
以单活为前提，随多活**作废**：prepare 与 JS validateConfig 双层拦全局唯一
（含保留端口）。0.8.0 与 0.9.0 同日发布，存量影响≈0。

### 决策 3：菜单与守卫重连的呈现/触发面

- 菜单五组（HTML 原型定稿）：状态区（SF Symbols 着色圆点）→ **代 理 ▸**
  （只管 -D 会话：启停/系统代理/角色单选/经代理启动）→ **端口映射 ▸**（只管
  -L 会话：代理隧道显示「随代理运行」信息行，其余各自启停/单会话重连）→
  AI 路由/抓包 → 系 统 ▸ → 精简页脚。代理与端口映射分离为两个独立菜单是
  用户在原型体验后确认的方向。
- 守卫重连逐隧道定向：`reconnectProxy {if_connected, tunnel_id?}`——运行中
  的转发会话按各自连接态守卫重建，**未运行/未连接的绝不因保存配置被拉起**。
  `restart_forward` 持显式重连语义：会话存在即重建（error/退避态同——评审
  Spec-A 修正，曾按 connected 门控造成死按钮）。

## 否决

- **对称多活（任意隧道可带 -D）**：:8888 只有一个 SOCKS5 上游，「谁在跑 -D」
  必须有唯一答案；按先连先得裁决会引入不可预测性。
- **per-tunnel socks5 端口 + 代理路由表**：为不存在的需求引入配置面与路由
  复杂度。
- **转发会话参与「暂停」**：暂停是代理语义（停 :8888 上游的重试）；转发
  会话各自独立重试，不共享开关。
- **保留 v0.8 跨隧道同端口豁免 + 运行时互踢检测**：把冲突留给运行时等于
  让两条 ssh 死循环互顶再靠告警兜底——保存前拦下是唯一廉价闭合点。

## 测试

- `tests/test_connection_coordinator.py`：多会话并行启停/守卫拒绝/收敛
  （check 即 reconcile）/降级续跑三态/唤醒重建/autostarts 跳过代理与已跑/
  显式重连语义（Spec-A）。
- `tests/test_config_state.py` / `tests/js/model.test.mjs`：跨隧道唯一双侧
  同口径（含布尔端口、保留端口）。
- `tests/test_menu_builder.py`：代理/端口映射两子菜单结构逐项钉住。
- 真机双服务器冒烟：A 代理 + B 转发并行、断 A 不影响 B、干净拆除。
