"""Smoke an installed SecondSight wheel in a clean Python environment.

This script is intentionally standalone instead of a normal pytest test:
install-smoke mounts it into a container without the source tree so the checks
exercise the wheel artifact, not editable imports from ``src/``.
"""

from __future__ import annotations

import glob
import json
import subprocess
import sys
import tempfile
from pathlib import Path


def main(argv: list[str]) -> int:
    wheel = _single_wheel(argv[1:])

    _run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--disable-pip-version-check",
            wheel,
        ]
    )
    secondsight = str(Path(sys.executable).parent / "secondsight")
    _run([secondsight, "--version"])

    with tempfile.TemporaryDirectory(prefix="secondsight-smoke-") as tmp:
        root = Path(tmp)
        ss_home = root / "secondsight-home"
        claude_home = root / "claude-home"
        ss_home.mkdir()
        claude_home.mkdir()

        (claude_home / "settings.json").write_text("{}", encoding="utf-8")

        init_json = root / "init.json"
        with init_json.open("w", encoding="utf-8") as output:
            _run(
                [
                    secondsight,
                    "init",
                    "--claude-home",
                    str(claude_home),
                    "--secondsight-home",
                    str(ss_home),
                    "--format",
                    "json",
                ],
                stdout=output,
            )

        state = json.loads((ss_home / "state.json").read_text(encoding="utf-8"))
        if state.get("init_agent") != "claude_code":
            raise SystemExit(f"unexpected init_agent in state.json: {state!r}")
        _assert_has_files(claude_home / "hooks", "hook install")

    print(f"installed wheel smoke ok: {wheel}")
    return 0


def _single_wheel(patterns: list[str]) -> str:
    wheels: list[str] = []
    for pattern in patterns:
        wheels.extend(glob.glob(pattern))
    wheels = sorted(set(wheels))
    if len(wheels) != 1:
        raise SystemExit(f"expected exactly one wheel, found {len(wheels)}: {wheels}")
    return wheels[0]


def _run(
    command: list[str],
    *,
    stdout=None,
) -> None:
    subprocess.run(command, check=True, stdout=stdout)


def _assert_has_files(path: Path, label: str) -> None:
    if not path.is_dir() or not any(child.is_file() for child in path.iterdir()):
        raise SystemExit(f"{label} missing files: {path}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
