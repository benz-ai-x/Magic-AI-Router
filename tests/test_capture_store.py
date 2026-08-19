import os

import pytest

import capture_store


def test_refuses_existing_unmarked_custom_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(capture_store, "_home_dir", lambda: os.path.realpath(tmp_path))
    existing = tmp_path / "Documents"
    existing.mkdir(mode=0o755)
    with pytest.raises(OSError, match="非 Magic AI Router"):
        capture_store.prepare(str(existing))
    assert os.stat(existing).st_mode & 0o777 == 0o755


def test_refuses_symlink_store(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(capture_store, "_home_dir", lambda: os.path.realpath(tmp_path))
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "captures"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError, match="安全"):
        capture_store.prepare(str(link))


def test_new_custom_store_gets_marker_and_private_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(capture_store, "_home_dir", lambda: os.path.realpath(tmp_path))
    store = tmp_path / "captures"
    result = capture_store.prepare(str(store))
    assert result == str(store)
    assert (store / capture_store.MARKER).exists()
    assert os.stat(store).st_mode & 0o777 == 0o700


def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(capture_store, "_home_dir", lambda: os.path.realpath(tmp_path))
    return tmp_path


def test_clean_removes_files_but_keeps_dir_and_marker(tmp_path, monkeypatch):
    home = _isolated_home(tmp_path, monkeypatch)
    store = home / "captures"
    capture_store.prepare(str(store))
    for name in ("2026-08-18.jsonl", "2026-08-17.jsonl", "2026-08-16.jsonl.1"):
        (store / name).write_text("{}\n")
    removed = capture_store.clean(str(store))
    assert removed == 3
    assert store.is_dir()
    assert (store / capture_store.MARKER).exists()
    assert list(store.iterdir()) == [store / capture_store.MARKER]


def test_clean_creates_missing_dir_and_reports_zero(tmp_path, monkeypatch):
    home = _isolated_home(tmp_path, monkeypatch)
    store = home / "captures"
    removed = capture_store.clean(str(store))
    assert removed == 0
    assert store.is_dir()
    assert (store / capture_store.MARKER).exists()


def test_clean_skips_marker_subdirs_and_symlinks(tmp_path, monkeypatch):
    home = _isolated_home(tmp_path, monkeypatch)
    store = home / "captures"
    capture_store.prepare(str(store))
    sub = store / "subdir"
    sub.mkdir()
    target = home / "elsewhere.jsonl"
    target.write_text("{}\n")
    link = store / "link.jsonl"
    link.symlink_to(target)
    (store / "2026-08-18.jsonl").write_text("{}\n")
    removed = capture_store.clean(str(store))
    assert removed == 1
    assert sub.is_dir()
    assert link.is_symlink()
    assert target.exists()  # symlink target untouched


def test_clean_refuses_unmarked_foreign_dir(tmp_path, monkeypatch):
    home = _isolated_home(tmp_path, monkeypatch)
    foreign = home / "Documents"
    foreign.mkdir(mode=0o755)
    with pytest.raises(OSError, match="非 Magic AI Router"):
        capture_store.clean(str(foreign))


def test_clean_tolerates_vanishing_files(tmp_path, monkeypatch):
    home = _isolated_home(tmp_path, monkeypatch)
    store = home / "captures"
    capture_store.prepare(str(store))
    (store / "a.jsonl").write_text("{}\n")
    (store / "b.jsonl").write_text("{}\n")
    real_unlink = os.unlink

    def flaky_unlink(path, **kwargs):
        # "a.jsonl" vanishes between scandir and unlink — scandir order is
        # filesystem-dependent, so key off the path, not the call index.
        if str(path).endswith("a.jsonl"):
            raise FileNotFoundError(path)
        return real_unlink(path, **kwargs)

    monkeypatch.setattr(capture_store.os, "unlink", flaky_unlink)
    removed = capture_store.clean(str(store))
    # The vanished file is skipped, not counted; the survivor is removed.
    assert removed == 1
    assert not (store / "b.jsonl").exists()
