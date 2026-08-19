"""Tests for util: resource_path, truncate, build-time stamp, version display."""
import os
import sys
import time

import util


class TestVersionDisplay:
    def test_stamp_appended_with_dot(self):
        assert util.version_display("0.4.3", "08102116") == "0.4.3.08102116"

    def test_no_stamp_returns_bare_version(self):
        assert util.version_display("0.4.3") == "0.4.3"
        assert util.version_display("0.4.3", None) == "0.4.3"
        assert util.version_display("0.4.3", "") == "0.4.3"


class TestStampFromFile:
    def test_reads_stamp_and_strips_newline(self, tmp_path):
        p = tmp_path / "build_time.txt"
        p.write_text("08102116\n")
        assert util._stamp_from_file(str(p)) == "08102116"

    def test_missing_file_returns_none(self, tmp_path):
        assert util._stamp_from_file(str(tmp_path / "nope.txt")) is None

    def test_empty_file_returns_none(self, tmp_path):
        p = tmp_path / "build_time.txt"
        p.write_text("")
        assert util._stamp_from_file(str(p)) is None


class TestStampFromSources:
    FIXED = 1700000000  # 2023-11-14 — exact time string derived via localtime

    def test_newest_mtime_wins(self, tmp_path):
        old = tmp_path / "a.py"
        old.write_text("")
        new = tmp_path / "b.py"
        new.write_text("")
        os.utime(old, (1000000000, 1000000000))
        os.utime(new, (self.FIXED, self.FIXED))
        expected = time.strftime("%m%d%H%M", time.localtime(self.FIXED))
        assert util._stamp_from_sources(str(tmp_path)) == expected

    def test_suanpan_subdir_scanned(self, tmp_path):
        (tmp_path / "suanpan").mkdir()
        f = tmp_path / "suanpan" / "x.py"
        f.write_text("")
        os.utime(f, (self.FIXED, self.FIXED))
        expected = time.strftime("%m%d%H%M", time.localtime(self.FIXED))
        assert util._stamp_from_sources(str(tmp_path)) == expected

    def test_html_counted(self, tmp_path):
        f = tmp_path / "config_ui.html"
        f.write_text("")
        os.utime(f, (self.FIXED, self.FIXED))
        expected = time.strftime("%m%d%H%M", time.localtime(self.FIXED))
        assert util._stamp_from_sources(str(tmp_path)) == expected

    def test_empty_dir_returns_none(self, tmp_path):
        assert util._stamp_from_sources(str(tmp_path)) is None


class TestBuildStamp:
    def test_bundled_file_wins(self, monkeypatch, tmp_path):
        (tmp_path / "build_time.txt").write_text("08102116")
        monkeypatch.setattr(util, "resource_path", lambda name: str(tmp_path / name))
        monkeypatch.delattr(sys, "frozen", raising=False)
        assert util.build_stamp() == "08102116"

    def test_frozen_without_file_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(util, "resource_path", lambda name: str(tmp_path / name))
        monkeypatch.setattr(sys, "frozen", True, raising=False)
        assert util.build_stamp() is None

    def test_dev_falls_back_to_source_mtimes(self, monkeypatch, tmp_path):
        # No stamp file anywhere; not frozen → scans the repo's own sources.
        monkeypatch.setattr(util, "resource_path", lambda name: str(tmp_path / name))
        monkeypatch.delattr(sys, "frozen", raising=False)
        stamp = util.build_stamp()
        assert stamp is not None
        assert len(stamp) == 8 and stamp.isdigit()
