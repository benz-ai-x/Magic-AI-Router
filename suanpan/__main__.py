"""Entry point: `python -m suanpan` or `suanpan` (after pip install)."""

import os
import sys

from suanpan.main import run_from_config_path


def main() -> None:
    config_path = os.environ.get("SUANPAN_CONFIG", "./suanpan.yaml")
    if len(sys.argv) > 1:
        config_path = sys.argv[1]
    run_from_config_path(config_path)


if __name__ == "__main__":
    main()
