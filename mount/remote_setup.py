"""远程 NFS 服务器一键安装与状态探测（幂等）。

所有 ssh 调用走 tunnel/ssh_launch.run_remote（host-key 三件套/认证注入
/失败分类的策略单一归宿）；sudo 密码经 stdin 管道，永不进 argv。

远程侧只动本应用拥有的两个文件：
- /etc/exports.d/magic-router.exports（导出表，整体重写、幂等）
- 目标挂载目录（mkdir -p）
导出恒绑 127.0.0.1（SSH 隧道在服务器本地回环终结，不对公网暴露），
insecure 必须（sshd 端口转发的源端口非特权）。仅支持 NFSv4——单端口
2049、无需 portmapper/mountd，一条 -L 隧道即可承载。
"""
import logging
import shlex

from tunnel import ssh_launch

logger = logging.getLogger("magic-proxy.nfs-setup")

NFS_PORT = 2049
EXPORTS_FILE = "/etc/exports.d/magic-router.exports"
INSTALL_TIMEOUT = 300   # apt/dnf install 的硬上限
PROBE_TIMEOUT = 20      # 秒级探测命令

# os-release ID/ID_LIKE → 包管理器家族（探测顺序：先精确 ID 再 ID_LIKE）
_DISTRO_FAMILIES = {
    "debian": "apt-get", "ubuntu": "apt-get", "linuxmint": "apt-get",
    "rhel": "dnf", "fedora": "dnf", "rocky": "dnf", "almalinux": "dnf",
    "centos": "dnf", "ol": "dnf", "anolis": "dnf",
    "amzn": "yum", "openeuler": "yum",
}
_PACKAGES = {"apt-get": "nfs-kernel-server", "dnf": "nfs-utils",
             "yum": "nfs-utils"}
_UNSUPPORTED = "暂不支持该服务器发行版（支持 Debian/Ubuntu 与 RHEL 系）"

# `ss` 在极简系统可能缺席——netstat 兜底；两个都无则按未监听上报
_LISTEN_CHECK = (
    f"(ss -ltn 2>/dev/null || netstat -ltn 2>/dev/null) "
    f"| grep -q ':{NFS_PORT} '")


def detect_distro(os_release_text):
    """解析 /etc/os-release 文本 → 包管理器家族（apt-get/dnf/yum）。

    先按 ID 精确匹配，再按 ID_LIKE 逐个回退；未识别返回 None。
    """
    ids = []
    for line in (os_release_text or "").splitlines():
        key, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if key.strip() == "ID" and value:
            ids.insert(0, value.strip())
        elif key.strip() == "ID_LIKE" and value:
            ids.extend(v.strip() for v in value.split() if v.strip())
    for ident in ids:
        if ident in _DISTRO_FAMILIES:
            return _DISTRO_FAMILIES[ident]
    return None


def sudo_prefix(sudo_password):
    """远程 sudo 形态：有密码走 `sudo -S -p ''`（stdin 管道喂），无密码
    走 `sudo -n`（NOPASSWD 前提，失败由 run_remote 分类）。"""
    return "sudo -S -p ''" if sudo_password else "sudo -n"


def exports_content(paths, uid_gid=None):
    """本应用导出表全文（幂等整体重写）。

    uid_gid=(uid, gid) 非空时附 all_squash——服务器/Mac 的 UID 不一致时
    属主统一映射到 SSH 用户，避免「属主显示为未知用户」。
    """
    opts = "rw,sync,no_subtree_check,insecure"
    if uid_gid:
        opts += f",all_squash,anonuid={uid_gid[0]},anongid={uid_gid[1]}"
    return "".join(f"{p} 127.0.0.1({opts})\n" for p in paths)


def install_script(pkg_mgr):
    """安装 + 起服务的 root 脚本（经一次 `sudo sh -c` 执行，密码只喂一次）。

    Debian 系 nfs-server.service 是 nfs-kernel-server 的别名，但老版本
    只有本名——两个都试。安装幂等：已安装时包管理器自行跳过。
    """
    package = _PACKAGES[pkg_mgr]
    update = "apt-get update -qq" if pkg_mgr == "apt-get" else "true"
    return (
        "set -e\n"
        f"{update}\n"
        f"{pkg_mgr} install -y {shlex.quote(package)}\n"
        "systemctl enable --now nfs-server 2>/dev/null"
        " || systemctl enable --now nfs-kernel-server 2>/dev/null"
        " || true\n"
        "echo MR_NFS_INSTALLED\n")


def apply_script(paths, content, uid_gid=None):
    """建目录 + 写导出表 + 重载 + 验证监听的 root 脚本（一次 sudo）。

    squash 模式（uid_gid 非空）下，**新建**的目录 chown 给 SSH 用户——
    mkdir 以 root 跑，不 chown 的话 root:root 0755 目录对被 squash 成
    SSH 用户的客户端不可写（ali 上侥幸能写只因 SSH 用户是 root）。已
    存在的目录不动属主（可能是用户自己的数据目录）。
    """
    mkdirs = []
    for p in paths:
        quoted = shlex.quote(p)
        if uid_gid:
            mkdirs.append(
                f"if [ ! -d {quoted} ]; then "
                f"mkdir -p {quoted} && chown {uid_gid[0]}:{uid_gid[1]} "
                f"{quoted}; fi\n")
        else:
            mkdirs.append(f"mkdir -p {quoted}\n")
    return (
        "set -e\n"
        "mkdir -p /etc/exports.d\n"
        f"{''.join(mkdirs)}"
        f"printf %s {shlex.quote(content)} > {EXPORTS_FILE}\n"
        "chmod 644 " + EXPORTS_FILE + "\n"
        "systemctl is-active --quiet nfs-server 2>/dev/null"
        " || systemctl start nfs-server 2>/dev/null"
        " || systemctl start nfs-kernel-server 2>/dev/null || true\n"
        "exportfs -ra\n"
        f"if {_LISTEN_CHECK}; then echo MR_NFS_READY; "
        "else echo MR_NFS_NOT_LISTENING >&2; exit 41; fi\n"
        "echo MR_NFS_APPLIED\n")


