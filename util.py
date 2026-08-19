"""Shared utility helpers used across modules.

Centralizes resource-path resolution (PyInstaller ``_MEIPASS`` fallback)
and text truncation so that controllers and views don't cross-import each
other for trivial helpers.
"""
import glob
import os
import sys
import time


# Dev-mode search dirs for bundled resources (frozen .app keeps them flat in
# _MEIPASS via --add-data "src:."; in dev they live in their domain packages).
_RESOURCE_DEV_DIRS = ("", "shellui", "capture", "sysctl", "docs", "docs/examples", "assets")


def resource_path(name: str) -> str:
    """Locate a bundled resource in dev and inside the PyInstaller .app."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    flat = os.path.join(base, name)
    if hasattr(sys, "_MEIPASS") or os.path.exists(flat):
        return flat
    for d in _RESOURCE_DEV_DIRS:
        cand = os.path.join(base, d, name)
        if os.path.exists(cand):
            return cand
    return flat


def _stamp_from_file(path: str):
    """Read a build-time stamp (MMDDHHMM) written by build.sh; None if absent."""
    try:
        with open(path) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _stamp_from_sources(root: str):
    """Dev-mode build-time proxy: newest source-file mtime as MMDDHHMM."""
    newest = 0.0
    for pat in ("*.py", "*.html", os.path.join("suanpan", "*.py")):
        for f in glob.glob(os.path.join(root, pat)):
            try:
                newest = max(newest, os.path.getmtime(f))
            except OSError:
                pass
    return time.strftime("%m%d%H%M", time.localtime(newest)) if newest else None


def build_stamp():
    """Build-time stamp MMDDHHMM: bundled ``build_time.txt`` in the packaged
    app, newest-source mtime in dev. None if undeterminable."""
    stamp = _stamp_from_file(resource_path("build_time.txt"))
    if stamp:
        return stamp
    if getattr(sys, "frozen", False):
        return None
    return _stamp_from_sources(os.path.dirname(os.path.abspath(__file__)))


def version_display(version: str, stamp=None) -> str:
    """``0.4.3`` + ``08102116`` → ``0.4.3.08102116`` (About-menu version)."""
    return f"{version}.{stamp}" if stamp else version


def truncate(s: str, n: int) -> str:
    """Truncate *s* to *n* characters, appending an ellipsis if cut."""
    return s if len(s) <= n else s[:n] + "…"
