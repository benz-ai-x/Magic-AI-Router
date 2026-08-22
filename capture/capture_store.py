"""Private, bounded filesystem store for sensitive AI captures."""
import json
import os
import stat
from datetime import datetime


DEFAULT_CAPTURE_DIR = os.path.expanduser("~/.magic-proxy-captures")
DEFAULT_CAPTURE_PORT = 8080
MARKER = ".magic-proxy-capture-store"
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_STORE_BYTES = 200 * 1024 * 1024
MAX_RECORD_BYTES = 8 * 1024 * 1024  # 单条 record 上限：append 前拒收


def _home_dir():
    return os.path.realpath(os.path.expanduser("~"))


def prepare(path=None):
    requested = os.path.abspath(os.path.expanduser(path or DEFAULT_CAPTURE_DIR))
    home = _home_dir()
    parent = os.path.realpath(os.path.dirname(requested))
    resolved = os.path.realpath(requested) if os.path.lexists(requested) else os.path.join(parent, os.path.basename(requested))
    if os.path.commonpath((home, resolved)) != home:
        raise OSError("抓包目录必须位于当前用户主目录内")
    existed = os.path.lexists(requested)
    if existed:
        info = os.lstat(requested)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OSError("抓包路径不是安全的普通目录")
        if info.st_uid != os.getuid():
            raise OSError("抓包目录所有者不正确")
        marker = os.path.join(requested, MARKER)
        if requested != os.path.abspath(DEFAULT_CAPTURE_DIR) and not os.path.isfile(marker):
            raise OSError("拒绝修改非 Magic AI Router 创建的现有目录")
    else:
        os.makedirs(requested, mode=0o700)
    os.chmod(requested, 0o700)
    marker = os.path.join(requested, MARKER)
    if os.path.lexists(marker):
        marker_info = os.lstat(marker)
        if stat.S_ISLNK(marker_info.st_mode) or not stat.S_ISREG(marker_info.st_mode):
            raise OSError("抓包目录标记文件不安全")
    fd = os.open(marker, os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(fd)
    return requested


def clean(path=None):
    """Delete capture file contents, keeping the directory itself.

    Goes through prepare() so the same ownership/marker/symlink guarantees
    hold as for writes; only regular files are removed (the marker survives,
    symlinks and subdirectories are never touched). Returns the number of
    files deleted. Raises OSError for unsafe paths — callers surface it.
    """
    directory = prepare(path)
    removed = 0
    with os.scandir(directory) as scan:
        for entry in scan:
            if entry.name == MARKER:
                continue
            info = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                continue
            try:
                os.unlink(entry.path)
                removed += 1
            except FileNotFoundError:
                pass
    return removed


def _trim_store(directory):
    entries = []
    total = 0
    with os.scandir(directory) as scan:
        for entry in scan:
            if not entry.name.endswith((".jsonl", ".jsonl.1")) or entry.is_symlink():
                continue
            info = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                continue
            total += info.st_size
            entries.append((info.st_mtime, entry.path, info.st_size))
    for _mtime, path, size in sorted(entries):
        if total <= MAX_STORE_BYTES:
            break
        os.unlink(path)
        total -= size


def _converge_after_append(directory):
    """append 后即时预算收敛（_trim_store：留新删旧；绝非全清）。

    best-effort——失败只记日志，写入结果不受影响。
    """
    try:
        _trim_store(directory)
    except OSError:
        import logging
        logging.getLogger("magic-proxy.capture_store").debug(
            "post-append converge skipped", exc_info=True)


def append_json(record, directory):
    directory = prepare(directory)
    _trim_store(directory)
    dir_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    dir_info = os.fstat(dir_fd)
    if not stat.S_ISDIR(dir_info.st_mode) or dir_info.st_uid != os.getuid():
        os.close(dir_fd)
        raise OSError("抓包目录所有者或类型不安全")
    # 「今天」按系统本地时区（与 usage 聚合的 CST 钉死口径是刻意的
    # 分叉：抓包文件按用户直觉的本地日历滚动；跨子系统对照数据时注意）
    name = datetime.now().strftime("%Y-%m-%d") + ".jsonl"
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    try:
        payload = json.dumps(record, ensure_ascii=False)
        if len(payload.encode("utf-8")) > MAX_RECORD_BYTES:
            raise OSError(
                f"单条抓包记录超过 {MAX_RECORD_BYTES} 字节上限，已拒收")
        try:
            info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("抓包目标不是普通文件")
            if info.st_size >= MAX_FILE_BYTES:
                try:
                    os.unlink(name + ".1", dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
                os.rename(name, name + ".1", src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        fd = os.open(name, flags, 0o600, dir_fd=dir_fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            os.close(fd)
            raise OSError("抓包文件所有者或类型不安全")
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        # append 后容量收敛：单条超大也不让总量长期超限
        _converge_after_append(directory)
        return os.path.join(directory, name)
    finally:
        os.close(dir_fd)
