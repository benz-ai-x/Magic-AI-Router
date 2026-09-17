"""Tests for mount/coordinator — 挂载生命周期状态机。

NfsSession 与 mount_control 全部打桩；executor 换同步假件（派发即执行）
——reconcile 的收敛语义（断线卸载/恢复重挂/退避/teardown）在此钉死。
"""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from mount import coordinator as mc
from mount.coordinator import MountCoordinator


def _resolve(row):
    row = row or {}
    explicit = str(row.get("local_dir") or "").strip()
    return explicit or f"/Volumes/{row.get('name')}"


def _config(*tunnels):
    return {"tunnels": list(tunnels)}


def _tunnel(tid="t-1", name="srv", enabled=True, port=12049, mounts=None,
            forwards=()):
    return {"id": tid, "name": name, "ssh_host": "h", "ssh_user": "u",
            "auth_type": "key", "forwards": list(forwards),
            "nfs": {"enabled": enabled, "local_port": port,
                    "squash_to_ssh_user": False,
                    "mounts": mounts if mounts is not None else [
                        {"name": "data", "remote_path": "/data",
                         "local_dir": "", "auto_mount": True}]}}


class _FakeRetry:
    def __init__(self):
        self.reset_called = 0

    def consume_due(self):
        return False

    def reset(self):
        self.reset_called += 1

    def cancel(self):
        pass

    def handle_error(self):
        pass


class _FakeMonitor:
    def __init__(self, status="connected"):
        self.status = status
        self.checked = []

    def check(self, port):
        self.checked.append(port)

    @property
    def is_host_key_changed(self):
        return False


class _FakeSession:
    """替身：只暴露 coordinator 消费的面。"""

    instances = []

    def __init__(self, tunnel_id, local_port, log_sink, tunnel_fn,
                 password_fn):
        self.tunnel_id = tunnel_id
        self.local_port = local_port
        self.monitor = _FakeMonitor()
        self.retry = _FakeRetry()
        self.host_key = SimpleNamespace(change_prompted=False)
        self.connects = 0
        self.stops = 0
        _FakeSession.instances.append(self)

    def connect(self):
        self.connects += 1

    def tick(self):
        self.ticks = getattr(self, "ticks", 0) + 1

    def reconnect_now(self):
        self.connects += 1

    def stop(self, blocking=True):
        self.stops += 1


class _FakeExecutor:
    """同步执行派发——测试确定性（真实线程语义由集成/E2E 覆盖）。"""

    def __init__(self):
        self.calls = []

    def submit(self, fn, *args):
        import concurrent.futures
        self.calls.append((fn.__name__, args))
        fn(*args)
        fut = concurrent.futures.Future()
        fut.set_result(None)
        return fut


class CoordinatorTestBase(unittest.TestCase):
    def setUp(self):
        _FakeSession.instances = []
        self.executor = _FakeExecutor()
        self.table = {}
        self.mounted_result = {"ok": True}
        self.unmounted_result = {"ok": True}
        self.mountpoint_ok = (True, "")
        self.sudoers_ok = (True, "")
        self.clock = [1000.0]

        session_patch = patch.object(mc, "NfsSession", _FakeSession)
        session_patch.start()
        self.addCleanup(session_patch.stop)
        self.coord = MountCoordinator(
            get_config=lambda: self.cfg,
            get_tunnel_password=lambda t: "",
            ssh_log_sink=lambda line: None,
            resolve_mount_dir=_resolve,
            clock=lambda: self.clock[0])
        self.coord._workers = self.executor
        self._patches = [
            patch.object(mc.mount_control, "nfs_mounts",
                         lambda: self.table),
            patch.object(mc.mount_control, "is_mounted",
                         lambda d: d in self.table),
            patch.object(mc.mount_control, "ensure_mountpoint",
                         lambda d: self.mountpoint_ok),
            patch.object(mc.mount_control, "install_sudoers",
                         lambda dirs: self.sudoers_ok),
            patch.object(mc.mount_control, "mount_nfs",
                         lambda lp, rp, d: self.mounted_result),
            patch.object(mc.mount_control, "force_unmount",
                         lambda d: self.unmounted_result),
            patch.object(mc.mount_control, "probe_local_port",
                         lambda p, timeout=0.3: True),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)
        self.cfg = _config(_tunnel())

    def _states_dict(self):
        return {k: v.status for k, v in self.coord._states.items()}