def _probe_body():
    """无需 root 的状态探测命令（exportfs 探测存在性、导出表 0644 可读）。
    输出三段标记 + 导出表原文，由 _parse_probe 解析。"""
    return (
        "command -v exportfs >/dev/null 2>&1 && echo MR_EXPORTFS; "
        f"if {_LISTEN_CHECK}; then echo MR_LISTENING; fi; "
        f"cat {EXPORTS_FILE} 2>/dev/null || true")


def _parse_probe(stdout):
    """解析探测输出：标记行（MR_*）+ 导出表原文——取最后一个标记之后的
    剩余行作为导出表（标记顺序不保证 listening 在 exportfs 之后出现）。"""
    lines = stdout.splitlines()
    last_marker = -1
    for i, line in enumerate(lines):
        if line in ("MR_EXPORTFS", "MR_LISTENING"):
            last_marker = i
    return {
        "installed": "MR_EXPORTFS" in lines,
        "listening": "MR_LISTENING" in lines,
        "exports": "\n".join(lines[last_marker + 1:]) + "\n"
        if last_marker >= 0 and lines[last_marker + 1:] else "",
    }


def check_remote(tunnel, password=""):
    """探测远程 NFS 状态：发行版 / 已装 / 2049 监听 / 现有导出表。

    返回 {"ok": True, "family", "installed", "listening", "exports"} 或
    {"ok": False, "error": "<中文短语>"}——绝不抛异常。
    """
    release = ssh_launch.run_remote(
        tunnel, "cat /etc/os-release", password=password,
        timeout=PROBE_TIMEOUT)
    if not release["ok"]:
        return {"ok": False, "error": release["error"]}
    family = detect_distro(release["stdout"])
    if family is None:
        return {"ok": False, "error": _UNSUPPORTED}
    status = ssh_launch.run_remote(
        tunnel, _probe_body(), password=password, timeout=PROBE_TIMEOUT)
    if not status["ok"]:
        return {"ok": False, "error": status["error"]}
    result = _parse_probe(status["stdout"])
    result.update({"ok": True, "family": family})
    return result


def _fetch_uid_gid(tunnel, password):
    """取 SSH 用户的 uid/gid（squash 用）。失败返回 None（降级不 squash）。"""
    r = ssh_launch.run_remote(tunnel, "id -u; id -g", password=password,
                              timeout=PROBE_TIMEOUT)
    if not r["ok"]:
        return None
    try:
        uid, gid = (int(x) for x in r["stdout"].split())
        return uid, gid
    except ValueError:
        return None


def setup_remote(tunnel, mounts, password="", sudo_password="",
                 squash_to_ssh_user=False):
    """一键安装 + 配置导出（幂等，可重复执行）。

    mounts 是远程绝对路径列表。返回
    {"ok": True, "installed": bool, "squash": bool, "detail": str} 或
    {"ok": False, "error": str, "stage": "detect|install|apply"}——绝不抛异常。
    """
    if not mounts:
        return {"ok": False, "error": "未配置远程挂载路径", "stage": "detect"}
    probe = check_remote(tunnel, password=password)
    if not probe["ok"]:
        return {**probe, "stage": "detect"}

    uid_gid = None
    if squash_to_ssh_user:
        uid_gid = _fetch_uid_gid(tunnel, password)

    installed = probe["installed"] and probe["listening"]
    if not installed:
        script = f"{sudo_prefix(sudo_password)} sh -c " \
            f"{shlex.quote(install_script(probe['family']))}"
        r = ssh_launch.run_remote(tunnel, script, password=password,
                                  sudo_password=sudo_password,
                                  timeout=INSTALL_TIMEOUT)
        if not r["ok"]:
            return {"ok": False,
                    "error": f"安装 NFS 服务失败：{r['error']}",
                    "stage": "install"}

    content = exports_content(mounts, uid_gid=uid_gid)
    script = f"{sudo_prefix(sudo_password)} sh -c " \
        f"{shlex.quote(apply_script(mounts, content, uid_gid=uid_gid))}"
    applied = ssh_launch.run_remote(tunnel, script, password=password,
                                    sudo_password=sudo_password,
                                    timeout=INSTALL_TIMEOUT)
    if not applied["ok"]:
        error = applied["error"]
        if "MR_NFS_NOT_LISTENING" in applied.get("stderr", ""):
            error = "导出已写入，但 NFS 服务未在 2049 监听（请检查服务器 nfs-server 状态）"
        return {"ok": False, "error": f"应用导出配置失败：{error}",
                "stage": "apply"}
    logger.info("远程 NFS 就绪：family=%s squash=%s paths=%s",
                probe["family"], bool(uid_gid), mounts)
    return {"ok": True, "installed": not installed, "squash": bool(uid_gid),
            "detail": "远程 NFS 已就绪"}
