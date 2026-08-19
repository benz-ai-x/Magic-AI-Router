"""PyInstaller entry point for the bundled mitmdump helper (ADR-022).

Not invoked by app.py yet -- that wiring is ADR-022 Task 2/4. This module
exists solely so `PyInstaller` has a concrete script to analyze; it just
delegates straight into mitmproxy's own CLI entry point.
"""
import sys

from mitmproxy.tools.main import mitmdump

if __name__ == "__main__":
    sys.exit(mitmdump())