class TestTickReconcile(CoordinatorTestBase):
    def test_autostart_creates_session_and_mounts(self):
        self.coord.apply_autostarts()
        self.coord.tick()
        self.assertEqual(len(_FakeSession.instances), 1)
        self.assertEqual(self.coord._states[("t-1", "data")].status,
                         mc.STATUS_MOUNTED)
        self.assertIn(("data", "/Volumes/data"), [("data", "/Volumes/data")])
        self.assertTrue(self.executor.calls)  # mount job 已派发

    def test_disconnected_session_force_unmounts(self):
        self.coord.apply_autostarts()
        self.coord.tick()               # mounted
        # 隧道断开但 mount 表里仍挂载着 → 强制卸载
        _FakeSession.instances[0].monitor.status = "stopped"
        self.coord.tick()
        self.assertEqual(self.coord._states[("t-1", "data")].status,
                         mc.STATUS_UNMOUNTED)

    def test_recovery_remounts_after_backoff(self):
        self.coord.apply_autostarts()
        self.coord.tick()
        # 挂载失败 → error + 退避
        self.mounted_result = {"ok": False, "error": "挂载失败：x"}
        self.coord._states[("t-1", "data")].next_retry = 0
        self.coord._states[("t-1", "data")].busy = False
        self.coord._states[("t-1", "data")].status = mc.STATUS_UNMOUNTED
        self.coord.tick()
        self.assertEqual(self.coord._states[("t-1", "data")].status,
                         mc.STATUS_ERROR)
        self.assertIn("x", self.coord._states[("t-1", "data")].error)
        # 退避期内不再派发
        before = len(self.executor.calls)
        self.clock[0] += 1
        self.coord.tick()
        self.assertEqual(len(self.executor.calls), before)
        # 退避窗口过后自动重试
        self.mounted_result = {"ok": True}
        self.clock[0] += mc.MOUNT_RETRY_BACKOFF
        self.coord.tick()
        self.assertEqual(self.coord._states[("t-1", "data")].status,
                         mc.STATUS_MOUNTED)

    def test_tunnel_deleted_teardown(self):
        self.coord.apply_autostarts()
        self.coord.tick()
        self.cfg = _config()  # 隧道没了
        self.coord.tick()
        self.assertEqual(self.coord._sessions, {})
        self.assertEqual(_FakeSession.instances[0].stops, 1)

    def test_nfs_disabled_teardown(self):
        self.coord.apply_autostarts()
        self.coord.tick()
        self.cfg = _config(_tunnel(enabled=False))
        self.coord.tick()
        self.assertEqual(self.coord._sessions, {})

    def test_port_change_rebuilds_session(self):
        self.coord.apply_autostarts()
        self.coord.tick()
        self.cfg = _config(_tunnel(port=13000))
        self.coord.tick()
        self.assertEqual(len(_FakeSession.instances), 2)
        self.assertEqual(self.coord._sessions["t-1"].local_port, 13000)

    def test_mount_states_projection(self):
        self.cfg = _config(_tunnel(mounts=[
            {"name": "a", "remote_path": "/a", "local_dir": "",
             "auto_mount": False},
            {"name": "b", "remote_path": "/b", "local_dir": "",
             "auto_mount": False}]))
        states = self.coord.mount_states()
        self.assertEqual(
            [(s[2], s[3]) for s in states],
            [("a", mc.STATUS_UNMOUNTED), ("b", mc.STATUS_UNMOUNTED)])


