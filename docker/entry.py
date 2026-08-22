"""Docker 容器入口（issue #22）：Suanpan 网关的 Linux 部署形态.

两种模式：
- ``serve``（默认）：首启引导 /data/suanpan.yaml → create_app →
  uvicorn 绑 0.0.0.0:<listen_port>（绕开 run_from_config_path 的回环
  守卫：容器内绑 0.0.0.0 是 Docker 标准姿势，信任边界移到宿主机端口
  映射——compose 固定 ``127.0.0.1:<port>:<port>``，宿主机侧仅回环可达）。
- ``sync-claude-code`` [--dry-run]：重定向 config_store.PATHS 三键
  （sp→/data/suanpan.yaml、mp→/data/magic-proxy.json、claude_settings→
  /host-claude/settings.json）后整体复用 services.claude_code_setup 的
  setup()/preview()。mp 键也必须重定向——#9 之后 AUTH_TOKEN 写的是本地
  客户端 token（mpconf/local_token，存 PATHS["mp"]），落在 /data 卷才能
  跨容器重建保持稳定。容器内 suanpan_listen() 算出的
  http://127.0.0.1:<port> 恰等于宿主机侧 Claude Code 应使用的地址。

Linux 容器内 PyObjC 的 ``Security`` 不存在：``sysctl.keychain`` 自身
try/except 可选化（Security=None + 全吞异常兜底），import 链裸奔即可，
无需任何 stub。
"""
from __future__ import annotations

import json
import os
import sys

from services.suanpan_runtime import SuanpanRuntime


def default_paths(env=None):
    """三条路径的容器默认值（env 可覆盖，便于本地调试与测试）。"""
    env = os.environ if env is None else env
    data_dir = env.get("SUANPAN_DATA_DIR", "/data")
    return {
        "sp": os.path.join(data_dir, "suanpan.yaml"),
        "mp": os.path.join(data_dir, "magic-proxy.json"),
        "claude_settings": env.get(
            "CLAUDE_SETTINGS_PATH", "/host-claude/settings.json"),
    }


def bootstrap_default_config(sp_path: str, data_dir: str) -> bool:
    """首启引导：/data/suanpan.yaml 缺失时生成最小默认配置。

    用量日志指向 data 卷（容器重建不丢）；usage_log 自身不建目录，
    logs/ 在此建好。经 config_store.atomic_write（0600 + 原子替换）。
    已存在则不动（返回 False）——用户配置永不被覆盖。
    """
    if os.path.exists(sp_path):
        return False
    log_path = os.path.join(data_dir, "logs", "usage.jsonl")
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    default_yaml = (
        "listen_port: 9527\n"
        "request_timeout_s: 3600\n"
        "body_limit_mb: 50\n"
        f"usage_log:\n  enabled: true\n  path: {log_path}\n"
        "providers: {}\n"
        "router: {}\n"
        "rules: []\n"
    )
    from mpconf import config_store
    ok = config_store.atomic_write(sp_path, default_yaml, mode=0o600)
    if not ok:
        raise OSError(f"无法创建默认配置 {sp_path}")
    return True


def redirect_paths(sp_path: str, mp_path: str, claude_settings_path: str) -> None:
    """重定向 config_store.PATHS 三键（文档化的单一运行时重定向点）。"""
    from mpconf import config_store
    config_store.PATHS.update({
        "sp": sp_path,
        "mp": mp_path,
        "claude_settings": claude_settings_path,
    })


def run_sync(sp_path: str, mp_path: str, claude_settings_path: str,
             dry_run: bool = False) -> int:
    """同步 Claude Code 配置：重定向 PATHS 后复用 claude_code_setup.

    dry_run=True 走 preview()（只出逐键 diff）；否则 setup()（幂等写入，
    首写 .bak，token 掩码）。结果以 JSON 打到 stdout；退出码 0/1。
    """
    redirect_paths(sp_path, mp_path, claude_settings_path)
    from services import claude_code_setup
    if dry_run:
        result = claude_code_setup.preview()
    else:
        result = claude_code_setup.setup()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


def load_app(sp_path: str):
    """serve 工厂 seam：引导默认配置 → load_config → create_app.

    不在此跑 uvicorn——测试经本函数拿到 (app, listen_port) 即可验证
    /health 与端口。绕开 run_from_config_path 的回环守卫（见模块
    docstring）：绑定 0.0.0.0 由 run_serve 决定。
    """
    bootstrap_default_config(sp_path, os.path.dirname(sp_path) or ".")
    from suanpan.config import load_config
    from suanpan.main import create_app
    config = load_config(sp_path)
    return create_app(config, config_path=sp_path), config.listen_port


