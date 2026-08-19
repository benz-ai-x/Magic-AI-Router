import os
import subprocess
from unittest.mock import MagicMock, patch

from tunnel import host_key
def _cp(returncode=0, stdout="", stderr=""):
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = stderr
    return cp


def test_existing_key_is_accepted_without_scan(tmp_path, monkeypatch):
    path = tmp_path / "known_hosts"
    path.write_text("example")
    monkeypatch.setattr(host_key, "KNOWN_HOSTS_PATH", str(path))
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    with patch("subprocess.run", return_value=_cp(stdout="# found\nexample")) as run:
        known, keys, fingerprints, err = host_key.inspect(
            {"ssh_host": "example.com", "ssh_port": 22}
        )
    assert known and not err and not keys and not fingerprints
    assert run.call_count == 1


def test_scan_returns_fingerprint_for_user_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(host_key, "KNOWN_HOSTS_PATH", str(tmp_path / "missing"))
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    results = [
        _cp(stdout="example.com ssh-ed25519 AAAA\n"),
        _cp(stdout="256 SHA256:abc example.com (ED25519)\n"),
    ]
    with patch("subprocess.run", side_effect=results):
        known, keys, fingerprints, err = host_key.inspect(
            {"ssh_host": "example.com", "ssh_port": 22}
        )
    assert not known and not err
    assert "ssh-ed25519" in keys
    assert "SHA256:abc" in fingerprints


def test_accept_uses_private_permissions(tmp_path, monkeypatch):
    path = tmp_path / "known_hosts"
    monkeypatch.setattr(host_key, "KNOWN_HOSTS_PATH", str(path))
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    assert host_key.accept("example.com ssh-ed25519 AAAA")
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_rejects_option_like_host(tmp_path, monkeypatch):
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    known, _, _, err = host_key.inspect({"ssh_host": "-oProxyCommand=bad", "ssh_port": 22})
    assert not known
    assert "不安全字符" in err


def test_replace_updates_only_selected_host(tmp_path, monkeypatch):
    path = tmp_path / "known_hosts"
    path.write_text("a.example ssh-ed25519 OLD\nb.example ssh-ed25519 KEEP\n")
    monkeypatch.setattr(host_key, "KNOWN_HOSTS_PATH", str(path))
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    assert host_key.replace(
        {"ssh_host": "a.example", "ssh_port": 22},
        "a.example ssh-ed25519 NEW",
    )
    text = path.read_text()
    assert "OLD" not in text
    assert "KEEP" in text
    assert "NEW" in text


def test_invalid_port_type_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    known, _, _, err = host_key.inspect({"ssh_host": "example.com", "ssh_port": "abc"})
    assert not known
    assert "端口无效" in err


def test_port_out_of_range_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    known, _, _, err = host_key.inspect({"ssh_host": "example.com", "ssh_port": 99999})
    assert not known
    assert "无效" in err


def test_known_lookup_subprocess_error(tmp_path, monkeypatch):
    path = tmp_path / "known_hosts"
    path.write_text("example")
    monkeypatch.setattr(host_key, "KNOWN_HOSTS_PATH", str(path))
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    with patch("subprocess.run", side_effect=OSError("ssh-keygen missing")):
        known, _, _, err = host_key.inspect({"ssh_host": "example.com", "ssh_port": 22})
    assert not known
    assert "ssh-keygen missing" in err


def test_scan_subprocess_error(tmp_path, monkeypatch):
    monkeypatch.setattr(host_key, "KNOWN_HOSTS_PATH", str(tmp_path / "missing"))
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    with patch("subprocess.run", side_effect=OSError("ssh-keyscan missing")):
        known, _, _, err = host_key.inspect({"ssh_host": "example.com", "ssh_port": 22})
    assert not known
    assert "无法扫描" in err


def test_scan_no_keys_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(host_key, "KNOWN_HOSTS_PATH", str(tmp_path / "missing"))
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    with patch("subprocess.run", return_value=_cp(returncode=1, stdout="", stderr="timeout")):
        known, _, _, err = host_key.inspect({"ssh_host": "example.com", "ssh_port": 22})
    assert not known
    assert "timeout" in err


