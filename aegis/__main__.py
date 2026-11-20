"""`python -m aegis` and the installed `aegis` command."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    from run import main as run_main

    return run_main(argv)


if __name__ == "__main__":
    sys.exit(main())
