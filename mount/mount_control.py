"""本地挂载控制：mount 表解析、mount_nfs/umount 调用、sudoers 引导。

挂载状态真相源是 `/sbin/mount` 输出（无需 sudo）——所有判定（已挂载/
卸载成功）都回读表而非信任单次命令返回码。

本地提权模型（ADR-007）：mount_nfs/umount 需要 root，一次性经 osascript
管理员授权写入 /etc/sudoers.d/magic-router-mount（先 visudo -cf 校验），
规则仅限这两个二进制（任意参数）——范围最小化，密码不再被提示。
"""
import base64
import getpass
import logging
import os
import shlex
import socket
import subprocess

logger = logging.getLogger("magic-proxy.nfs-mount")

MOUNT_NFS_BIN = "/sbin/mount_nfs"
UMOUNT_BIN = "/sbin/umount"
MOUNT_BIN = "/sbin/mount"
SUDOERS_PATH = "/etc/sudoers.d/magic-router-mount"

MOUNT_TIMEOUT = 20      # mount_nfs 对死隧道会阻塞——硬上限
UMOUNT_TIMEOUT = 10
OSA_TIMEOUT = 300       # 管理员授权弹窗的思考时间
PORT_PROBE_TIMEOUT = 0.3


# ── mount 表解析（真相源）────────────────────────────────────────

def parse_mounts(output):
    """解析 `mount` 输出 → {绝对挂载点: 服务端 spec}，只保留 NFS 条目。

    行形状 `spec on mountpoint (fstype, opts…)`；括号列表首项是文件系统
    类型——"(nfs" 前缀即 NFS。挂载点含 " on "/" (" 的病态命名不在
    防御范围（rsplit 从右取，与真实 macOS 输出一致）。
    """
    result = {}
    for line in (output or "").splitlines():
        head, sep, opts = line.rpartition(" (")
        if not sep or not opts.rstrip(")").startswith("nfs"):
            continue
        spec, sep, mountpoint = head.rpartition(" on ")
        if not sep or not mountpoint.startswith("/"):
            continue
        result[mountpoint] = spec.strip()
    return result


