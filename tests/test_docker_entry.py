"""Docker 容器入口（docker/entry.py）单元测试 — issue #22.

docker/ 不是包：测试用 importlib 按文件路径加载（issue 验收指定的加载
方式）。所有路径经参数/env 注入 tmp_path——绝不触碰真实 ~/.claude 或
~/.magic-proxy.json。
"""
import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
ENTRY_PATH = ROOT / "docker" / "entry.py"


def load_entry():
    """按文件路径加载 docker/entry.py（docker/ 无 __init__.py）。"""
    spec = importlib.util.spec_from_file_location("docker_entry_under_test", ENTRY_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def entry():
    return load_entry()


@pytest.fixture(autouse=True)
def _restore_paths():
    """PATHS 与 sys.modules 快照恢复——entry 的重定向绝不泄漏到其他测试。

    Security 只做引用级恢复（真实 PyObjC 模块被 pop 后重新 import 会在
    同进程内二次注册并 objc.error，因此绝不触发真实重导入）。"""
    from mpconf import config_store
    saved_paths = dict(config_store.PATHS)
    saved_security = sys.modules.get("Security")
    yield
    config_store.PATHS.clear()
    config_store.PATHS.update(saved_paths)
    # 只清理 stub——测试期间合法真实导入的 Security 留在 sys.modules
    # （真实 PyObjC 模块被 pop 后重导入会同进程二次注册 objc.error）
    cur = sys.modules.get("Security")
    if cur is not None and getattr(cur, "__docker_stub__", False):
        if saved_security is None:
            sys.modules.pop("Security", None)
        else:
            sys.modules["Security"] = saved_security


# ── install_macos_stubs ────────────────────────────────────────────

class TestInstallMacosStubs:
    def test_force_installs_stub_module(self, entry):
        """force=True：注入带标记的空模块，keychain 可导入。"""
        entry.install_macos_stubs(force=True)
        assert getattr(sys.modules["Security"], "__docker_stub__", False)
        # sysctl.keychain 模块级只 `import Security`——stub 足以导入成功
        import sysctl.keychain  # noqa: F401

    def test_stub_is_idempotent(self, entry):
        entry.install_macos_stubs(force=True)
        first = sys.modules["Security"]
        entry.install_macos_stubs(force=True)
        assert sys.modules["Security"] is first

    def test_fallback_when_import_fails(self, entry, monkeypatch):
        """非 force：import 失败（Linux 形态）时落到 stub。"""
        # sys.modules[name] = None 是 Python 的"导入必失败"哨兵
        monkeypatch.setitem(sys.modules, "Security", None)
        entry.install_macos_stubs()
        assert getattr(sys.modules["Security"], "__docker_stub__", False)

    def test_noop_when_real_security_present(self, entry):
        """真实 Security 可导入（macOS 形态）：非 force 不替换。"""
        entry.install_macos_stubs()
        assert not getattr(sys.modules.get("Security"), "__docker_stub__", False)


# ── bootstrap_default_config ───────────────────────────────────────

class TestBootstrapDefaultConfig:
    def test_creates_config_when_missing(self, entry, tmp_path):
        sp = tmp_path / "suanpan.yaml"
        created = entry.bootstrap_default_config(str(sp), str(tmp_path))
        assert created is True
        assert sp.exists()
        raw = yaml.safe_load(sp.read_text())
        # 用量日志指向 data 卷（容器重建不丢）
        assert raw["usage_log"]["path"] == str(tmp_path / "logs" / "usage.jsonl")
        # 经真实 schema 校验通过
        from suanpan.config import load_config
        cfg = load_config(str(sp))
        assert cfg.listen_port == 9527
        assert cfg.providers == {}

    def test_creates_log_dir(self, entry, tmp_path):
        """usage_log 不自建目录——bootstrap 必须建好 logs/。"""
        sp = tmp_path / "suanpan.yaml"
        entry.bootstrap_default_config(str(sp), str(tmp_path))
        assert (tmp_path / "logs").is_dir()

    def test_file_mode_0600(self, entry, tmp_path):
        sp = tmp_path / "suanpan.yaml"
        entry.bootstrap_default_config(str(sp), str(tmp_path))
        assert stat.S_IMODE(os.stat(sp).st_mode) == 0o600

    def test_existing_config_untouched(self, entry, tmp_path):
        sp = tmp_path / "suanpan.yaml"
        sp.write_text("listen_port: 9999\nproviders: {}\n")
        created = entry.bootstrap_default_config(str(sp), str(tmp_path))
        assert created is False
        assert yaml.safe_load(sp.read_text())["listen_port"] == 9999


# ── redirect_paths ─────────────────────────────────────────────────

class TestRedirectPaths:
    def test_updates_all_three_keys(self, entry, tmp_path):
        from mpconf import config_store
        sp, mp, claude = (str(tmp_path / n) for n in
                          ("suanpan.yaml", "magic-proxy.json", "settings.json"))
        entry.redirect_paths(sp, mp, claude)
        assert config_store.PATHS["sp"] == sp
        assert config_store.PATHS["mp"] == mp
        assert config_store.PATHS["claude_settings"] == claude
        assert config_store.get_path("sp") == sp


# ── run_sync ───────────────────────────────────────────────────────

class TestRunSync:
    def _paths(self, tmp_path):
        return (str(tmp_path / "suanpan.yaml"),
                str(tmp_path / "magic-proxy.json"),
                str(tmp_path / "settings.json"))

    def test_dry_run_writes_nothing(self, entry, tmp_path, capsys):
        """--dry-run 走 preview()：只出 diff，不落盘 settings。"""
        sp, mp, claude = self._paths(tmp_path)
        code = entry.run_sync(sp, mp, claude, dry_run=True)
        assert code == 0
        assert not os.path.exists(claude)
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        assert out["target"] == claude

    def test_sync_writes_gateway_env_and_token(self, entry, tmp_path, capsys):
        """实写：三件套 + AUTH_TOKEN=本地 token（#9 后契约，非占位）。"""
        sp, mp, claude = self._paths(tmp_path)
        entry.bootstrap_default_config(sp, str(tmp_path))
        code = entry.run_sync(sp, mp, claude, dry_run=False)
        assert code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is True
        env = json.loads(Path(claude).read_text())["env"]
        assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:9527"
        assert env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] == "1"
        # token 持久化在 data 卷的 mp 配置里，与写入 settings 的一致
        tok = json.loads(Path(mp).read_text())["local_client_token"]
        assert tok == env["ANTHROPIC_AUTH_TOKEN"]
        assert len(tok) == 32 and all(c in "0123456789abcdef" for c in tok)

    def test_sync_idempotent_already(self, entry, tmp_path, capsys):
        sp, mp, claude = self._paths(tmp_path)
        entry.bootstrap_default_config(sp, str(tmp_path))
        entry.run_sync(sp, mp, claude)
        first = Path(claude).read_text()
        capsys.readouterr()
        code = entry.run_sync(sp, mp, claude)
        assert code == 0
        out = json.loads(capsys.readouterr().out)
        assert out["action"] == "already"
        assert Path(claude).read_text() == first  # 重复同步不写入

    def test_failure_returns_nonzero(self, entry, tmp_path, capsys, monkeypatch):
        """setup 失败（如目标目录不可写）→ 非零退出码 + ok=False。"""
        sp, mp, claude = self._paths(tmp_path)
        # claude 指向一个"父目录是文件"的死路径 → 写入必败
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        dead = str(blocker / "settings.json")
        code = entry.run_sync(sp, mp, dead, dry_run=False)
        assert code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["ok"] is False


