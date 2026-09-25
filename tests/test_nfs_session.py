"""Tests for mount/nfs_session — 专用会话的 argv 投影面。

关键不变量：注入的 tunnel 副本只携带 NFS 这一条 -L——用户自己的转发
行由他们自己的会话持有，同端口双进程绑定会因 ExitOnForwardFailure
互顶死循环。

2026-09-25 v2 回归修复后：替换必须落在 ``services.ssh.forwards``
（命令构建 ssh_launch._forwards 只认 v2 键）——顶层 v1 键清空仅防
读时兼容。测试钉在**命令级**（build_tunnel_command 的 argv），形状
级断言防不住「写错键、构建读不到」这类静默落空。
"""
import unittest

from mount.nfs_session import NFS_REMOTE_PORT, NfsSession
from tunnel import ssh_launch


def _v2_tunnel(forwards=None):
    """v2 服务器行：连接参数在 ssh 节，转发实例在 services.ssh。"""
    return {"id": "t-1", "name": "srv",
            "ssh": {"user": "u", "host": "srv", "port": 22,
                    "auth_type": "key", "key": "~/.ssh/id"},
            "services": {"ssh": {"forwards":
                                 forwards if forwards is not None else []}}}


def _session(tunnel, port=12049):
    holder = {"t": tunnel}
    return NfsSession(
        tunnel_id="t-1", local_port=port, log_sink=lambda line: None,
        tunnel_fn=lambda: holder["t"], password_fn=lambda t: "")


def _fw(lp, rp=80, host="127.0.0.1"):
    return {"local_port": lp, "remote_host": host, "remote_port": rp}


class TestInjectedTunnelV2(unittest.TestCase):
    """v2 形状（运行时唯一真实形状——merge 后无 v1）。"""

    def test_user_forwards_replaced_in_services_node(self):
        s = _session(_v2_tunnel(forwards=[_fw(8030, 3080), _fw(9080)]))
        injected = s._injected_tunnel()
        self.assertEqual(
            ((injected.get("services") or {}).get("ssh") or {}).get("forwards"),
            [{"local_port": 12049, "remote_host": "127.0.0.1",
              "remote_port": NFS_REMOTE_PORT}])

    def test_v1_toplevel_forwards_cleared(self):
        """v1 顶层键（读时兼容路径）不得残留旧行。"""
        tunnel = {**_v2_tunnel(forwards=[_fw(8030, 3080)]),
                  "forwards": [_fw(9000, 5432)]}
        injected = _session(tunnel)._injected_tunnel()
        self.assertEqual(injected.get("forwards"), [])

    def test_injected_is_copy_original_untouched(self):
        original = _v2_tunnel(forwards=[_fw(8030, 3080)])
        _session(original)._injected_tunnel()
        svc = ((original.get("services") or {}).get("ssh") or {})
        self.assertEqual(len(svc.get("forwards")), 1)

    def test_tunnel_gone_returns_none(self):
        s = _session(_v2_tunnel())
        s._identity_fn = lambda: None
        self.assertIsNone(s._injected_tunnel())

    def test_local_port_recorded(self):
        self.assertEqual(_session(_v2_tunnel(), port=13000).local_port, 13000)


class TestInjectedCommandArgv(unittest.TestCase):
    """命令级回归（2026-09-25 真机事故）：NFS 会话的 ssh argv 只许携带
    NFS 这一条 -L。事故形态：替换写 v1 顶层键落空 → NFS 会话复制转发
    会话全部 -L → 双会话抢绑同口，输家 ExitOnForwardFailure 无限重试，
    NFS 本地端口永无人监听 → probe 永不通 → 挂载永不派发。"""

    def test_argv_carries_only_nfs_forward(self):
        s = _session(_v2_tunnel(forwards=[_fw(8030, 3080), _fw(9080)]),
                     port=12049)
        sc = ssh_launch.build_tunnel_command(s._injected_tunnel(),
                                             socks5_port=None)
        cmd = sc.cmd
        sc.close_password_fd()
        fw_args = [i for i, a in enumerate(cmd) if a == "-L"]
        self.assertEqual(len(fw_args), 1)   # 只有 NFS 一条 -L
        self.assertEqual(cmd[fw_args[0] + 1],
                         f"127.0.0.1:12049:127.0.0.1:{NFS_REMOTE_PORT}")
        self.assertNotIn("8030", cmd)
        self.assertNotIn("9080", cmd)


if __name__ == "__main__":
    unittest.main()
