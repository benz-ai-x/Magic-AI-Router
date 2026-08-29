"""SSH 调用策略单一归宿：argv 构建、一次性探针、stderr 失败分类。

「按我们的策略调用 ssh」只在本模块存在一份：

- host-key 三件套（StrictHostKeyChecking=yes + 应用专用 known_hosts +
  GlobalKnownHostsFile=/dev/null）——两个调用方行为恒等；
- 认证注入：sshpass-via-fd（密码永不出现在 argv / ps）或 key 认证 -i 传参；
- stderr → 中文短语的失败分类表（有序，变更先于未信任）。

两个调用方：
- tunnel/proxy.py::SSHMonitor.start —— 长驻隧道，消费 build_tunnel_command；
- services/config_server.py::test_tunnel —— 一次性探针，走 probe() 全包。
"""
import os
import subprocess
from dataclasses import dataclass
from typing import Optional

from tunnel import host_key

# 探针的硬上限：ssh 自己的 ConnectTimeout 只管 TCP，这个管其余一切
# （sshpass 提示等待、密钥交换卡住），HTTP 请求绝不无限挂起。
PROBE_TIMEOUT = 15

_FAILURE_PHRASES = (
    # 顺序敏感：密钥已变更的 stderr 同时含 "Host key verification failed"，
    # 更具体的模式必须先命中。
    ("REMOTE HOST IDENTIFICATION HAS CHANGED", "主机密钥已变更，请先从菜单栏处理告警"),
    ("Host key verification failed", "主机密钥未信任，请先从菜单栏连接一次完成信任"),
    ("Permission denied", "认证失败：密钥或密码被拒绝"),
    ("Connection refused", "连接被服务器拒绝"),
    ("Could not resolve hostname", "无法解析服务器地址"),
    ("Connection timed out", "连接超时"),
    ("No route to host", "无法路由到服务器"),
    ("Network is unreachable", "网络不可达"),
)

_HOST_KEY_CHANGED_PHRASE = _FAILURE_PHRASES[0][0]


def describe_failure(stderr):
    """Map raw ssh stderr to a short Chinese phrase for the config UI."""
    text = (stderr or "").strip()
    for needle, phrase in _FAILURE_PHRASES:
        if needle in text:
            return phrase
    first_line = text.splitlines()[0] if text else "未知错误"
    return f"连接失败：{first_line[:120]}"


def host_key_changed(stderr):
    """True when ssh exited because the server's host key changed.

    与 describe_failure 共用同一张有序分类表——调用方不再自行字符串匹配。
    """
    return _HOST_KEY_CHANGED_PHRASE in (stderr or "")


@dataclass
class SshCommand:
    """一次 ssh 调用的完整描述。

    cmd 是真实 exec argv；display_cmd 仅供日志展示（密码 fd 打码为 ***）；
    password_fd 由调用方在 spawn 完成后以 close_password_fd() 释放。
    """

    cmd: list
    display_cmd: str
    destination: str
    pass_fds: tuple = ()
    password_fd: Optional[int] = None

    def close_password_fd(self):
        """释放密码管道读端；幂等，重复调用与 OS 错误都不抛。"""
        if self.password_fd is None:
            return
        try:
            os.close(self.password_fd)
        except OSError:
            pass
        self.password_fd = None


def _destination(tunnel):
    user = tunnel.get("ssh_user", "")
    host = tunnel["ssh_host"]
    return f"{user}@{host}" if user else host


def _host_key_args():
    return [
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"UserKnownHostsFile={host_key.KNOWN_HOSTS_PATH}",
        "-o", "GlobalKnownHostsFile=/dev/null",
    ]


