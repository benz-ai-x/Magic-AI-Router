# ADR-007: NFSv4 over SSH 隧道挂载

- 状态：Accepted
- 日期：2026-09-17
- 决策者：tech-lead（用户确认四项关键决策）
- 影响范围：mount/（新域）、tunnel/ssh_launch.py、mpconf/config.py、mpconf/config_state.py、shared/keychain.py、app.py、shellui/menu_builder.py、shellui/config_ui.html、shellui/bridge_protocol.py、services/config_server.py、services/lifecycle_runtime.py
- 关联：ADR-005（多活会话三件套的镜像）；架构守卫白名单边 ("mount", "tunnel")

## 上下文

用户需要把远程服务器目录当本地磁盘用（开发文件直读直写）。备选：
SSHFS（需 macFUSE 内核扩展，上游停止维护，系统升级易碎）、rsync/syncthing
（同步非挂载）、NFSv3 over 多端口隧道（portmapper/mountd/lockd 动态端口，
需钉死 4-5 个端口，脆弱）。**NFSv4 单 TCP 端口 2049、无 mount 协议**，一条
`ssh -L` 隧道即可承载，且 macOS 自带 NFSv4 客户端（mount_nfs）——本地零依赖。

## 决策

### 决策 1：仅支持 NFSv4（用户确认）

单端口 = 单隧道承载本隧道全部挂载（多导出共享同一条 -L）。NFSv3 明确
不支持。挂载参数恒 `vers=4,port=<local_port>,tcp,hard`。

### 决策 2：独立专用 NFS 会话（不注入用户的 forwards）

镜像 ADR-005 的 `_ForwardSession` 三件套（SSHMonitor + RetryScheduler +
HostKeyFlow）建 `mount/nfs_session.py`，注入的 tunnel 副本**只携带 NFS 这
一条 -L**——用户自己的转发行由他们自己的会话持有，同端口双进程绑定会因
ExitOnForwardFailure 互顶死循环（prepare 的端口冲突校验拦同端口配置）。
代价：同一 host 可能跑两条 ssh 进程（host-key 信任流共享 known_hosts，
无额外交互）。

### 决策 3：远程提权——sudo -S 密码走 stdin 管道（用户确认）

`run_remote`（ssh_launch 新增）与 probe 同源的 host-key 三件套 + 认证
注入；sudo 密码经 ssh stdin 管道传给远程 `sudo -S -p ''`——本地与远程
argv 均不出现密码（sshpass 的 pty 回显由返回前 scrub 兜底）。凭据解析序：
UI 显式输入 > 密码登录复用隧道密码 > Keychain sudo 槽（`nfs-sudo:` 账户，
密钥登录的隧道输入一次后免输）；空 = `sudo -n`（NOPASSWD 服务器）。

### 决策 4：本地提权——一次性 sudoers.d 规则（用户确认）

mount_nfs/umount 需要 root。首次挂载经 osascript 管理员授权写
`/etc/sudoers.d/magic-router-mount`（base64 过双层引号 + `visudo -cf`
先校验），规则仅限两个二进制（任意参数）；顺带创建 /Volumes 挂载点。
挂载状态真相源 = `/sbin/mount` 输出（无需 sudo），不信任单次命令返回码。

### 决策 5：hard 挂载 + 断线自动卸载重挂（用户确认）

hard 保证写完整；MountCoordinator（tick reconcile，同 check_forwards
的「check 即收敛」风格）在会话非 connected 且已挂载时立即强制卸载
（防 Finder 卡死），恢复后自动重挂 auto_mount 项。退出路径先
`unmount_all`（阻塞上限 8s）再断会话。mount/umount 子进程全在 worker
线程（主线程 tick 绝不阻塞）；失败退避 30s，显式点击绕过退避。

### 决策 6：远程只动应用拥有的文件

`/etc/exports.d/magic-router.exports`（整体重写、幂等）+ 目标目录
`mkdir -p`；导出恒绑 `127.0.0.1` + `insecure` 必须（sshd 端口转发源端口
非特权）。root 脚本经单次 `sudo sh -c <shlex.quote(script)>` 执行（密码
只喂一次）；用户路径全部 shlex.quote。

## 后果

- 正面：本地零内核扩展；服务器侧不对公网暴露 NFS；与多活转发/代理并行。
- 负面/已知限制：吞吐受 SSH 加密限制（日常开发够用，非大 IO 场景）；UID
  映射不一致时属主显示怪异（可开 `squash_to_ssh_user` 用 all_squash 统一
  到 SSH 用户）；挂载点默认 /Volumes/<名>（用户可改任意绝对路径）。
- 防睡眠聚合纳入挂载状态（hard 挂载睡着 = Finder 卡死）。
- 挂载启停/远程安装是运行时意图不落盘（bridge 消息 → MountCoordinator）；
  持久意图只有配置里的 auto_mount。
