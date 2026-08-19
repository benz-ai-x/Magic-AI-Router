"""App-name guard — the user-visible product name is "Magic AI Router".

Commit 65f0bae renamed the app with a typo ("Mage" for "Magic") that
then propagated into packaging, CI and docs (c38c210, f6910f6).
This guard keeps the misspelling from sneaking back into any tracked file,
and pins the canonical name on the surfaces users and the build actually
read (rumps name, PyInstaller --name, APP_NAME, .app paths).
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Built by concatenation so this file itself never contains the phrase.
_TYPO = "Mage" + " AI Router"
CANONICAL = "Magic AI Router"

# Packaging/release surfaces that must spell the product name exactly.
_PACKAGING_PINS = {
    "app.py": 'name="Magic AI Router"',
    "build.sh": '--name "Magic AI Router"',
    "scripts/build_dmg.sh": 'APP_NAME="Magic AI Router"',
    "scripts/notarize.sh": 'APP_NAME="Magic AI Router"',
}


class TestNoMisspelledAppName:
    def test_typo_absent_repo_wide(self):
        """No git-tracked file may contain the misspelled product name.

        Scans tracked files only — untracked build artifacts (dist/,
        build/, *.spec) and caches (__pycache__, .playwright-mcp/) are
        regenerated and die with the next `bash build.sh`; binaries are
        tolerated via errors="ignore".
        """
        ls = subprocess.run(
            ["git", "ls-files", "-z"], cwd=ROOT,
            capture_output=True, check=True)
        offenders = [
            rel for rel in ls.stdout.decode().strip("\0").split("\0")
            if rel and _TYPO in (ROOT / rel).read_text(
                encoding="utf-8", errors="ignore")
        ]
        assert not offenders, f"misspelled app name found in: {offenders}"


class TestCanonicalNamePinned:
    def test_packaging_surfaces_pin_canonical_name(self):
        for rel, pin in _PACKAGING_PINS.items():
            text = (ROOT / rel).read_text(encoding="utf-8")
            assert pin in text, f"{rel} lost the canonical name pin: {pin!r}"
