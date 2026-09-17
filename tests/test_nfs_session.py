"""Tests for mount/nfs_session — 专用会话的 argv 投影面。

关键不变量：注入的 tunnel 副本只携带 NFS 这一条 -L——用户自己的转发
行由他们自己的会话持有，同端口双进程绑定会因 ExitOnForwardFailure
互顶死循环。
"""
import unittest

from mount.nfs_session import NFS_REMOTE_PORT, NfsSession


def _tunnel(forwards=None):
    return {"id": "t-1", "name": "srv", "ssh_host": "srv", "ssh_user": "u",
            "ssh_port": 22, "auth_type": "key", "ssh_key": "~/.ssh/id",
            "forwards": forwards if forwards is not None else []}


def _session(tunnel, port=12049):
    holder = {"t": tunnel}
    return NfsSession(
        tunnel_id="t-1", local_port=port, log_sink=lambda line: None,
        tunnel_fn=lambda: holder["t"], password_fn=lambda t: "")


class TestInjectedTunnel(unittest.TestCase):
    def test_user_forwards_replaced_not_appended(self):
        user_fw = {"local_port": 9000, "remote_host": "db",
                   "remote_port": 5432}
        s = _session(_tunnel(forwards=[user_fw]))
        injected = s._injected_tunnel()
        self.assertEqual(injected["forwards"], [
            {"local_port": 12049, "remote_host": "127.0.0.1",
             "remote_port": NFS_REMOTE_PORT}])

    def test_injected_is_copy_original_untouched(self):
        original = _tunnel(forwards=[{"local_port": 9000,
                                      "remote_host": "db",
                                      "remote_port": 5432}])
        _session(original)._injected_tunnel()
        self.assertEqual(len(original["forwards"]), 1)

    def test_tunnel_gone_returns_none(self):
        s = _session(_tunnel())
        s._tunnel_fn = lambda: None
        self.assertIsNone(s._injected_tunnel())

    def test_local_port_recorded(self):
        self.assertEqual(_session(_tunnel(), port=13000).local_port, 13000)


if __name__ == "__main__":
    unittest.main()