# ── serve：app 工厂 seam（不真跑 uvicorn） ────────────────────────

class TestLoadApp:
    def test_builds_app_with_health_route(self, entry, tmp_path):
        sp = str(tmp_path / "suanpan.yaml")
        entry.bootstrap_default_config(sp, str(tmp_path))
        app, port = entry.load_app(sp)
        assert port == 9527
        paths = {getattr(r, "path", None) for r in app.routes}
        assert "/health" in paths

    def test_bootstraps_when_missing(self, entry, tmp_path):
        """load_app 前置引导——serve 模式首启即有可用配置。"""
        sp = str(tmp_path / "suanpan.yaml")
        entry.load_app(sp)
        assert os.path.exists(sp)


# ── default_paths ──────────────────────────────────────────────────

class TestDefaultPaths:
    def test_container_defaults(self, entry):
        paths = entry.default_paths({})
        assert paths["sp"] == "/data/suanpan.yaml"
        assert paths["mp"] == "/data/magic-proxy.json"
        assert paths["claude_settings"] == "/host-claude/settings.json"

    def test_env_overrides(self, entry):
        paths = entry.default_paths({
            "SUANPAN_DATA_DIR": "/srv/sp",
            "CLAUDE_SETTINGS_PATH": "/srv/claude/settings.json",
        })
        assert paths["sp"] == "/srv/sp/suanpan.yaml"
        assert paths["mp"] == "/srv/sp/magic-proxy.json"
        assert paths["claude_settings"] == "/srv/claude/settings.json"


# ── main：模式分发 ─────────────────────────────────────────────────

