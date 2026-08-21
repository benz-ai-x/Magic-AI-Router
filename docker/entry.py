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

零修改现有代码：Linux 容器内 PyObjC 的 ``Security`` 不存在，而
``suanpan.config`` → ``mpconf.config`` → ``sysctl.keychain`` 的模块级
import 链会拉到它——入口在 import suanpan 前注入空模块 stub；Docker
路径永不调用被 stub 的 keychain 函数。
"""
from __future__ import annotations

import json
import os
import sys
import types


def install_macos_stubs(force: bool = False) -> None:
    """为 Linux 容器注入 ``Security`` 空模块（PyObjC 缺失时）。

    ``sysctl/keychain.py`` 模块级只 ``import Security``、函数内才取属性，
    Docker 路径不调用这些函数——空模块即足以让 import 链通过。
    force=True 无条件替换为 stub（测试用；容器内永不传）。stub 带
    ``__docker_stub__`` 标记，测试据此清理、绝不误删真实 PyObjC 模块。
    """
    if not force:
        try:
            import Security  # noqa: F401
            return
        except ImportError:
            pass
    cur = sys.modules.get("Security")
    if getattr(cur, "__docker_stub__", False):
        return  # 已是 stub——幂等
    stub = types.ModuleType("Security")
    stub.__docker_stub__ = True
    sys.modules["Security"] = stub


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
    install_macos_stubs()
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
    install_macos_stubs()
    bootstrap_default_config(sp_path, os.path.dirname(sp_path) or ".")
    from suanpan.config import load_config
    from suanpan.main import create_app
    config = load_config(sp_path)
    return create_app(config, config_path=sp_path), config.listen_port


def run_serve() -> int:
    """serve 模式：网关绑 0.0.0.0（信任边界=宿主机端口映射）。"""
    paths = default_paths()
    app, port = load_app(paths["sp"])
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
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
    print("用法: entry.py [serve | sync-claude-code [--dry-run]]",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
