# ADR-011: 服务器中心配置模型（Server → Service → Instance）

日期：2026-09-25
状态：已接受（v2 schema，随 v0.13.0 落地）

## 背景

v1 配置以 `tunnels[]` 为中心：每条"隧道"把 SSH 连接参数、端口转发行、NFS 节捆在一个扁平对象里。这带来三个问题：

1. 同一台远程服务器配两次 = 两条孤立"隧道"，连接参数重复维护；
2. 设置窗被模型塑形：「隧道」页编辑连接+转发、「远程挂载」页复用同一列表编辑 NFS——用户被迫按"配隧道"思考；
3. 新服务类型（OpenVPN 等）没有挂靠点——只能再开一个复用隧道列表的页面。

## 决策

配置 schema 换轴为**服务器中心**的三层模型：

```
服务器（servers[]：id + name + ssh{连接参数}）
 └─ 服务（services.{ssh, nfs, …}：这台机器能提供什么）
     └─ 实例（forwards[] / mounts[]：每个服务的具体用途行）
```

六项拍板（用户决策）：

1. **实例 = 用途行**：每条 -L 转发 = 转发实例、-D 代理 = 唯一代理实例、每个挂载 = NFS 实例。**运行时会话模型不变**（一服务器一 SSH 会话承载其全部转发）。
2. **自动迁移 + 保 id**：v1 `tunnels[]` → v2 `servers[]` 一次性迁移；稳定 id（`t-<sha1>@host:port`）逐字节保留——Keychain 槽位（`tunnel:{id}`）与运行时会话身份随之保值。新增 `schema_version` 显式版本化。
3. **代理角色单一真相**：`proxy_server_id` 取代 v1 的 `current_tunnel_id` + `current_tunnel`（下标投影）双表示。
4. **菜单保持功能分组**（操作面与配置面分离：菜单按用户想做的事分组，设置窗才是服务器中心的配置面）。
5. **设置窗「服务器」单视图吞并 隧道+远程挂载 两页**（后续批次）。
6. **服务卡 + 一键检测**：每服务一张卡（配置态徽标 + 手动「检测服务」探活——SSH 复用 probe、NFS 复用 check_remote、OpenVPN 新探针）；OpenVPN 本轮仅作枚举占位。

## 后果

- 服务器形状知识的单一归宿在 `mpconf.config`（归一化访问器 servers/server_by_id/proxy_server/server_forwards/server_nfs）；tunnel/mount 域按分层 DAG 不 import mpconf，用本地微访问器。
- `mpconf.validate.tunnel_rows_errors` → `server_rows_errors`（校验文案 隧道→服务器，镜像语料同步）。
- Keychain 账户字符串是兼容契约——`tunnel:{id}` 与 `user@host:port` 逐字节不变，只换字段读取路径（ssh 节）。
- 未保存的新服务器（无 id）不能被设为代理服务器（角色标记按钮隐藏）——id 是角色的寻址前提。

## OpenVPN 备忘（占位，正式实施时补决策记录）

前期可行性共识（2026-09 评估，未立项）：VPN 与 SSH 隧道**二选一互斥**；互斥粒度必须是全部 SSH 会话（-D/-L/NFS），否则路由坑复活；技术路线 = openvpn 子进程 + management interface + 模式 choke point + 拆除屏障；断开不自动回切 SSH。本轮仅预留服务类型枚举与远端探测（`command -v openvpn`）。
