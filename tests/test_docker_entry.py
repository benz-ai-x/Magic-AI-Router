"""Docker 容器入口（docker/entry.py）单元测试 — issue #22.

docker/ 不是包：测试用 importlib 按文件路径加载（issue 验收指定的加载
方式）。所有路径经参数/env 注入 tmp_path——绝不触碰真实 ~/.claude 或
~/.magic-proxy.json。
"""
import importlib.util
import json
import os
import stat
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
    """PATHS 快照恢复——entry 的重定向绝不泄漏到其他测试。"""
    from mpconf import config_store
    saved_paths = dict(config_store.PATHS)
    yield
    config_store.PATHS.clear()
    config_store.PATHS.update(saved_paths)


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

class TestMakeConfigServer:
    """装配工厂：Docker 差异 = 纯构造参数，无私有符号接触。"""

    def _spy(self, monkeypatch):
        import services.config_server as cs_mod
        calls = []
        real = cs_mod.ConfigServer

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return real(*args, **kwargs)

        monkeypatch.setattr(cs_mod, "ConfigServer", spy)
        return calls

    def test_constructs_parameterized_config_server(
            self, entry, tmp_path, monkeypatch):
        mp = str(tmp_path / "magic-proxy.json")
        sp = str(tmp_path / "suanpan.yaml")
        calls = self._spy(monkeypatch)
        cb = object()
        entry.make_config_server(mp, sp, on_sp_saved=cb, port=0)
        _args, kwargs = calls[0]
        assert kwargs["bind_host"] == "0.0.0.0"
        assert kwargs["port"] == 0
        assert kwargs["on_sp_saved"] is cb
        # token 与 config-ui 同源：配置卷里的 local_client_token
        assert kwargs["token"] == entry.config_token(mp)

    def test_redirects_paths_before_construct(self, entry, tmp_path):
        from mpconf import config_store
        mp = str(tmp_path / "magic-proxy.json")
        sp = str(tmp_path / "suanpan.yaml")
        entry.make_config_server(mp, sp, port=0)
        assert config_store.PATHS["sp"] == sp
        assert config_store.PATHS["mp"] == mp

    def test_returns_started_ready_server(self, entry, tmp_path):
        mp = str(tmp_path / "magic-proxy.json")
        sp = str(tmp_path / "suanpan.yaml")
        srv = entry.make_config_server(mp, sp, port=0)
        try:
            assert srv.start()
            assert srv._server.server_address[0] == "0.0.0.0"
        finally:
            srv.stop()


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
        srv = entry.make_config_server(mp, sp, port=0)
        assert srv.start()
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


# ── 配置热重载：DockerConfigServer 传递 on_sp_saved ─────────────────

class TestConfigHotReload:
    def test_on_sp_saved_passed_to_server(self, entry, tmp_path):
        """PUT /api/state 保存 SP 成功后触发 on_sp_saved——Docker 版此前
        没传回调，网关内存配置不随保存更新（热重载缺口）。"""
        mp = str(tmp_path / "magic-proxy.json")
        sp = str(tmp_path / "suanpan.yaml")
        entry.bootstrap_default_config(sp, str(tmp_path))
        fired = []
        srv = entry.make_config_server(
            mp, sp, on_sp_saved=lambda: fired.append(1), port=0)
        assert srv.start()
        port = srv._server.server_address[1]
        try:
            import urllib.request
            body = (b'{"sp":{"listen_port":9527,"providers":{},'
                    b'"router":{},"rules":[]}}')
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/state", data=body,
                headers={"Authorization": f"Bearer {srv.token}",
                         "Content-Type": "application/json"},
                method="PUT")
            resp = urllib.request.urlopen(req)
            assert resp.status == 200
            assert fired == [1]  # 保存成功 → 回调触发
        finally:
            srv.stop()

    def test_on_sp_saved_not_fired_on_validation_failure(self, entry, tmp_path):
        """校验失败（422）不触发 on_sp_saved——只在完整提交后。"""
        mp = str(tmp_path / "magic-proxy.json")
        sp = str(tmp_path / "suanpan.yaml")
        entry.bootstrap_default_config(sp, str(tmp_path))
        fired = []
        srv = entry.make_config_server(
            mp, sp, on_sp_saved=lambda: fired.append(1), port=0)
        assert srv.start()
        port = srv._server.server_address[1]
        try:
            import urllib.request
            import urllib.error
            body = b'{"sp":{"listen_port":"not-a-port"}}'
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/state", data=body,
                headers={"Authorization": f"Bearer {srv.token}",
                         "Content-Type": "application/json"},
                method="PUT")
            with pytest.raises(urllib.error.HTTPError) as ei:
                urllib.request.urlopen(req)
            assert ei.value.code == 422
            assert fired == []  # 校验失败 → 不触发
        finally:
            srv.stop()


