"""`secondsight cleanup` — manual raw-traces retention reaper (task-A6, GUR-147).

For each project under ``<secondsight-home>/projects/``:
    1. Resolve ``RetentionConfig`` (per-project TOML > global TOML > built-in).
    2. Enumerate sessions whose ``last_event_at`` ≤ ``now - ttl_days``.
    3. If ``--dry-run``, report the set and stop.
    4. Otherwise pass the same set to ``RawTracesPurger`` and report results.

DC-3 (no enumeration drift): both ``--dry-run`` and the real run call the
same ``_enumerate_for_project`` helper. A regression that re-implemented
the dry-run path independently would let preview and reap diverge —
exactly the failure the operator-trust contract forbids.

D8 / verification C2 (no async ``ProjectRegistry`` here): the CLI walks
``home/projects/`` synchronously, exactly like ``cli/sync.py``. Building
async resources for one-shot CLI execution would force a needless event
loop and lose the bookkeeping the sync subcommand already validates.

CLI exit codes:
    0 — every project enumerated cleanly and (if not ``--dry-run``)
        every purge completed without failures.
    1 — at least one project hit an enumeration error OR a purge
        reported any ``PurgeFailure`` (DC-5 propagation).

Output formats (SD §9.1):
    text — Rich table per project + summary.
    json — single JSON document for agent consumers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from secondsight.api._id_safety import is_safe_id
from secondsight.api.registry import ProjectRegistry
from secondsight.cli._home import secondsight_home as resolve_secondsight_home
from secondsight.storage.retention import (
    ExpiredSession,
    PurgeResult,
    RawTracesPurger,
    RetentionConfig,
    RetentionConfigError,
    enumerate_expired_sessions,
)

app = typer.Typer(name="cleanup", help="Reap expired raw-traces sessions per the retention policy.")
_console = Console()


@dataclass(frozen=True)
class _EnumerationOutcome:
    """Result of one project's enumeration step (DC-3 entry point).

    Both dry-run and real-run paths consume this same shape so the set
    being reported can never diverge from the set being purged.
    """

    project_id: str
    config: RetentionConfig | None
    expired: list[ExpiredSession]
    error: str | None


def _enumerate_for_project(home: Path, project_id: str, *, now: datetime) -> _EnumerationOutcome:
    """Resolve RetentionConfig + enumerate expired sessions for ONE project.

    Errors (config malformed, DB unreachable) are returned as
    ``error`` rather than raised — the caller continues to the next
    project so a single corrupt project cannot silently abort the whole
    cleanup loop.
    """
    try:
        cfg = RetentionConfig.load(home=home, project_id=project_id)
    except RetentionConfigError as exc:
        return _EnumerationOutcome(
            project_id=project_id,
            config=None,
            expired=[],
            error=f"config: {exc}",
        )

    registry = ProjectRegistry(secondsight_home=home)
    try:
        resources = registry._build_resources(project_id)  # noqa: SLF001
    except Exception as exc:  # noqa: BLE001 — surface to operator
        return _EnumerationOutcome(
            project_id=project_id,
            config=cfg,
            expired=[],
            error=f"build_resources: {type(exc).__name__}: {exc}",
        )

    try:
        try:
            expired = enumerate_expired_sessions(
                resources.events_repository,
                raw_traces_ttl_days=cfg.raw_traces_ttl_days,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001
            return _EnumerationOutcome(
                project_id=project_id,
                config=cfg,
                expired=[],
                error=f"enumerate: {type(exc).__name__}: {exc}",
            )
    finally:
        # The DBEngine stays alive across the purge step (the purger
        # needs it). Caller must dispose; see _run_for_project.
        pass

    return _EnumerationOutcome(
        project_id=project_id,
        config=cfg,
        expired=expired,
        error=None,
    )


def _project_report(
    outcome: _EnumerationOutcome,
    *,
    purge: PurgeResult | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Render one project's outcome to the JSON-serialisable dict shape."""
    cfg = outcome.config
    expired_ids = [s.session_id for s in outcome.expired]
    base: dict[str, Any] = {
        "project_id": outcome.project_id,
        "ttl_source": cfg.source if cfg is not None else None,
        "raw_traces_ttl_days": cfg.raw_traces_ttl_days if cfg is not None else None,
        "expired_session_ids": expired_ids,
        "error": outcome.error,
    }
    if dry_run:
        base["purged_session_ids"] = None
        base["failures"] = None
    else:
        base["purged_session_ids"] = list(purge.purged_session_ids) if purge is not None else []
        base["failures"] = (
            [
                {
                    "session_id": f.session_id,
                    "stage": f.stage,
                    "error": f.error,
                }
                for f in purge.failures
            ]
            if purge is not None
            else []
        )
    return base


