"""Top-level Typer application — composes the four P1 subcommands.

CLI tree (per SD §9.2)::

    secondsight
    ├── init    # P1-11/12 — install hook scripts + register with Claude Code
    ├── serve   # (existing) — daemon lifecycle
    ├── status  # P1-12 — server + per-project status
    └── sync    # P1-13 — filesystem -> DB backfill

`main()` is wired in as the ``[project.scripts]`` entry point, replacing
the temporary ``__main__.main`` shim from GUR-112's install smoke test.

The top-level help text intentionally surfaces SD §9.1 dual-persona usage
(human + agent consumers via ``--format json``) so a freshly installed
``secondsight`` is self-documenting at the prompt.
"""

from __future__ import annotations

import sys
from importlib.metadata import PackageNotFoundError, version

import typer

from secondsight.cli import init as init_cmd
from secondsight.cli import serve as serve_cmd
from secondsight.cli import status as status_cmd
from secondsight.cli import sync as sync_cmd

app = typer.Typer(
    name="secondsight",
    help=(
        "SecondSight — observation + analysis for coding agents.\n\n"
        "Most subcommands accept --format json for agent-friendly output."
    ),
    add_completion=False,
    no_args_is_help=True,
)

# Each subcommand module exposes its own Typer app. We mount them as named
# subcommands so the help tree mirrors the SD §9.2 layout.
app.add_typer(init_cmd.app, name="init")
app.add_typer(serve_cmd.app, name="serve")
app.add_typer(status_cmd.app, name="status")
app.add_typer(sync_cmd.app, name="sync")


@app.command(name="version")
def version_cmd() -> None:
    """Print the installed package version."""
    try:
        ver = version("secondsight")
    except PackageNotFoundError:  # pragma: no cover — dev edge-case
        typer.echo("secondsight: package metadata not found", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"secondsight {ver}")


def main(argv: list[str] | None = None) -> int:
    """Console-script entry point.

    `argv=None` defers to sys.argv (Typer's normal behaviour). We accept an
    explicit list for tests so they can drive the full CLI without
    monkey-patching sys.argv.

    --version / -V are handled here (before delegating to Typer) for
    backwards compatibility with the GUR-112 install-smoke test that
    invokes ``secondsight --version``. Typer's own ``--version`` is
    awkward at the top-level (it would need a callback), so we keep the
    pre-Typer shortcut.
    """
    args = sys.argv[1:] if argv is None else list(argv)
    if args and args[0] in ("--version", "-V"):
        try:
            ver = version("secondsight")
        except PackageNotFoundError:
            print("secondsight: package metadata not found", file=sys.stderr)
            return 1
        print(f"secondsight {ver}")
        return 0

    try:
        app(args=args, standalone_mode=False)
    except typer.Exit as exc:
        return int(exc.exit_code)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 1
    return 0


__all__ = ["app", "main"]