def nfs_mounts():
    """当前 NFS 挂载表（无需 sudo）。任何失败都折叠为 {}——绝不抛异常。"""
    try:
        proc = subprocess.run([MOUNT_BIN], capture_output=True,
                              timeout=5, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if proc.returncode != 0:
        return {}
    return parse_mounts(proc.stdout.decode("utf-8", "replace"))


def is_mounted(local_dir):
    return os.path.abspath(local_dir) in nfs_mounts()


def probe_local_port(port, timeout=PORT_PROBE_TIMEOUT):
    """127.0.0.1:port 的 TCP 探测（NFS 隧道就绪判定）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(("127.0.0.1", int(port)))
        return True
    except OSError:
        return False
    finally:
        s.close()


# ── 挂载点目录 ────────────────────────────────────────────────────

def ensure_mountpoint(local_dir):
    """建挂载点目录（无 sudo；/Volumes 对 admin 组可写）。"""
    path = os.path.abspath(os.path.expanduser(local_dir))
    if os.path.isdir(path):
        return True, ""
    try:
        os.makedirs(path, exist_ok=True)
        return True, ""
    except OSError as exc:
        return False, (
            f"无法创建挂载点 {path}：{exc.strerror or exc}\n"
            "（/Volumes 下创建需要本机管理员；或改用家目录内的路径）")


# ── sudoers 引导（一次性管理员授权）──────────────────────────────

def sudoers_rule(user=None):
    """免密规则：仅 mount_nfs 与 umount 两个二进制（任意参数）。"""
    return (f"{user or getpass.getuser()} "
            "ALL=(root) NOPASSWD: /sbin/mount_nfs, /sbin/umount\n")


def check_sudoers():
    """当前用户能否免密跑 mount_nfs/umount（读 sudo -n -l，绝不弹密码框）。"""
    try:
        proc = subprocess.run(["sudo", "-n", "-l"], capture_output=True,
                              timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    if proc.returncode != 0:
        return False
    out = proc.stdout.decode("utf-8", "replace")
    return MOUNT_NFS_BIN in out and UMOUNT_BIN in out


def _admin_script(rule, local_dirs):
    """osascript 以 root 执行的命令串：visudo 校验后安装 sudoers 规则，
    顺带创建挂载点目录。规则内容走 base64——避开双层引号地狱。"""
    rule_b64 = base64.b64encode(rule.encode("utf-8")).decode("ascii")
    tmp = SUDOERS_PATH + ".tmp"
    parts = [
        f"echo {rule_b64} | base64 -D > {tmp}",
        f"visudo -cf {tmp}",
        f"mv {tmp} {SUDOERS_PATH}",
        f"chmod 0440 {SUDOERS_PATH}",
    ]
    for d in local_dirs:
        parts.append(f"mkdir -p {shlex.quote(os.path.abspath(d))}")
    inner = " && ".join(parts)
    # 内层只含单引号（shlex.quote）——AppleScript 字符串字面量安全
    return f'do shell script "{inner}" with administrator privileges'


def install_sudoers(local_dirs=()):
    """弹一次 macOS 管理员授权写入 sudoers 规则 + 创建挂载点目录（幂等）。

    快速路径的判定是「规则已存在 **且** 所有目录已存在」——macOS 26 起
    /Volumes 是 root:wheel 0755，admin 组无写权限，普通 makedirs 建不了
    /Volumes/<名>；目录缺失时必须再走一次管理员脚本（规则重装幂等无害，
    代价是每个新的 /Volumes 挂载点多一次授权弹窗）。

    返回 (ok, error)；用户取消授权 → (False, "用户取消了管理员授权")。
    """
    dirs = [os.path.abspath(os.path.expanduser(d)) for d in local_dirs]
    missing = [d for d in dirs if not os.path.isdir(d)]
    if check_sudoers() and not missing:
        return True, ""
    try:
        proc = subprocess.run(
            ["osascript", "-e", _admin_script(sudoers_rule(), dirs)],
            capture_output=True, timeout=OSA_TIMEOUT,
            stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"无法请求管理员授权：{exc}"
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", "replace")
        if "User canceled" in stderr or "用户取消" in stderr:
            return False, "用户取消了管理员授权"
        return False, f"管理员授权失败：{stderr.strip()[:160]}"
    if not check_sudoers():
        return False, "sudoers 规则写入后校验未通过"
    return True, ""


# ── 挂载 / 卸载 ───────────────────────────────────────────────────

def mount_nfs(local_port, remote_path, local_dir, timeout=MOUNT_TIMEOUT):
    """挂载 `127.0.0.1:<remote_path>`（NFSv4，经 SSH 隧道本地端口）。

    幂等：挂载点已在 NFS 表中直接成功。绝不抛异常。
    """
    local_dir = os.path.abspath(os.path.expanduser(local_dir))
    if is_mounted(local_dir):
        return {"ok": True}
    argv = ["sudo", "-n", MOUNT_NFS_BIN,
            "-o", f"vers=4,port={int(local_port)},tcp,hard",
            f"127.0.0.1:{remote_path}", local_dir]
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "挂载超时（隧道不通或服务未响应）"}
    except OSError as exc:
        return {"ok": False, "error": f"无法启动 mount_nfs：{exc}"}
    if proc.returncode == 0 and is_mounted(local_dir):
        return {"ok": True}
    stderr = proc.stderr.decode("utf-8", "replace")
    return {"ok": False, "error": _classify_mount_failure(stderr)}


def _classify_mount_failure(stderr):
    text = (stderr or "").strip()
    lowered = text.lower()
    if "a password is required" in lowered or "not authorized" in lowered:
        return "需要管理员授权（挂载前会自动引导，请重试）"
    if "no route" in lowered or "timed out" in lowered:
        return "无法连到远程 NFS（隧道未就绪？）"
    if "unknown host" in lowered:
        return "远程 NFS 未响应"
    first = text.splitlines()[0] if text else "未知错误"
    return f"挂载失败：{first[:160]}"


def unmount(local_dir, force=False, timeout=UMOUNT_TIMEOUT):
    """卸载并回读 mount 表确认。绝不抛异常。"""
    local_dir = os.path.abspath(os.path.expanduser(local_dir))
    if not is_mounted(local_dir):
        return {"ok": True}
    argv = ["sudo", "-n", UMOUNT_BIN] + (["-f"] if force else []) + [local_dir]
    try:
        subprocess.run(argv, capture_output=True, timeout=timeout,
                       stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if not is_mounted(local_dir):
        return {"ok": True}
    return {"ok": False, "error": f"卸载 {local_dir} 失败（进程可能正占用该目录）"}


def force_unmount(local_dir):
    """卸载升级链：普通 → -f → diskutil（无 sudo，DiskArbitration 兜底）。"""
    r = unmount(local_dir)
    if r["ok"]:
        return r
    r = unmount(local_dir, force=True)
    if r["ok"]:
        return r
    local_dir = os.path.abspath(os.path.expanduser(local_dir))
    try:
        subprocess.run(["diskutil", "unmount", "force", local_dir],
                       capture_output=True, timeout=UMOUNT_TIMEOUT,
                       stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if not is_mounted(local_dir):
        return {"ok": True}
    return {"ok": False,
            "error": f"强制卸载失败：请手动执行 sudo umount -f {local_dir}"}