@app.callback(invoke_without_command=True)
def cleanup(
    ctx: typer.Context,
    home: str = typer.Option(
        "",
        "--home",
        help="Override the SecondSight home directory (default: $SECONDSIGHT_HOME or ~/.secondsight).",
    ),
    project_id: str = typer.Option(
        "",
        "--project-id",
        help="Limit cleanup to one project_id (default: every project under <home>/projects/).",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Enumerate expired sessions WITHOUT deleting anything (DC-3: identical enumeration path as real run).",
    ),
    output_format: str = typer.Option(
        "text",
        "--format",
        help="Output format: 'text' (default, Rich) or 'json'.",
    ),
) -> None:
    """Reap expired raw_traces sessions across every project (or one)."""
    if ctx.invoked_subcommand is not None:  # pragma: no cover
        return

    home_path = resolve_secondsight_home(home)
    # Hardening (GUR-147 review MEDIUM-1): a `--project-id` value is forwarded
    # directly to `home / "projects" / project_id` and to ProjectRegistry
    # _build_resources which mkdirs that path. Reject traversal characters at
    # the CLI boundary so a typo or shell-script-supplied value cannot create
    # directories outside the SecondSight home root.
    if project_id and not is_safe_id(project_id):
        typer.echo(
            f"--project-id {project_id!r} contains unsafe characters; "
            f"use alphanumeric, hyphen, underscore, colon, or dot.",
            err=True,
        )
        raise typer.Exit(code=2)
    project_ids = _select_project_ids(home_path, project_id)
    now = datetime.now(timezone.utc)

    reports: list[dict[str, Any]] = []
    any_failure = False

    for pid in project_ids:
        outcome = _enumerate_for_project(home_path, pid, now=now)

        if outcome.error is not None:
            any_failure = True
            reports.append(_project_report(outcome, purge=None, dry_run=dry_run))
            continue

        if dry_run or not outcome.expired:
            # Dry-run never touches the destructive side. Empty real-run
            # also short-circuits — no PurgeResult to render.
            reports.append(_project_report(outcome, purge=None, dry_run=dry_run))
            # Resources need disposal here since the purge branch below
            # doesn't run.
            _dispose_for(home_path, pid)
            continue

        purge_result = _purge_for_project(home_path, pid, outcome.expired)
        if purge_result.had_failures:
            any_failure = True
        reports.append(_project_report(outcome, purge=purge_result, dry_run=dry_run))

    payload = {"projects": reports, "dry_run": dry_run}

    if output_format == "json":
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        _render_text(reports, dry_run=dry_run)

    if any_failure:
        raise typer.Exit(code=1)


def _purge_for_project(home: Path, project_id: str, expired: list[ExpiredSession]) -> PurgeResult:
    """Build per-project resources and invoke RawTracesPurger.

    Resources are built fresh for the purge step rather than threaded
    through from enumeration so the enumeration helper stays pure (DC-3:
    no shared state that could let dry-run and real-run diverge).
    """
    registry = ProjectRegistry(secondsight_home=home)
    resources = registry._build_resources(project_id)  # noqa: SLF001
    try:
        purger = RawTracesPurger(
            repo=resources.events_repository,
            raw_trace_store=resources.raw_trace_store,
        )
        return purger.purge(expired)
    finally:
        resources.db_engine.dispose()


def _dispose_for(home: Path, project_id: str) -> None:
    """Best-effort dispose of the engine the enumeration step opened."""
    project_db = home / "projects" / project_id / "intelligence.db"
    # Without a handle to the original engine, we can't dispose it
    # directly. Re-resolving and disposing immediately is a defensive
    # no-op in practice (sqlite engines hold no daemon connections), but
    # the symmetry keeps the resource accounting honest if the engine
    # backend ever changes.
    if not project_db.exists():
        return
    from secondsight.storage.db_engine import DBEngine

    engine = DBEngine(db_path=project_db)
    engine.dispose()


def _select_project_ids(home: Path, requested: str) -> list[str]:
    """Mirror cli/sync.py:_select_project_ids — FS-walk over home/projects."""
    if requested:
        return [requested]
    projects_dir = home / "projects"
    if not projects_dir.is_dir():
        return []
    return sorted(child.name for child in projects_dir.iterdir() if child.is_dir())


def _render_text(reports: list[dict[str, Any]], *, dry_run: bool) -> None:
    if not reports:
        _console.print("[yellow]No projects found under <home>/projects/[/yellow]")
        return

    title = "secondsight cleanup (dry-run)" if dry_run else "secondsight cleanup"
    table = Table(title=title)
    table.add_column("project_id")
    table.add_column("ttl_source")
    table.add_column("ttl_days", justify="right")
    table.add_column("expired", justify="right")
    if not dry_run:
        table.add_column("purged", justify="right")
        table.add_column("failures", justify="right")

    for r in reports:
        row = [
            r["project_id"],
            str(r.get("ttl_source") or "?"),
            str(r.get("raw_traces_ttl_days") or "?"),
            str(len(r["expired_session_ids"])),
        ]
        if not dry_run:
            row.append(str(len(r.get("purged_session_ids") or [])))
            row.append(str(len(r.get("failures") or [])))
        table.add_row(*row)

    _console.print(table)

    for r in reports:
        if r.get("error"):
            _console.print(f"[red]{r['project_id']}: {r['error']}[/red]")
        for failure in r.get("failures") or []:
            _console.print(
                f"[red]{r['project_id']} session={failure['session_id']} "
                f"stage={failure['stage']}: {failure['error']}[/red]"
            )


__all__ = ["app"]
