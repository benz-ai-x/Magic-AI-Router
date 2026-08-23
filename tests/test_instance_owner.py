"""InstanceOwnership（issue #3）：进程所有权的可验证记录.

Seam S1 —— sysctl/instance_owner 是唯一的所有权真相：锁记录（pid/启动
时间/exe/nonce）经 O_EXCL 原子创建；端口占用只是发现线索，不是所有权
证明。basename 启发式（_is_stale_instance）被本模块取代并删除。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from sysctl.instance_owner import InstanceOwner


def _owner(tmpdir, pid_info=None, pid=None):
    return InstanceOwner(
        lock_path=str(tmpdir / "instance.json"),
        pid_info=pid_info or (lambda p: ("START_OF_%d" % p, "/exe/app")),
        pid=pid or os.getpid(),
    )


class TestAcquire(unittest.TestCase):
    def test_acquire_creates_atomic_lock_record(self):
        with tempfile.TemporaryDirectory() as d:
            o = _owner(Path(d))
            rec = o.acquire()
            self.assertEqual(rec["pid"], os.getpid())
            data = json.load(open(o.lock_path))
            self.assertEqual(data["pid"], os.getpid())
            self.assertEqual(data["start"], "START_OF_%d" % os.getpid())
            self.assertEqual(data["exe"], "/exe/app")
            self.assertTrue(data["nonce"])


class TestAcquireExisting(unittest.TestCase):
    def _lockdir(self):
        return tempfile.TemporaryDirectory()

    def test_live_owner_conflict_returns_none_and_lock_untouched(self):
        with self._lockdir() as d:
            winner = _owner(Path(d))
            winner.acquire()  # 假定 winner 自己活着（pid_info 正常返回）
            before = open(winner.lock_path).read()
            loser = _owner(Path(d))
            self.assertIsNone(loser.acquire())
            self.assertEqual(open(loser.lock_path).read(), before,
                             "失败方不得触碰成功方的锁")

    def test_dead_owner_lock_is_taken_over(self):
        with self._lockdir() as d:
            stale = _owner(Path(d), pid_info=lambda p: ("OLD_START", "/old/exe"), pid=999)
            stale.acquire()
            fresh = _owner(Path(d))
            rec = fresh.acquire()
            self.assertIsNotNone(rec)
            data = json.load(open(fresh.lock_path))
            self.assertEqual(data["pid"], fresh._pid)
            self.assertTrue(data["nonce"])  # 接管者自带新 nonce


class TestOwnershipProof(unittest.TestCase):
    """端口占用者是否属于本应用：pid + 启动时间双匹配才算证明。"""

    def _acquired(self):
        d = tempfile.TemporaryDirectory()
        o = _owner(Path(d.name), pid=4242)
        rec = o.acquire()
        self.addCleanup(d.cleanup)
        return o, rec

    def test_pid_and_start_match_proves_ownership(self):
        o, rec = self._acquired()
        self.assertTrue(o.owns(4242, rec["start"]))

    def test_pid_reuse_same_pid_different_start_is_not_owner(self):
        o, rec = self._acquired()
        self.assertFalse(o.owns(4242, "LATER_START_OF_REUSED_PID"))

    def test_foreign_pid_is_not_owner(self):
        o, _ = self._acquired()
        self.assertFalse(o.owns(9999, "WHATEVER"))

    def test_corrupt_or_missing_lock_proves_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            o = _owner(Path(d))
            self.assertFalse(o.owns(1, "x"))
            open(o.lock_path, "w").write("{corrupt")
            self.assertFalse(o.owns(1, "x"))


class TestRelease(unittest.TestCase):
    def test_release_removes_own_lock(self):
        with tempfile.TemporaryDirectory() as d:
            o = _owner(Path(d))
            o.acquire()
            o.release()
            self.assertFalse(os.path.exists(o.lock_path))

    def test_release_without_lock_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            _owner(Path(d)).release()  # 不抛即过


class TestOwnsPid(unittest.TestCase):
    """owns_pid：调用方只有 pid（端口占用者），pid_info 由 owner 自取。"""

    def test_own_pid_with_matching_start(self):
        with tempfile.TemporaryDirectory() as d:
            o = _owner(Path(d), pid=4242)
            o.acquire()
            self.assertTrue(o.owns_pid(4242))

    def test_reused_pid_does_not_own(self):
        with tempfile.TemporaryDirectory() as d:
            # 锁里 pid=4242/start=S_A；此后 pid_info 对 4242 返回 S_B（复用）
            o = InstanceOwner(lock_path=str(Path(d) / "instance.json"),
                              pid_info=lambda p: ("S_A", "/exe"), pid=4242)
            o.acquire()
            reused = InstanceOwner(lock_path=o.lock_path,
                                    pid_info=lambda p: ("S_B", "/exe"), pid=4242)
            self.assertFalse(reused.owns_pid(4242))




class TestRealPsParse(unittest.TestCase):
    """#65：真实 ps 输出进测试——替身注入曾让「要求 2 行」的解析错配
    在生产裸奔（_holder_alive 恒 False，守卫从未生效）。"""

    def test_ps_pid_info_parses_single_line_shape(self):
        from sysctl.instance_owner import _ps_pid_info
        start, exe = _ps_pid_info(os.getpid())
        self.assertIsNotNone(start, "真实 ps 单行输出必须可解析")
        self.assertIsNotNone(exe)
        self.assertIn(":", start, "lstart 含 HH:MM:SS")

    def test_ps_pid_info_dead_pid_returns_none(self):
        from sysctl.instance_owner import _ps_pid_info
        start, exe = _ps_pid_info(99999999)
        self.assertIsNone(start)
        self.assertIsNone(exe)

    def test_live_owner_lock_blocks_second_acquire(self):
        """活实例持锁时第二次 acquire 必须拒绝——守卫真正生效
        （解析修复前锁恒被抢走）。同进程模拟第二实例：手工种一条
        「他者」锁记录（pid=当前进程但 start 不同=不可接管）再竞争。"""
        import tempfile
        from pathlib import Path
        from sysctl.instance_owner import InstanceOwner, _ps_pid_info
        with tempfile.TemporaryDirectory() as d:
            lock = Path(d) / "lock.json"
            start, exe = _ps_pid_info(os.getpid())
            # 真实当前进程持锁（真 ps 解析出的 start 与 holder-alive
            # 校验路径同形）——二次 acquire 必须被拒
            import json
            lock.write_text(json.dumps(
                {"pid": os.getpid(), "start": start, "exe": exe,
                 "nonce": "n"}))
            second = InstanceOwner(lock_path=str(lock))
            self.assertFalse(second.acquire(),
                             "活实例（真 ps 可解析）持锁时必须拒绝二次 acquire")



if __name__ == "__main__":
    unittest.main()