def _with_auth(tunnel, ssh_args, password, extra_auth_args=()):
    """把认证注入策略套到 ssh_args 上，返回完整的 SshCommand。

    password 认证走 sshpass -d fd（密码经管道传递，永不出现在 argv/ps）；
    key 认证前置 -i（显式 null 兜底为空串，argv 绝不出现 None）。
    extra_auth_args 是模式专属认证参数（探针的 BatchMode /
    NumberOfPasswordPrompts）。
    """
    destination = _destination(tunnel)
    if tunnel.get("auth_type") == "password":
        r_fd, w_fd = os.pipe()
        try:
            os.write(w_fd, (password + "\n").encode())
        except OSError:
            # 写端已废：读端一并关闭再抛出，fd 不泄漏
            try:
                os.close(r_fd)
            except OSError:
                pass
            raise
        finally:
            os.close(w_fd)
        cmd = (["sshpass", "-d", str(r_fd), "ssh"] + list(extra_auth_args)
               + ssh_args)
        display_cmd = " ".join(
            ["sshpass", "-d", "***", "ssh"] + list(extra_auth_args) + ssh_args)
        return SshCommand(cmd=cmd, display_cmd=display_cmd,
                          destination=destination, pass_fds=(r_fd,),
                          password_fd=r_fd)
    key = str(tunnel.get("ssh_key") or "")
    cmd = ["ssh"] + list(extra_auth_args) + ["-i", key] + ssh_args
    return SshCommand(cmd=cmd, display_cmd=" ".join(cmd),
                      destination=destination)


def build_tunnel_command(tunnel, socks5_port, password=""):
    """长驻隧道（ssh -D）的完整调用描述；spawn 由 SSHMonitor 负责。"""
    port = str(tunnel.get("ssh_port", 22))
    ssh_args = (
        ["-D", str(socks5_port), "-N", "-o", "ExitOnForwardFailure=yes"]
        + _host_key_args()
        + ["-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
           # #87：跨国链路——更快判死（60s）、不标 DSCP（防中间设备针对性丢包）、
           # 建连自带 3 次重试（缓解瞬时 connect 超时）
           "-o", "IPQoS=none", "-o", "ConnectionAttempts=3"])
    if tunnel.get("ssh_compression", True):
        ssh_args.append("-C")
    ssh_args.extend(["-p", port, _destination(tunnel)])
    return _with_auth(tunnel, ssh_args, password)


def probe(tunnel, password=""):
    """一次性连通性探针：与真实隧道同策略地连一次并立即退出。

    绿结果意味着隧道本身也会连上；未信任的主机快速失败，绝不自动信任。
    key 认证跑 BatchMode，passphrase 提示永远挂不住；password 认证复用
    sshpass-via-fd，密码不进 argv。调用方须先做完输入校验与密码取用
    （本函数假设 tunnel 字段已合法）。

    返回 {"ok": True} 或 {"ok": False, "error": "<中文短语>"}——绝不抛异常。
    """
    port = str(tunnel.get("ssh_port", 22))
    ssh_args = (["-o", "ConnectTimeout=5"] + _host_key_args()
                + ["-p", port, _destination(tunnel), "true"])
    if tunnel.get("auth_type") == "password":
        extra = ("-o", "NumberOfPasswordPrompts=1")
    else:
        extra = ("-o", "BatchMode=yes")
    sc = None
    try:
        # argv 构建（含 os.pipe/write）也在 try 内：fd 耗尽等 OSError
        # 同样归「无法启动 ssh」，契约「绝不抛异常」无条件成立。
        sc = _with_auth(tunnel, ssh_args, password, extra)
        proc = subprocess.run(
            sc.cmd, capture_output=True, timeout=PROBE_TIMEOUT,
            pass_fds=sc.pass_fds)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "连接超时"}
    except OSError:
        hint = "（密码认证需要 sshpass）" if password else ""
        return {"ok": False, "error": f"无法启动 ssh{hint}"}
    finally:
        if sc is not None:
            sc.close_password_fd()
    if proc.returncode == 0:
        return {"ok": True}
    # bytes + replace decode (not text=True)：SSH stderr 可能携带原始字节，
    # 严格 locale 解码绝不能把探针打崩。
    stderr = (proc.stderr or b"").decode("utf-8", "replace")
    return {"ok": False, "error": describe_failure(stderr)}