def test_fingerprint_subprocess_error(tmp_path, monkeypatch):
    monkeypatch.setattr(host_key, "KNOWN_HOSTS_PATH", str(tmp_path / "missing"))
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    results = [
        _cp(stdout="example.com ssh-ed25519 AAAA\n"),
        OSError("ssh-keygen missing"),
    ]
    with patch("subprocess.run", side_effect=results):
        known, _, _, err = host_key.inspect({"ssh_host": "example.com", "ssh_port": 22})
    assert not known
    assert "无法计算" in err


def test_fingerprint_failure_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(host_key, "KNOWN_HOSTS_PATH", str(tmp_path / "missing"))
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    results = [
        _cp(stdout="example.com ssh-ed25519 AAAA\n"),
        _cp(returncode=1, stdout="", stderr="bad key"),
    ]
    with patch("subprocess.run", side_effect=results):
        known, _, _, err = host_key.inspect({"ssh_host": "example.com", "ssh_port": 22})
    assert not known
    assert "bad key" in err


def test_accept_empty_keys_returns_false():
    assert host_key.accept("   ") is False


def test_accept_oserror_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    monkeypatch.setattr(host_key, "KNOWN_HOSTS_PATH", str(tmp_path / "known_hosts"))
    with patch("os.open", side_effect=OSError("cannot open")):
        assert host_key.accept("example.com ssh-ed25519 AAAA") is False


def test_ensure_storage_rejects_non_directory(tmp_path, monkeypatch):
    import stat as stat_mod
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    fake = MagicMock()
    fake.st_mode = stat_mod.S_IFREG  # regular file, not a directory
    fake.st_uid = os.getuid()
    with patch("os.lstat", return_value=fake):
        try:
            host_key._ensure_storage()
            assert False, "expected OSError"
        except OSError as e:
            assert "不是普通目录" in str(e)


def test_ensure_storage_rejects_wrong_owner(tmp_path, monkeypatch):
    import stat as stat_mod
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    fake = MagicMock()
    fake.st_mode = stat_mod.S_IFDIR
    fake.st_uid = os.getuid() + 1  # different owner
    with patch("os.lstat", return_value=fake):
        try:
            host_key._ensure_storage()
            assert False, "expected OSError"
        except OSError as e:
            assert "所有者" in str(e)


def test_replace_invalid_host_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    assert host_key.replace({"ssh_host": "-bad", "ssh_port": 22}, "key") is False


def test_accept_rejects_non_regular_file(tmp_path, monkeypatch):
    import stat as stat_mod
    path = tmp_path / "known_hosts"
    monkeypatch.setattr(host_key, "KNOWN_HOSTS_PATH", str(path))
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    fake = MagicMock()
    fake.st_mode = stat_mod.S_IFDIR
    fake.st_uid = os.getuid()
    with patch("os.fstat", return_value=fake):
        assert host_key.accept("example.com ssh-ed25519 AAAA") is False


def test_replace_rejects_non_regular_existing(tmp_path, monkeypatch):
    import stat as stat_mod
    path = tmp_path / "known_hosts"
    path.write_text("old entry\n")
    monkeypatch.setattr(host_key, "KNOWN_HOSTS_PATH", str(path))
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    fake = MagicMock()
    fake.st_mode = stat_mod.S_IFDIR
    fake.st_uid = os.getuid()
    with patch("os.fstat", return_value=fake):
        assert host_key.replace({"ssh_host": "example.com", "ssh_port": 22}, "key") is False


def test_replace_none_ssh_port_returns_false(tmp_path, monkeypatch):
    """Regression: int(None) raises TypeError; replace must catch it and
    return False instead of propagating."""
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    monkeypatch.setattr(host_key, "KNOWN_HOSTS_PATH", str(tmp_path / "kh"))
    assert host_key.replace({"ssh_host": "example.com", "ssh_port": None}, "key") is False


def test_replace_non_numeric_ssh_port_returns_false(tmp_path, monkeypatch):
    """A non-numeric ssh_port (list/dict) triggers TypeError in int()."""
    monkeypatch.setattr(host_key, "APP_SECURITY_DIR", str(tmp_path))
    monkeypatch.setattr(host_key, "KNOWN_HOSTS_PATH", str(tmp_path / "kh"))
    assert host_key.replace(
        {"ssh_host": "example.com", "ssh_port": [1, 2]}, "key") is False