class TestMain:
    def test_empty_argv_defaults_to_serve(self, entry, monkeypatch):
        calls = []
        monkeypatch.setattr(entry, "run_serve", lambda: calls.append("serve") or 0)
        assert entry.main([]) == 0
        assert calls == ["serve"]

    def test_serve_explicit(self, entry, monkeypatch):
        calls = []
        monkeypatch.setattr(entry, "run_serve", lambda: calls.append("serve") or 0)
        assert entry.main(["serve"]) == 0
        assert calls == ["serve"]

    def test_sync_dry_run_dispatch(self, entry, monkeypatch, tmp_path):
        seen = {}

        def fake_sync(sp, mp, claude, dry_run=False):
            seen.update(locals())
            return 0

        monkeypatch.setattr(entry, "run_sync", fake_sync)
        monkeypatch.setenv("SUANPAN_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(tmp_path / "s.json"))
        assert entry.main(["sync-claude-code", "--dry-run"]) == 0
        assert seen["dry_run"] is True
        assert seen["sp"] == str(tmp_path / "suanpan.yaml")
        assert seen["claude"] == str(tmp_path / "s.json")

    def test_unknown_mode_usage_exit_2(self, entry, capsys):
        assert entry.main(["bogus"]) == 2
        assert "serve" in capsys.readouterr().err


# ── config-ui：DockerConfigServer ─────────────────────────────────

class TestDockerConfigServer:
    def test_subclass_binds_wildcard(self, entry):
        """容器内须绑 0.0.0.0（基类硬编码 127.0.0.1）——子类覆盖，零修改基类。"""
        srv = entry.DockerConfigServer(port=0)
        assert srv._bind_host == "0.0.0.0"

    def test_default_port_9528(self, entry):
        srv = entry.DockerConfigServer()
        assert srv._port == 9528

    def test_keychain_stubbed(self, entry):
        """Linux 无 Keychain——注入存根，SP-only 保存跳过 keychain 段。"""
        srv = entry.DockerConfigServer()
        assert srv._keychain is None


# ── config-ui：token 解析 ─────────────────────────────────────────

class TestConfigToken:
    def test_token_is_local_token_from_mp(self, entry, tmp_path):
        """config token 复用 #22 的 local_client_token（零新 secret 文件）。"""
        mp = str(tmp_path / "magic-proxy.json")
        tok = entry.config_token(mp)
        import json as _j
        persisted = _j.loads((tmp_path / "magic-proxy.json").read_text())
        assert tok == persisted["local_client_token"]
        assert len(tok) == 32

    def test_token_idempotent(self, entry, tmp_path):
        mp = str(tmp_path / "magic-proxy.json")
        assert entry.config_token(mp) == entry.config_token(mp)


# ── config-ui：真实 HTTP 冒烟（tmp 路径 + 随机端口，不碰真实配置） ────

class TestConfigUiHttp:
    def _server(self, entry, tmp_path):
        mp = str(tmp_path / "magic-proxy.json")
        sp = str(tmp_path / "suanpan.yaml")
        entry.bootstrap_default_config(sp, str(tmp_path))
        srv = entry.DockerConfigServer(port=0, mp_path=mp, sp_path=sp)
        ok, _url = srv.start()
        assert ok
        # 端口 0 → OS 分配；从 server 对象取实际端口
        port = srv._server.server_address[1]
        return srv, port

    def test_get_root_requires_token(self, entry, tmp_path):
        import urllib.request
        import urllib.error
        srv, port = self._server(entry, tmp_path)
        try:
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/")
            assert ei.value.code == 401
        finally:
            srv.stop()

    def test_get_root_with_token_serves_html(self, entry, tmp_path):
        import urllib.request
        srv, port = self._server(entry, tmp_path)
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/",
                headers={"Authorization": f"Bearer {srv.token}"})
            html = urllib.request.urlopen(req).read().decode("utf-8")
            assert "AI 路由" in html or "供应商" in html  # config_ui.html 内容
        finally:
            srv.stop()

    def test_api_state_returns_masked_sp(self, entry, tmp_path):
        import urllib.request
        import json as _j
        srv, port = self._server(entry, tmp_path)
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/state",
                headers={"Authorization": f"Bearer {srv.token}"})
            data = _j.loads(urllib.request.urlopen(req).read())
            assert data["sp"]["listen_port"] == 9527
            assert data["sp"]["providers"] == {}
        finally:
            srv.stop()

    def test_agent_md_public_no_token(self, entry, tmp_path):
        import urllib.request
        srv, port = self._server(entry, tmp_path)
        try:
            body = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/agent.md").read().decode("utf-8")
            assert len(body) > 0
        finally:
            srv.stop()

    def test_non_loopback_host_rejected(self, entry, tmp_path):
        import urllib.request
        import urllib.error
        srv, port = self._server(entry, tmp_path)
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/agent.md",
                headers={"Host": "evil.example.com"})
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(req)
            assert ei.value.code == 403
        finally:
            srv.stop()


# ── config-ui：main 分发 ──────────────────────────────────────────

class TestMainConfigUi:
    def test_config_ui_dispatch(self, entry, monkeypatch):
        calls = []
        monkeypatch.setattr(entry, "run_config_ui",
                            lambda: calls.append("config-ui") or 0)
        assert entry.main(["config-ui"]) == 0
        assert calls == ["config-ui"]
