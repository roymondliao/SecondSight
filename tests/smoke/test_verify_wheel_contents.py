"""Verify that the built wheel carries required package resources."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

REQUIRED_PREFIXES = (
    "secondsight/_resources/dashboard/index.html",
    "secondsight/_resources/hooks",
)


def main(argv: list[str]) -> int:
    wheels = [Path(arg) for arg in argv[1:]]
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one wheel, found {len(wheels)}: {wheels}")

    wheel = wheels[0]
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()

    missing = [
        prefix for prefix in REQUIRED_PREFIXES if not any(name.startswith(prefix) for name in names)
    ]
    if missing:
        raise SystemExit(f"wheel missing bundled resources: {missing}")

    print(f"wheel contents ok: {wheel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
