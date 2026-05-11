"""Console-script entry point for the `secondsight` CLI (GUR-98 / P1-12).

Pre-GUR-98 this file shipped a stub that supported only ``--version``
(see GUR-112 install smoke test). GUR-98 wires the full Typer app
(:mod:`secondsight.cli.app`); this module now just delegates so the
``[project.scripts]`` mapping in pyproject.toml stays a stable target.

The thin ``if __name__ == '__main__'`` shim lets ``python -m secondsight``
work in the same way as the installed entry point — useful for tests and
for ``uv run python -m secondsight`` during local development.
"""

from __future__ import annotations

import sys

from secondsight.cli.app import main


if __name__ == "__main__":
    sys.exit(main())
