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
    rebuild: bool = typer.Option(
        False,
        "--rebuild",
        help=(
            "Full DB rebuild from filesystem (GUR-108, P3B-7). "
            "Deletes and recreates the DB, then runs full filesystem backfill. "
            "Derived data (analysis results, directives) will be lost and "
            "must be re-generated via 'secondsight analyze'."
        ),
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

    if rebuild:
        _handle_rebuild(home_path, project_ids, output_format)
        return

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
        # asyncio.Lock that we don't need in a CLI context. A failure here
        # (corrupt DB, missing dir, permission error) for one project must
        # not silently abort the loop — record a per-project error entry
        # and continue, matching status.py's parity.
        try:
            resources = registry._build_resources(pid)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001 — surface to operator
            any_failure = True
            reports.append(
                {
                    "project_id": pid,
                    "error": f"{type(exc).__name__}: {exc}",
                    "backfill": None,
                }
            )
            continue
        # An unexpected raise inside backfill.run (SQLAlchemy connection
        # death, permission error during iterdir) would otherwise abort
        # every remaining project's sync. Surfacing it as a per-project
        # error keeps the loop honest. GUR-98 review-finding S1.
        try:
            try:
                backfill = FilesystemBackfill(resources)
                backfill_report = backfill.run()
            finally:
                resources.db_engine.dispose()
        except Exception as exc:  # noqa: BLE001 — surface to operator
            any_failure = True
            reports.append(
                {
                    "project_id": pid,
                    "error": f"backfill raised: {type(exc).__name__}: {exc}",
                    "backfill": None,
                }
            )
            continue

        if backfill_report.failures:
            any_failure = True

        reports.append(
            {
                "project_id": pid,
                "error": None,
                "backfill": asdict(backfill_report),
            }
        )

    # Archive fallback_events.jsonl OUTSIDE the project loop. The fallback
    # file lives at the SecondSight-home level, not per-project — running
    # it inside the loop both (a) made the archive run zero times when
    # there were zero projects (silent accumulation; review-finding C3)
    # and (b) implicitly tied the archive to the first project's success.
    # A single archive call after the loop is correct regardless of how
    # many projects we processed (including zero).
    archive_payload: dict[str, Any] | None = None
    if not no_fallback_archive:
        archive_report = archive_fallback_events(home_path / "fallback_events.jsonl")
        archive_payload = _archive_to_dict(archive_report)
        if archive_report.error is not None:
            # An unreadable fallback file is a hard failure: pending work
            # we cannot rotate. Surface it in the exit code so scripts
            # notice (review-finding C2).
            any_failure = True

    if output_format == "json":
        typer.echo(
            json.dumps(
                {"projects": reports, "fallback_archive": archive_payload},
                indent=2,
                sort_keys=True,
            )
        )
    else:
        _render_text(reports, archive_payload)

    if any_failure:
        raise typer.Exit(code=1)


def _handle_rebuild(
    home_path: Path,
    project_ids: list[str],
    output_format: str,
) -> None:
    """Full DB rebuild from filesystem (GUR-108, P3B-7).

    For each project:
    1. Delete the existing intelligence.db (if it exists).
    2. Run full filesystem backfill to re-insert all events.

    Derived data (behavior_flags, session_reports, directives,
    analysis_runs) is NOT recreated — the operator must run
    'secondsight analyze' to regenerate analysis results.

    This is the nuclear recovery option when the DB is corrupt or
    when the schema has changed in a way that requires a clean start.
    """
    import os
    import time

    registry = ProjectRegistry(secondsight_home=home_path)
    reports: list[dict[str, Any]] = []
    any_failure = False

    if not project_ids:
        _console.print("[yellow]No projects found under <home>/projects/[/yellow]")
        raise typer.Exit(code=0)

    _console.print(
        f"[bold red]REBUILD[/bold red]: Rebuilding DB for "
        f"{len(project_ids)} project(s). Existing databases will be deleted."
    )

    for pid in project_ids:
        project_dir = home_path / "projects" / pid
        db_path = project_dir / "intelligence.db"

        # Step 1: backup and delete existing DB.
        if db_path.exists():
            ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
            backup_path = db_path.with_name(f"intelligence.db.{ts}.pre-rebuild.bak")
            try:
                os.replace(db_path, backup_path)
                _console.print(
                    f"  {pid}: backed up DB to {backup_path.name}"
                )
            except OSError as exc:
                any_failure = True
                reports.append({
                    "project_id": pid,
                    "error": f"backup failed: {type(exc).__name__}: {exc}",
                    "backfill": None,
                })
                continue

            # Also remove WAL and SHM files if they exist.
            for suffix in (".db-wal", ".db-shm"):
                wal_path = project_dir / f"intelligence{suffix}"
                if wal_path.exists():
                    try:
                        wal_path.unlink()
                    except OSError:
                        pass

        # Step 2: run full filesystem backfill.
        try:
            resources = registry._build_resources(pid)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001
            any_failure = True
            reports.append({
                "project_id": pid,
                "error": f"{type(exc).__name__}: {exc}",
                "backfill": None,
            })
            continue

        try:
            try:
                backfill = FilesystemBackfill(resources)
                backfill_report = backfill.run()
            finally:
                resources.db_engine.dispose()
        except Exception as exc:  # noqa: BLE001
            any_failure = True
            reports.append({
                "project_id": pid,
                "error": f"backfill raised: {type(exc).__name__}: {exc}",
                "backfill": None,
            })
            continue

        if backfill_report.failures:
            any_failure = True

        reports.append({
            "project_id": pid,
            "error": None,
            "backfill": asdict(backfill_report),
        })
        _console.print(
            f"  {pid}: rebuilt — {backfill_report.filesystem_inserted} "
            f"events inserted, {len(backfill_report.failures)} failures"
        )

    if output_format == "json":
        typer.echo(json.dumps({"rebuild": True, "projects": reports}, indent=2, sort_keys=True))
    else:
        _render_text(reports, None)
        _console.print(
            "\n[yellow]Note:[/yellow] Analysis results (directives, reports) "
            "were not rebuilt. Run 'secondsight analyze' to regenerate."
        )

    if any_failure:
        raise typer.Exit(code=1)
    raise typer.Exit(code=0)


def _select_project_ids(home: Path, requested: str) -> list[str]:
    if requested:
        return [requested]
    projects_dir = home / "projects"
    if not projects_dir.is_dir():
        return []
    return sorted(child.name for child in projects_dir.iterdir() if child.is_dir())


def _archive_to_dict(report: FallbackArchiveReport) -> dict[str, Any]:
    return {
        "archived": report.archived,
        "archive_path": (str(report.archive_path) if report.archive_path is not None else None),
        "line_count": report.line_count,
        "error": report.error,
    }


def _render_text(reports: list[dict[str, Any]], archive_payload: dict[str, Any] | None) -> None:
    if not reports:
        _console.print("[yellow]No projects found under <home>/projects/[/yellow]")
    else:
        table = Table(title="secondsight sync")
        table.add_column("project_id")
        table.add_column("sync_log replayed", justify="right")
        table.add_column("sync_log remaining", justify="right")
        table.add_column("fs inserted", justify="right")
        table.add_column("fs already-present", justify="right")
        table.add_column("failures", justify="right")
        for r in reports:
            if r.get("error") or r.get("backfill") is None:
                table.add_row(r["project_id"], "?", "?", "?", "?", "ERR")
                continue
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
            if r.get("error"):
                _console.print(f"[red]{r['project_id']}: {r['error']}[/red]")
            elif r["backfill"]["failures"]:
                _console.print(f"[yellow]{r['project_id']}: failures[/yellow]")
                for f in r["backfill"]["failures"]:
                    _console.print(f"  - {f}")

    if archive_payload is None:
        return
    if archive_payload.get("error"):
        _console.print(f"[red]fallback file unreadable:[/red] {archive_payload['error']}")
    elif archive_payload.get("archived"):
        _console.print(
            f"fallback file archived "
            f"({archive_payload['line_count']} line(s)) -> "
            f"{archive_payload['archive_path']}"
        )


__all__ = ["app"]
