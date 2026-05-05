"""`secondsight sync` — backfill DB from filesystem and sync_log (P1-13).

Two recovery paths (see ``storage/filesystem_backfill.py`` for the full
contract):

    Path A — replay sync_log entries (server up, DB INSERT had failed).
    Path B — walk per-session ``events/*.json`` and INSERT any missing rows.
    Path C — archive ``fallback_events.jsonl`` to a timestamped .bak file.

Without flags `secondsight sync` runs Path A + B + C across every project
under ``<secondsight-home>/projects/``. Pass ``--project-id PID`` to scope
to one project. Pass ``--no-fallback-archive`` to skip Path C (useful if
you have an external archiver).

CLI exit codes:
    0 if every project ran clean (no failures);
    1 if any project reported failures (corrupt files, DB errors, etc.).

Output formats (SD §9.1):
    text — Rich table per project plus a summary line;
    json — single JSON document (one entry per project) for agent consumers.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from secondsight.api.registry import ProjectRegistry
from secondsight.cli._home import secondsight_home as resolve_secondsight_home
from secondsight.storage.filesystem_backfill import (
    BackfillReport,
    FallbackArchiveReport,
    FilesystemBackfill,
    archive_fallback_events,
)

app = typer.Typer(name="sync", help="Backfill SecondSight DB from filesystem traces.")
_console = Console()


@app.callback(invoke_without_command=True)
def sync(
    ctx: typer.Context,
    home: str = typer.Option(
        "",
        "--home",
        help="Override the SecondSight home directory (default: $SECONDSIGHT_HOME or ~/.secondsight).",
    ),
    project_id: str = typer.Option(
        "",
        "--project-id",
        help="Limit sync to one project_id (default: every project under <home>/projects/).",
    ),
    no_fallback_archive: bool = typer.Option(
        False,
        "--no-fallback-archive",
        help="Skip archiving fallback_events.jsonl (Path C).",
    ),
    output_format: str = typer.Option(
        "text",
        "--format",
        help="Output format: 'text' (default, Rich) or 'json'.",
    ),
) -> None:
    """Backfill DB from filesystem + sync_log + archive fallback file."""
    if ctx.invoked_subcommand is not None:  # pragma: no cover
        return

    home_path = resolve_secondsight_home(home)
    project_ids = _select_project_ids(home_path, project_id)

    # An asynchronous registry is overkill here (we run synchronously,
    # one project at a time, no concurrent first-event materialisations),
    # so we resolve resources via the lower-level _build_resources path.
    # We construct the registry only to share the validation it does on
    # ``home_path`` (must be absolute, must exist).
    registry = ProjectRegistry(secondsight_home=home_path)

    reports: list[dict[str, Any]] = []
    any_failure = False
    for pid in project_ids:
        # _build_resources is synchronous and has the same effect as the
        # async path's lazy materialisation, just without the per-project
        # asyncio.Lock that we don't need in a CLI context.
        resources = registry._build_resources(pid)  # noqa: SLF001
        try:
            backfill = FilesystemBackfill(resources)
            backfill_report = backfill.run()
        finally:
            resources.db_engine.dispose()

        archive_report: FallbackArchiveReport | None = None
        if not no_fallback_archive:
            # Fallback file is shared across projects (single ~/.secondsight
            # location), so we only archive it ONCE per `secondsight sync`
            # invocation — the first project's pass owns it. Otherwise
            # iterating N projects would archive it N times (the 2nd-Nth
            # passes would see an empty file and no-op; harmless but noisy).
            if pid == project_ids[0]:
                archive_report = archive_fallback_events(
                    home_path / "fallback_events.jsonl"
                )

        if backfill_report.failures:
            any_failure = True

        reports.append(
            {
                "project_id": pid,
                "backfill": _backfill_to_dict(backfill_report),
                "fallback_archive": (
                    _archive_to_dict(archive_report)
                    if archive_report is not None
                    else None
                ),
            }
        )

    if output_format == "json":
        typer.echo(json.dumps({"projects": reports}, indent=2, sort_keys=True))
    else:
        _render_text(reports)

    if any_failure:
        raise typer.Exit(code=1)


def _select_project_ids(home: Path, requested: str) -> list[str]:
    if requested:
        return [requested]
    projects_dir = home / "projects"
    if not projects_dir.is_dir():
        return []
    return sorted(
        child.name for child in projects_dir.iterdir() if child.is_dir()
    )


def _backfill_to_dict(report: BackfillReport) -> dict[str, Any]:
    return asdict(report)


def _archive_to_dict(report: FallbackArchiveReport) -> dict[str, Any]:
    return {
        "archived": report.archived,
        "archive_path": (
            str(report.archive_path) if report.archive_path is not None else None
        ),
        "line_count": report.line_count,
    }


def _render_text(reports: list[dict[str, Any]]) -> None:
    if not reports:
        _console.print("[yellow]No projects found under <home>/projects/[/yellow]")
        return
    table = Table(title="secondsight sync")
    table.add_column("project_id")
    table.add_column("sync_log replayed", justify="right")
    table.add_column("sync_log remaining", justify="right")
    table.add_column("fs inserted", justify="right")
    table.add_column("fs already-present", justify="right")
    table.add_column("failures", justify="right")
    for r in reports:
        b = r["backfill"]
        table.add_row(
            r["project_id"],
            str(b["sync_log_replayed"]),
            str(b["sync_log_remaining"]),
            str(b["filesystem_inserted"]),
            str(b["filesystem_already_present"]),
            str(len(b["failures"])),
        )
    _console.print(table)
    for r in reports:
        if r["backfill"]["failures"]:
            _console.print(
                f"[yellow]{r['project_id']}: failures[/yellow]"
            )
            for f in r["backfill"]["failures"]:
                _console.print(f"  - {f}")
        if r["fallback_archive"] and r["fallback_archive"]["archived"]:
            _console.print(
                f"  fallback file archived "
                f"({r['fallback_archive']['line_count']} line(s)) -> "
                f"{r['fallback_archive']['archive_path']}"
            )


__all__ = ["app"]
