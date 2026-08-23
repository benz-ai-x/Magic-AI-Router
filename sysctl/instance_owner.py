"""InstanceOwnership（issue #3）：进程所有权的可验证记录.

端口占用只是**发现线索**，不是所有权证明——只有锁记录（pid / 进程启动
时间 / exe 路径 / nonce）能证明一个 PID 属于 Magic AI Router。启动时间
入锁抵抗 PID 复用：同号 PID 但启动时间不同 = 别的进程，永远不发信号。

锁经 O_EXCL 原子创建；并发启动只有一个成功，失败方不触碰成功方的锁。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess

logger = logging.getLogger("magic-proxy.instance")

# 锁记录默认位置：macOS 应用支持目录（产品 macOS-only；测试经构造参数重定向）。
DEFAULT_LOCK_PATH = os.path.join(
    os.path.expanduser("~/Library/Application Support/Magic AI Router"),
    "instance.json")


def _ps_pid_info(pid: int):
    """生产 pid_info：ps 取进程启动时间与可执行路径。失败返回 (None, None)。

    ps -o lstart= -o comm= 输出单行「lstart(定宽日期) + 空白 + comm」——
    不是两行（#65：len(lines)!=2 的解析错配曾让 _holder_alive 恒 False，
    单实例守卫在生产从未生效）。lstart 定宽为 5 词（Www Mmm dd HH:MM:SS
    yyyy），按 5 词切前缀即可靠分出 comm。
    """
    try:
        out = subprocess.run(
            ["ps", "-p", str(pid), "-o", "lstart=", "-o", "comm="],
            capture_output=True, timeout=3, text=True,
            # LC_ALL=C：钉死 lstart 的英文 5 词形态——CJK locale 下 ps 输出
            # 本地化（「二  8月/18 …」4 词）会让定宽切分失配，守卫失败开放
            env={**os.environ, "LC_ALL": "C"})
        if out.returncode != 0:
            return None, None
        line = out.stdout.strip()
        if not line:
            return None, None
        parts = line.split(None, 5)  # lstart 定宽 5 词；第 6 段起为 comm
        if len(parts) != 6:
            return None, None
        start = " ".join(parts[:5])
        exe = parts[5].strip()
        return (start or None), (exe or None)
    except (OSError, subprocess.SubprocessError):
        return None, None


class InstanceOwner:
    """持有/验证本应用的实例所有权锁。"""

    def __init__(self, lock_path=None, pid_info=None, pid=None):
        self.lock_path = lock_path or DEFAULT_LOCK_PATH
        self._pid_info = pid_info or _ps_pid_info
        self._pid = pid if pid is not None else os.getpid()

    def _record(self):
        start, exe = self._pid_info(self._pid)
        return {"pid": self._pid, "start": start, "exe": exe,
                "nonce": secrets.token_hex(8)}

    def acquire(self):
        """原子创建锁记录并返回之。

        已存在且持有者仍活 → 返回 None（冲突），锁文件分毫不动——并发
        启动的失败方绝不触碰成功方的锁。已存在但持有者已死（pid_info
        查无此进程）→ 陈旧锁接管：先写临时文件再原子替换。
        """
        rec = self._record()
        os.makedirs(os.path.dirname(self.lock_path), exist_ok=True)
        try:
            fd = os.open(self.lock_path,
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            return None if self._holder_alive() else self._takeover(rec)
        with os.fdopen(fd, "w") as f:
            json.dump(rec, f)
        return rec

    def _load(self):
        try:
            with open(self.lock_path) as f:
                data = json.load(f)
            return data if isinstance(data, dict) else None
        except (OSError, ValueError):
            return None

    def _holder_alive(self):
        """锁持有者是否仍活着：pid 可查且启动时间一致（抗 PID 复用）。"""
        data = self._load()
        if not data or not isinstance(data.get("pid"), int):
            return False
        start, _exe = self._pid_info(data["pid"])
        return start is not None and start == data.get("start")

    def _takeover(self, rec):
        """陈旧锁接管：临时文件 + 原子替换，替换后重读自证。

        两进程同时接管陈旧锁时，os.replace 后锁内 nonce 只属于其一：
        重读 nonce 非己方即输掉竞争，返回 None（最多一个成功）。
        """
        tmp = self.lock_path + ".takeover.%d" % self._pid
        with open(tmp, "w") as f:
            json.dump(rec, f)
        os.replace(tmp, self.lock_path)
        data = self._load()
        if not data or data.get("nonce") != rec.get("nonce"):
            return None
        return rec

    def owns_pid(self, pid):
        """调用方只持 pid 时的便捷判定：pid_info 自取（启动时间 + exe）后比对。"""
        start, exe = self._pid_info(pid)
        return self.owns(pid, start, exe)

    def owns(self, pid, start, exe=None):
        """是否为本应用实例：pid + 启动时间匹配（抗 PID 复用）。

        exe 给出时一并比对（可执行路径三重证明）。"""
        data = self._load()
        if not data:
            return False
        if data.get("pid") != pid or start is None or data.get("start") != start:
            return False
        if exe is not None and data.get("exe") != exe:
            return False
        return True

    def release(self):
        """移除自己的锁（pid 匹配才删——活进程 pid 唯一，足以判别归属）。

        无锁/他人锁为 no-op：并发启动的失败方退出时绝不删成功方的锁。
        """
        data = self._load()
        if not data or data.get("pid") != self._pid:
            return
        try:
            os.unlink(self.lock_path)
        except OSError:
            pass