# ── 配置热重载：GatewayRunner（uvicorn 线程化 reload）────────────────

class TestGatewayRuntimeIntegration:
    """SuanpanRuntime(bind_host=...) 端到端：reload 把新配置换入运行中的
    网关（#40 语义，经真实 HTTP 观察）。"""

    def test_reload_picks_up_new_config(self, entry, tmp_path):
        import socket
        import time
        import urllib.request

        import yaml as _y

        sp = str(tmp_path / "suanpan.yaml")
        mp = str(tmp_path / "magic-proxy.json")
        entry.redirect_paths(sp, mp, str(tmp_path / "s.json"))
        entry.bootstrap_default_config(sp, str(tmp_path))
        # 自由端口，避免撞真实 9527
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        cfg = _y.safe_load(open(sp))
        cfg["listen_port"] = port
        open(sp, "w").write(_y.safe_dump(cfg))

        from services.suanpan_runtime import SuanpanRuntime
        rt = SuanpanRuntime(bind_host="127.0.0.1")
        assert rt.start()
        try:
            def models_text():
                return urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/v1/models", timeout=2
                ).read().decode("utf-8")

            for _ in range(50):
                try:
                    models_text()
                    break
                except Exception:
                    time.sleep(0.1)
            assert rt.running

            # 改配置（加 provider 模拟热更新场景）
            cfg = _y.safe_load(open(sp))
            cfg["providers"]["glm"] = {
                "base_url": "https://example.invalid", "api_key": "k",
                "auth_header": "x-api-key", "enabled": True,
                "models": ["glm-x"]}
            open(sp, "w").write(_y.safe_dump(cfg, allow_unicode=True))
            assert rt.reload()
            for _ in range(50):
                try:
                    if "glm" in models_text():
                        break
                except Exception:
                    pass
                time.sleep(0.1)
            assert rt.running
            assert "glm" in models_text()  # 新配置已换入内存
        finally:
            rt.stop()
        assert not rt.running

    def test_reload_noop_when_stopped(self, entry, tmp_path):
        from services.suanpan_runtime import SuanpanRuntime
        rt = SuanpanRuntime(bind_host="127.0.0.1")
        assert rt.reload() is True  # 未启动 → no-op 不炸
        assert not rt.running


# ── 配置热重载：run_serve 集成 ─────────────────────────────────────

class TestRunServeIntegration:
    def test_serve_wires_config_save_to_gateway_reload(self, entry, tmp_path,
                                                       monkeypatch):
        """run_serve：config server 的 on_sp_saved 指向网关 reload——
        配置页保存 → 网关热重载，无需重启容器。"""
        monkeypatch.setenv("SUANPAN_DATA_DIR", str(tmp_path))
        monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(tmp_path / "s.json"))
        captured = {}

        class FakeRunner:
            def __init__(self, bind_host=None):
                captured["bind_host"] = bind_host
                self.reload_called = 0

            def start(self):
                return True

            def reload(self):
                self.reload_called += 1
                return True

            def stop(self):
                pass

        class FakeCfgSrv:
            token = "t"
            url = "http://x/"

            def start(self):
                return True

            def stop(self):
                pass

        def fake_make(mp, sp, on_sp_saved=None):
            captured["on_sp_saved"] = on_sp_saved
            captured["mp"] = mp
            captured["sp_cfg"] = sp
            return FakeCfgSrv()

        monkeypatch.setattr(entry, "SuanpanRuntime", FakeRunner)
        monkeypatch.setattr(entry, "make_config_server", fake_make)
        # 主线程保活循环会永跑——sleep 第一次就 KeyboardInterrupt 跳出
        import time as _time
        monkeypatch.setattr(_time, "sleep",
                            lambda s: (_ for _ in ()).throw(KeyboardInterrupt))

        entry.run_serve()
        # 网关以 0.0.0.0 形态构造；on_sp_saved 已接线且指向 runner.reload
        assert captured["bind_host"] == "0.0.0.0"
        assert captured["sp_cfg"] == str(tmp_path / "suanpan.yaml")
        assert callable(captured["on_sp_saved"])
        captured["on_sp_saved"]()  # 模拟保存回调
