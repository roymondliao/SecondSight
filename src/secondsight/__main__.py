"""Stub `secondsight` console-script entry point.

Wired up so `pip install secondsight && secondsight --version` works end-to-end
in CI's install smoke test (GUR-112). GUR-98 will replace this with the full
Typer CLI (`init`/`serve`/`status`) and re-point `[project.scripts]`.
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 1 and args[0] in ("--version", "-V"):
        try:
            ver = version("secondsight")
        except PackageNotFoundError:
            print("secondsight: package metadata not found", file=sys.stderr)
            return 1
        print(f"secondsight {ver}")
        return 0
    print(
        "secondsight: CLI not yet wired up (tracked in GUR-98); only --version is available",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