class TestUserActions(CoordinatorTestBase):
    def test_start_mount_immediate_dispatch(self):
        self.coord.start_mount("t-1", "data")
        self.assertEqual(len(_FakeSession.instances), 1)
        self.assertEqual(self.coord._states[("t-1", "data")].status,
                         mc.STATUS_MOUNTED)

    def test_start_mount_creates_session_bypassing_backoff(self):
        # 会话 error 态带退避——显式点击须立即重连
        self.coord.apply_autostarts()
        self.coord.tick()
        sess = _FakeSession.instances[0]
        sess.monitor.status = "error"
        self.coord.start_mount("t-1", "data")
        self.assertEqual(sess.connects, 2)  # 初建 1 + 显式 1

    def test_stop_mount_unmounts_and_tears_down_last(self):
        self.coord.start_mount("t-1", "data")
        self.table["/Volumes/data"] = "127.0.0.1:/data"
        self.coord.stop_mount("t-1", "data")
        self.assertNotIn(("t-1", "data"), self.coord._desired)
        self.assertEqual(self.coord._states[("t-1", "data")].status,
                         mc.STATUS_UNMOUNTED)
        self.assertEqual(self.coord._sessions, {})

    def test_stop_mount_keeps_session_if_other_desired(self):
        self.cfg = _config(_tunnel(mounts=[
            {"name": "a", "remote_path": "/a", "local_dir": "",
             "auto_mount": False},
            {"name": "b", "remote_path": "/b", "local_dir": "",
             "auto_mount": False}]))
        self.coord.start_mount("t-1", "a")
        self.coord.start_mount("t-1", "b")
        self.table["/Volumes/a"] = "127.0.0.1:/a"
        self.coord.stop_mount("t-1", "a")
        self.assertIn("t-1", self.coord._sessions)  # b 还在挂

    def test_reconnect_now_forwards_to_sessions(self):
        self.coord.apply_autostarts()
        self.coord.tick()
        self.coord.reconnect_now()
        self.assertEqual(_FakeSession.instances[0].connects, 2)

    def test_unmount_all(self):
        self.coord.apply_autostarts()
        self.coord.tick()
        self.table["/Volumes/data"] = "127.0.0.1:/data"
        self.coord.unmount_all(timeout=1)
        self.assertEqual(self.coord._sessions, {})
        self.assertEqual(self.coord._desired, set())
        self.assertEqual(self.coord._states[("t-1", "data")].status,
                         mc.STATUS_UNMOUNTED)


class TestAggregation(CoordinatorTestBase):
    def test_any_mounted_and_session_connected(self):
        self.assertFalse(self.coord.any_mounted())
        self.assertFalse(self.coord.any_session_connected())
        self.coord.apply_autostarts()
        self.coord.tick()
        self.assertTrue(self.coord.any_mounted())
        self.assertTrue(self.coord.any_session_connected())


if __name__ == "__main__":
    unittest.main()


class TestStaleMountSweep(CoordinatorTestBase):
    """陈旧挂载清扫：上个实例崩溃留下的挂载（不在 desired）自动卸载。"""

    def test_stale_mount_of_undesired_entry_cleaned(self):
        # 表里有配置声明的挂载，但用户从未 start_mount（desired 空）
        self.table["/Volumes/data"] = "127.0.0.1:/data"
        self.coord.tick()
        self.assertEqual(self.coord._states[("t-1", "data")].status,
                         mc.STATUS_UNMOUNTED)
        self.assertIn(("_unmount_job", (("t-1", "data"),)),
                      self.executor.calls)

    def test_desired_mount_not_swept(self):
        self.coord.apply_autostarts()
        self.table["/Volumes/data"] = "127.0.0.1:/data"
        self.coord.tick()
        # desired 的收敛路径接管（connected → mounted），不走清扫
        self.assertEqual(self.coord._states[("t-1", "data")].status,
                         mc.STATUS_MOUNTED)

    def test_foreign_mount_dirs_untouched(self):
        # mount 表里不属于任何配置挂载项的目录（用户自己的 NFS）不动
        self.table["/Volumes/other"] = "fileserver:/x"
        self.coord.tick()
        self.assertEqual(self.executor.calls, [])
