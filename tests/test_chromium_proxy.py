"""Tests for chromium_proxy (pure logic + resolver, no live app)."""
from unittest.mock import patch, MagicMock

from capture import chromium_proxy
def test_launch_args_carries_proxy_server():
    assert chromium_proxy.launch_args("127.0.0.1:8888") == ["--proxy-server=127.0.0.1:8888"]


def test_launch_args_reflects_custom_address():
    assert "--proxy-server=10.0.0.2:8080" in chromium_proxy.launch_args("10.0.0.2:8080")


def test_known_apps_include_chatgpt_and_claude():
    keys = {e["key"] for e in chromium_proxy.KNOWN_APPS}
    assert {"chatgpt", "claude"} <= keys


def test_app_stem_strips_dot_app():
    assert chromium_proxy._app_stem("/Applications/Claude.app") == "Claude"
    assert chromium_proxy._app_stem("/Applications/ChatGPT.app/") == "ChatGPT"


def test_app_path_prefers_default_when_present(tmp_path):
    default = tmp_path / "Claude.app"
    default.mkdir()
    entry = {"bundle_id": "com.x", "default_path": str(default)}
    assert chromium_proxy.app_path(entry) == str(default)


def test_app_path_none_when_absent_and_mdfind_empty(tmp_path):
    entry = {"bundle_id": "com.x", "default_path": str(tmp_path / "nope.app")}
    with patch("subprocess.run", return_value=MagicMock(stdout="", returncode=0)):
        assert chromium_proxy.app_path(entry) is None


def test_app_path_falls_back_to_mdfind(tmp_path):
    found = tmp_path / "Found.app"
    found.mkdir()
    entry = {"bundle_id": "com.x", "default_path": str(tmp_path / "nope.app")}
    with patch("subprocess.run", return_value=MagicMock(stdout=f"{found}\n", returncode=0)):
        assert chromium_proxy.app_path(entry) == str(found)


def test_installed_apps_filters_to_resolvable(tmp_path):
    present = tmp_path / "ChatGPT.app"
    present.mkdir()
    fake_registry = [
        {"key": "chatgpt", "name": "ChatGPT", "bundle_id": "a", "default_path": str(present)},
        {"key": "claude", "name": "Claude", "bundle_id": "b", "default_path": str(tmp_path / "missing.app")},
    ]
    with patch.object(chromium_proxy, "KNOWN_APPS", fake_registry), \
         patch("subprocess.run", return_value=MagicMock(stdout="", returncode=0)):
        apps = chromium_proxy.installed_apps()
    assert [a["key"] for a in apps] == ["chatgpt"]
    assert apps[0]["path"] == str(present)


def test_is_running_true_on_zero_returncode():
    with patch("subprocess.run", return_value=MagicMock(returncode=0)):
        assert chromium_proxy.is_running("/Applications/Claude.app") is True


def test_is_running_escapes_regex_metacharacters():
    """#40: pgrep -f treats the pattern as a regex — a literal path with
    metacharacters must be escaped or it mis-matches other apps."""
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args
        return MagicMock(returncode=1)

    path = "/Applications/Claude+(beta).app"
    with patch("subprocess.run", side_effect=fake_run):
        chromium_proxy.is_running(path)
    pattern = captured["args"][captured["args"].index("-f") + 1]
    assert pattern == "Claude\\+\\(beta\\)\\.app/Contents/MacOS/Claude\\+\\(beta\\)"


def test_is_running_false_on_nonzero():
    with patch("subprocess.run", return_value=MagicMock(returncode=1)):
        assert chromium_proxy.is_running("/Applications/Claude.app") is False


def test_launch_builds_open_command_with_flag():
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args
        return MagicMock(returncode=0, stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        ok, err = chromium_proxy.launch("/Applications/Claude.app", "127.0.0.1:8888")
    assert ok and err == ""
    assert captured["args"][:3] == ["open", "-a", "/Applications/Claude.app"]
    assert "--proxy-server=127.0.0.1:8888" in captured["args"]


def test_launch_reports_error_on_failure():
    with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="boom")):
        ok, err = chromium_proxy.launch("/x/Claude.app", "127.0.0.1:8888")
    assert not ok and err == "boom"


def test_quit_app_uses_app_stem():
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args
        return MagicMock(returncode=0, stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        ok, _ = chromium_proxy.quit_app("/Applications/Claude.app")
    assert ok
    assert 'tell application "Claude" to quit' in captured["args"]


def test_quit_app_escapes_apple_script_stem():
    """#40: a quote in the app name must not break out of the tell string."""
    captured = {}

    def fake_run(args, **kw):
        captured["args"] = args
        return MagicMock(returncode=0, stderr="")

    with patch("subprocess.run", side_effect=fake_run):
        ok, _ = chromium_proxy.quit_app('/Applications/My "AI".app')
    assert ok
    assert 'tell application "My \\"AI\\"" to quit' in captured["args"]


def test_app_path_none_without_bundle_id(tmp_path):
    entry = {"default_path": str(tmp_path / "nope.app")}  # no bundle_id
    assert chromium_proxy.app_path(entry) is None


def test_app_path_none_on_subprocess_error(tmp_path):
    entry = {"bundle_id": "com.x", "default_path": str(tmp_path / "nope.app")}
    with patch("subprocess.run", side_effect=OSError("mdfind missing")):
        assert chromium_proxy.app_path(entry) is None


def test_is_running_false_on_subprocess_error():
    with patch("subprocess.run", side_effect=OSError("pgrep missing")):
        assert chromium_proxy.is_running("/Applications/Claude.app") is False


def test_quit_app_error_on_subprocess_exception():
    with patch("subprocess.run", side_effect=OSError("osascript missing")):
        ok, err = chromium_proxy.quit_app("/Applications/Claude.app")
    assert not ok
    assert "osascript missing" in err


def test_quit_app_error_on_nonzero_returncode():
    with patch("subprocess.run", return_value=MagicMock(returncode=1, stderr="not found")):
        ok, err = chromium_proxy.quit_app("/Applications/Claude.app")
    assert not ok
    assert err == "not found"


def test_wait_until_stopped_returns_true_when_stops():
    with patch.object(chromium_proxy, "is_running", side_effect=[True, False]):
        assert chromium_proxy.wait_until_stopped("/x/A.app", timeout=1, interval=0.01) is True


def test_wait_until_stopped_times_out():
    with patch.object(chromium_proxy, "is_running", return_value=True):
        assert chromium_proxy.wait_until_stopped("/x/A.app", timeout=0.05, interval=0.01) is False


def test_launch_error_on_subprocess_exception():
    with patch("subprocess.run", side_effect=OSError("open missing")):
        ok, err = chromium_proxy.launch("/x/A.app", "127.0.0.1:8888")
    assert not ok
    assert "open missing" in err


# ── RelaunchWaiter: non-blocking quit→relaunch state machine (#40) ──

def test_relaunch_waiter_returns_none_while_running():
    w = chromium_proxy.RelaunchWaiter("/x/A.app", "App", "127.0.0.1:8888")
    assert w.step(is_running_fn=lambda p: True) is None


def test_relaunch_waiter_launches_when_exited():
    w = chromium_proxy.RelaunchWaiter("/x/A.app", "App", "127.0.0.1:8888")
    assert w.step(is_running_fn=lambda p: False) == ("launch", "127.0.0.1:8888")


def test_relaunch_waiter_times_out():
    w = chromium_proxy.RelaunchWaiter("/x/A.app", "App", "127.0.0.1:8888", timeout=0.0)
    assert w.step(is_running_fn=lambda p: True) == ("timeout", None)
