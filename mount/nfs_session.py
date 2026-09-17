"""专用 NFS 转发会话：一条纯 -L（local_port → 127.0.0.1:2049）承载本
隧道全部 NFS 挂载（NFSv4 单端口，多个导出共享同一条隧道）。

镜像 tunnel/connection_coordinator._ForwardSession 的三件套（SSHMonitor
+ RetryScheduler + HostKeyFlow），但不进 ConnectionCoordinator 的转发
会话注册表——挂载生命周期（先卸载后断隧道）由 MountCoordinator 单独
编排，与用户的端口转发会话互不干扰。

会话 argv 只携带 NFS 这一 条 -L：用户自己的转发行由他们自己的会话
（代理会话/转发会话）持有——同端口双进程绑定会因 ExitOnForwardFailure
互顶死循环（prepare 的端口冲突校验拦同端口配置）。
"""
import logging

from tunnel.proxy import SSHMonitor
from tunnel.retry_scheduler import RetryScheduler
from tunnel.host_key_flow import HostKeyFlow

logger = logging.getLogger("magic-proxy.nfs-session")

NFS_REMOTE_PORT = 2049


class NfsSession:
    """一条隧道的 NFS 转发会话（纯 -L，无 -D）。"""

    def __init__(self, tunnel_id, local_port, log_sink, tunnel_fn,
                 password_fn):
        self.tunnel_id = tunnel_id
        self.local_port = local_port
        self.monitor = SSHMonitor(line_sink=log_sink)
        self.retry = RetryScheduler()
        self._tunnel_fn = tunnel_fn      # () -> 原始 tunnel dict or None
        self._password_fn = password_fn  # (tunnel) -> str
        self.host_key = HostKeyFlow(
            ssh_monitor=self.monitor,
            get_tunnel=tunnel_fn,
            get_socks5_port=lambda: None,  # 转发模式无 -D
            get_password=lambda: (
                password_fn(tunnel_fn()) if tunnel_fn() else ""),
            on_connect=self._start_now,
            on_reconnect=self.connect,
        )

    def _injected_tunnel(self):
        """用户隧道副本 + 仅含 NFS 的 forwards（见模块头：绝不双进程绑同口）。"""
        tunnel = self._tunnel_fn()
        if tunnel is None:
            return None
        row = {"local_port": self.local_port, "remote_host": "127.0.0.1",
               "remote_port": NFS_REMOTE_PORT}
        return {**tunnel, "forwards": [row]}

    def connect(self):
        """发起连接序列：重试计数清零 + host-key 信任检查（首连信任流）。"""
        self.retry.cancel()
        self.host_key.start_check()

    def _start_now(self):
        original = self._tunnel_fn()
        injected = self._injected_tunnel()
        if injected is not None:
            self.monitor.start(injected, None,
                               self._password_fn(original))

    def reconnect_now(self):
        """唤醒等外部事件：connected 视为僵尸链路主动重建再连。"""
        if self.monitor.status == "connecting":
            return
        if self.monitor.status == "connected":
            self.monitor.stop()
        self.connect()

    def stop(self, blocking=True):
        self.retry.cancel()
        self.host_key.cancel()
        self.monitor.stop(blocking=blocking)