def run_serve() -> int:
    """serve 模式：网关绑 0.0.0.0 + 配置页面 :9528（共用同一 token）。

    SuanpanRuntime(bind_host="0.0.0.0") 跑网关；config server 的
    on_sp_saved 指向 runner.reload——配置页保存 → 网关热重载，无需重启
    容器（与 macOS 版同一运行时，仅绑定地址形态不同）。
    """
    paths = default_paths()
    # 先重定向再构造——SuanpanRuntime 经 PATHS["sp"] 取配置路径；
    # 缺配置文件时 bootstrap 首启自建（usage_log 指到数据卷，重建不丢），
    # runner 的 _ensure_config 兜底
    redirect_paths(paths["sp"], paths["mp"], paths["claude_settings"])
    bootstrap_default_config(paths["sp"], os.path.dirname(paths["sp"]))
    runner = SuanpanRuntime(bind_host="0.0.0.0")
    runner.start()
    # 配置页面与网关同容器、同 token——config-ui 失败不阻塞网关（best-effort）
    cfg = make_config_server(paths["mp"], paths["sp"],
                             on_sp_saved=runner.reload)
    ok = cfg.start()
    if ok:
        print(f"配置页面: {cfg.url}  Bearer token: {cfg.token}", flush=True)
    else:
        print("config-ui 启动失败（9528 占用？），仅跑网关", file=sys.stderr)
    # 主线程阻塞保活（网关在 AsyncRuntime 的 daemon 线程里跑）
    try:
        import time
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        runner.stop()
        cfg.stop()
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] == "serve":
        if len(argv) > 1:
            print(f"serve 不接受参数: {' '.join(argv[1:])}", file=sys.stderr)
            return 2
        return run_serve()
    if argv[0] == "sync-claude-code":
        rest = argv[1:]
        unknown = [a for a in rest if a != "--dry-run"]
        if unknown:
            print(f"未知参数: {' '.join(unknown)}", file=sys.stderr)
            return 2
        paths = default_paths()
        return run_sync(paths["sp"], paths["mp"], paths["claude_settings"],
                        dry_run="--dry-run" in rest)
    if argv[0] == "config-ui":
        rest = argv[1:]
        if rest:
            print(f"config-ui 不接受参数: {' '.join(rest)}", file=sys.stderr)
            return 2
        return run_config_ui()
    if argv[0] == "config-token":
        paths = default_paths()
        print(config_token(paths["mp"]))
        return 0
    print("用法: entry.py [serve | sync-claude-code [--dry-run] | config-ui | config-token]",
          file=sys.stderr)
    return 2


# ── config-ui：Web 配置页面（:9528）──────────────────────────────────
# 复用参数化的 services.config_server.ConfigServer（纯 stdlib）：
# Docker 差异全部是 make_config_server 的构造参数（0.0.0.0 + 卷内固定
# token）。token 复用 #22 的 local_client_token（get_local_token 幂等，
# 零新 secret）。


def config_token(mp_path: str) -> str:
    """config-ui 的 bearer token = 本地客户端 token（幂等读取）。"""
    from mpconf.local_token import get_local_token
    return get_local_token(mp_path)


def make_config_server(mp_path: str, sp_path: str, on_sp_saved=None,
                       port: int = 9528):
    """Docker 形态的 config server 装配：差异全部是构造参数。

    重定向 PATHS 三键后构造参数化 ConfigServer——绑 0.0.0.0（容器外
    经宿主机端口映射可达）、token 取配置卷的 local_client_token
    （与 sync 同源，零新 secret）。私有符号接触为零。
    """
    paths = default_paths()
    redirect_paths(sp_path, mp_path, paths["claude_settings"])
    from services.config_server import ConfigServer
    return ConfigServer(port=port, bind_host="0.0.0.0",
                        token=config_token(mp_path),
                        on_sp_saved=on_sp_saved)


def run_config_ui() -> int:
    """config-ui 模式：启动 :9528 配置页面（容器内阻塞运行）。"""
    paths = default_paths()
    bootstrap_default_config(paths["sp"], os.path.dirname(paths["sp"]))
    srv = make_config_server(paths["mp"], paths["sp"])
    if not srv.start():
        print("config server 启动失败（9528 端口占用？）", file=sys.stderr)
        return 1
    print(f"配置页面: {srv.url}", flush=True)
    print(f"Bearer token（浏览器带 Authorization 头访问）: {srv.token}",
          flush=True)
    try:
        import time
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        srv.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
