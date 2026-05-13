"""`secondsight cleanup` — manual retention reaper.

Originally task-A6 of GUR-147 (raw_traces only); extended in task-B5 of
GUR-149 to also reap analysis_results (`session_reports` + `behavior_flags`)
per the resolved `analysis_ttl_days`.

For each project under ``<secondsight-home>/projects/``:
    1. Resolve ``RetentionConfig`` (per-project TOML > global TOML > built-in).
       BOTH ``raw_traces_ttl_days`` and ``analysis_ttl_days`` resolve
       independently with their own source attributions (DC-B1 detection
       contract: an operator typo on either field falls through to
       ``builtin_default`` and is visible only via the source attribution).
    2. Enumerate raw_traces sessions whose ``last_event_at`` ≤ ``now - raw_ttl``.
       Enumerate expired analyses (session_reports.created_at ≤ ``now - analysis_ttl``).
    3. If ``--dry-run``, report both enumerations and stop.
    4. Otherwise run BOTH purgers per project (raw_traces first, then
       analysis_results) and report results.

DC-3 (no enumeration drift): both ``--dry-run`` and the real run call the
same ``_enumerate_for_project`` helper. A regression that re-implemented
the dry-run path independently would let preview and reap diverge —
exactly the failure the operator-trust contract forbids. Holds for both
raw_traces and analysis enumerations.

D8 / verification C2 (no async ``ProjectRegistry`` here): the CLI walks
``home/projects/`` synchronously, exactly like ``cli/sync.py``. Building
async resources for one-shot CLI execution would force a needless event
loop and lose the bookkeeping the sync subcommand already validates.

D7 (auto-include both purgers): no ``--analysis-only`` / ``--raw-only``
flag. Each TTL is independently configurable; running both purgers per
project is the consistent default. If an operator wants to disable one
side, they set the corresponding TTL to a value high enough that no rows
ever expire.

CLI exit codes:
    0 — every project enumerated cleanly and (if not ``--dry-run``)
        every purge (raw_traces + analysis) completed without failures.
    1 — at least one project hit an enumeration error OR any purger
        reported any ``PurgeFailure`` (DC-5 propagation).

Output formats (SD §9.1):
    text — Rich table per project + summary.
    json — single JSON document for agent consumers.

JSON shape (task-B5 changes):
    Renamed ``ttl_source`` → ``raw_traces_ttl_source`` for symmetry
    with the new ``analysis_ttl_source``. Added ``analysis_ttl_days``,
    ``analysis_ttl_source``, ``expired_analysis_session_ids``,
    ``analysis_purged_session_ids``, ``analysis_failures``. The
    rename closes scar-B1-3 (deferred from task-B1).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from secondsight.api._id_safety import is_safe_id
from secondsight.api.registry import ProjectRegistry
from secondsight.cli._home import secondsight_home as resolve_secondsight_home
from secondsight.storage.analysis_retention import (
    AnalysisResultsPurger,
    ExpiredAnalysis,
    enumerate_expired_analyses,
)
from secondsight.storage.behavior_flags_repository import BehaviorFlagsRepository
from secondsight.storage.retention import (
    ExpiredSession,
    PurgeResult,
    RawTracesPurger,
    RetentionConfig,
    RetentionConfigError,
    enumerate_expired_sessions,
)
from secondsight.storage.session_reports_repository import SessionReportsRepository

app = typer.Typer(
    name="cleanup",
    help=(
        "Reap expired sessions per the retention policy. "
        "Reaps raw_traces (per raw_traces_ttl_days) AND analysis_results "
        "(session_reports + behavior_flags per analysis_ttl_days) for "
        "every project."
    ),
)
_console = Console()
_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _EnumerationOutcome:
    """Result of one project's enumeration step (DC-3 entry point).

    Both dry-run and real-run paths consume this same shape so the set
    being reported can never diverge from the set being purged. Carries
    BOTH raw_traces (``expired``) and analysis (``expired_analyses``)
    enumerations so the dual-purger flow shares one DC-3 contract.
    """

    project_id: str
    config: RetentionConfig | None
    expired: list[ExpiredSession]
    expired_analyses: list[ExpiredAnalysis]
    error: str | None


def _enumerate_for_project(home: Path, project_id: str, *, now: datetime) -> _EnumerationOutcome:
    """Resolve RetentionConfig + enumerate expired raw_traces AND analyses
    for ONE project.

    Errors (config malformed, DB unreachable) are returned as
    ``error`` rather than raised — the caller continues to the next
    project so a single corrupt project cannot silently abort the whole
    cleanup loop.

    The same SessionReportsRepository is built fresh from
    ProjectRegistry's resources; if the project has never been analyzed,
    the schema is created on-the-fly (idempotent ``create_schema``).
    """
    try:
        cfg = RetentionConfig.load(home=home, project_id=project_id)
    except RetentionConfigError as exc:
        return _EnumerationOutcome(
            project_id=project_id,
            config=None,
            expired=[],
            expired_analyses=[],
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
            expired_analyses=[],
            error=f"build_resources: {type(exc).__name__}: {exc}",
        )

    # Raw_traces + analysis enumeration share the same engine — wrap
    # both inside try/finally so the enumeration engine is ALWAYS
    # disposed before this helper returns (yin review B5 Critical fix:
    # was leaking when only one side had expired items, because the
    # post-loop _dispose_for fired only on the empty case).
    expired: list[ExpiredSession] = []
    expired_analyses: list[ExpiredAnalysis] = []
    error: str | None = None
    try:
        # Raw_traces side.
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
                expired_analyses=[],
                error=f"enumerate raw_traces: {type(exc).__name__}: {exc}",
            )

        # Analysis side. ProjectRegistry only wires up the events table
        # by default; we create the analysis schemas on the fly so a
        # fresh install enumerates cleanly (DC-B7 empty-install path).
        # Yin review B5 Critical fix: create_schema() is now INSIDE the
        # try block so a DDL failure produces a structured error rather
        # than crashing the entire CLI loop.
        try:
            reports_repo = SessionReportsRepository(resources.db_engine)
            reports_repo.create_schema()
            expired_analyses = enumerate_expired_analyses(
                reports_repo,
                analysis_ttl_days=cfg.analysis_ttl_days,
                now=now,
            )
        except Exception as exc:  # noqa: BLE001
            # Quality review B5 D fix: keep the raw_traces side's
            # enumeration available so the caller can still reap them;
            # surface the analysis-side failure via `error`.
            error = f"enumerate analyses: {type(exc).__name__}: {exc}"
    finally:
        # Always dispose. The two purge helpers below will build their
        # own fresh resources (DC-3: no shared state).
        resources.db_engine.dispose()

    return _EnumerationOutcome(
        project_id=project_id,
        config=cfg,
        expired=expired,
        expired_analyses=expired_analyses,
        error=error,
    )


def _failures_to_dicts(purge: PurgeResult | None) -> list[dict[str, Any]]:
    if purge is None:
        return []
    return [
        {
            "session_id": f.session_id,
            "stage": f.stage,
            "error": f.error,
        }
        for f in purge.failures
    ]


def _project_report(
    outcome: _EnumerationOutcome,
    *,
    purge: PurgeResult | None,
    analysis_purge: PurgeResult | None,
    dry_run: bool,
) -> dict[str, Any]:
    """Render one project's outcome to the JSON-serialisable dict shape.

    JSON shape (post-task-B5):
        - ``raw_traces_ttl_days`` / ``raw_traces_ttl_source`` (renamed
          from ``ttl_source`` per scar-B1-3 deferral, resolved here).
        - ``analysis_ttl_days`` / ``analysis_ttl_source`` (NEW, task-B5).
        - ``expired_session_ids`` (raw_traces) /
          ``expired_analysis_session_ids``.
        - ``purged_session_ids`` / ``analysis_purged_session_ids``.
        - ``failures`` / ``analysis_failures``.
    """
    cfg = outcome.config
    expired_ids = [s.session_id for s in outcome.expired]
    expired_analysis_ids = [a.session_id for a in outcome.expired_analyses]
    base: dict[str, Any] = {
        "project_id": outcome.project_id,
        "raw_traces_ttl_source": cfg.raw_traces_source if cfg is not None else None,
        "raw_traces_ttl_days": cfg.raw_traces_ttl_days if cfg is not None else None,
        "analysis_ttl_source": cfg.analysis_ttl_source if cfg is not None else None,
        "analysis_ttl_days": cfg.analysis_ttl_days if cfg is not None else None,
        "expired_session_ids": expired_ids,
        "expired_analysis_session_ids": expired_analysis_ids,
        "error": outcome.error,
    }
    if dry_run:
        base["purged_session_ids"] = None
        base["failures"] = None
        base["analysis_purged_session_ids"] = None
        base["analysis_failures"] = None
    else:
        base["purged_session_ids"] = list(purge.purged_session_ids) if purge is not None else []
        base["failures"] = _failures_to_dicts(purge)
        base["analysis_purged_session_ids"] = (
            list(analysis_purge.purged_session_ids) if analysis_purge is not None else []
        )
        base["analysis_failures"] = _failures_to_dicts(analysis_purge)
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
    """Reap expired sessions (raw_traces + analysis_results) across every project (or one).

    Each project resolves both ``raw_traces_ttl_days`` and ``analysis_ttl_days``
    independently from per-project / global / builtin TOML config layers,
    enumerates expired sessions per side, and (unless ``--dry-run``) runs both
    purgers per project. The resolved values + sources are emitted as a
    structured INFO log line per project (DC-B1 detection contract).
    """
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

        # B-S1 (DC-B1) log half: emit a structured INFO line per
        # project naming both resolved values + sources. This is the
        # detection contract for typo-driven silent fall-through to
        # builtin defaults — operators reading cleanup logs see
        # `analysis_ttl_source=builtin_default` when they expected
        # `per_project_config`. Suppressed for projects with config
        # errors (cfg is None on RetentionConfigError).
        if outcome.config is not None:
            cfg = outcome.config
            _logger.info(
                f"retention resolved: project_id={pid} "
                f"raw_traces_ttl_days={cfg.raw_traces_ttl_days} "
                f"raw_traces_source={cfg.raw_traces_source} "
                f"analysis_ttl_days={cfg.analysis_ttl_days} "
                f"analysis_ttl_source={cfg.analysis_ttl_source}"
            )

        # Yin review B5 Quality D fix: if outcome.error is set, the
        # analysis enumeration failed but raw_traces enumeration may
        # still have succeeded. We still attempt the raw_traces purge
        # so an enumeration partial-failure does not silently strand
        # already-detected expired raw_traces sessions.
        if outcome.error is not None:
            any_failure = True
            # Drop into the same purge branch below; outcome.expired is
            # populated if raw_traces enumeration succeeded before the
            # analysis-side failure.

        if dry_run:
            # Dry-run never touches either destructive side (D7: both purgers
            # are governed by the same dry-run gate).
            reports.append(
                _project_report(outcome, purge=None, analysis_purge=None, dry_run=dry_run)
            )
            continue

        # Real run. Run raw_traces purger first, then analysis purger
        # (task-B5 scar-B5-1: ordering pinned for crash-recovery — if
        # raw_traces fails, the analysis purger still attempts its own
        # set; if analysis fails after raw_traces succeeds, raw_traces
        # are already gone but the analysis enumeration is re-attemptable
        # on the next CLI run).
        #
        # Yin review B5 Critical fix: each purger call is wrapped in
        # try/except so a raw exception (connection lost, _build_resources
        # FileNotFoundError, etc.) is captured as a failure and the
        # second purger still attempts its own set. Without this wrap,
        # a raw exception from the first purger would silently skip the
        # second one and leave analysis_purge_result=None indistinguishable
        # from "no expired analyses".
        purge_result: PurgeResult | None = None
        analysis_purge_result: PurgeResult | None = None
        purge_errors: list[str] = []

        if outcome.expired:
            try:
                purge_result = _purge_for_project(home_path, pid, outcome.expired)
                if purge_result.had_failures:
                    any_failure = True
            except Exception as exc:  # noqa: BLE001
                any_failure = True
                purge_errors.append(f"raw_traces purge: {type(exc).__name__}: {exc}")

        if outcome.expired_analyses:
            try:
                analysis_purge_result = _analysis_purge_for_project(
                    home_path, pid, outcome.expired_analyses
                )
                if analysis_purge_result.had_failures:
                    any_failure = True
            except Exception as exc:  # noqa: BLE001
                any_failure = True
                purge_errors.append(f"analysis purge: {type(exc).__name__}: {exc}")

        # If a purge raised, surface the error in the report alongside
        # any pre-existing enumeration error.
        if purge_errors:
            combined_error = "; ".join(([outcome.error] if outcome.error else []) + purge_errors)
            outcome = replace(outcome, error=combined_error)

        reports.append(
            _project_report(
                outcome,
                purge=purge_result,
                analysis_purge=analysis_purge_result,
                dry_run=dry_run,
            )
        )

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


def _analysis_purge_for_project(
    home: Path, project_id: str, expired: list[ExpiredAnalysis]
) -> PurgeResult:
    """Build per-project resources and invoke AnalysisResultsPurger.

    Mirrors _purge_for_project: fresh resources, dispose in finally.
    SessionReportsRepository.create_schema() is idempotent — needed
    because ProjectRegistry only wires up events by default.
    """
    registry = ProjectRegistry(secondsight_home=home)
    resources = registry._build_resources(project_id)  # noqa: SLF001
    try:
        reports_repo = SessionReportsRepository(resources.db_engine)
        reports_repo.create_schema()
        flags_repo = BehaviorFlagsRepository(resources.db_engine)
        flags_repo.create_schema()
        purger = AnalysisResultsPurger(
            session_reports_repo=reports_repo,
            behavior_flags_repo=flags_repo,
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
    table.add_column("raw_src")
    table.add_column("raw_days", justify="right")
    table.add_column("raw_expired", justify="right")
    table.add_column("ana_src")
    table.add_column("ana_days", justify="right")
    table.add_column("ana_expired", justify="right")
    if not dry_run:
        table.add_column("raw_purged", justify="right")
        table.add_column("raw_fail", justify="right")
        table.add_column("ana_purged", justify="right")
        table.add_column("ana_fail", justify="right")

    for r in reports:
        row = [
            r["project_id"],
            str(r.get("raw_traces_ttl_source") or "?"),
            str(r.get("raw_traces_ttl_days") or "?"),
            str(len(r.get("expired_session_ids") or [])),
            str(r.get("analysis_ttl_source") or "?"),
            str(r.get("analysis_ttl_days") or "?"),
            str(len(r.get("expired_analysis_session_ids") or [])),
        ]
        if not dry_run:
            row.append(str(len(r.get("purged_session_ids") or [])))
            row.append(str(len(r.get("failures") or [])))
            row.append(str(len(r.get("analysis_purged_session_ids") or [])))
            row.append(str(len(r.get("analysis_failures") or [])))
        table.add_row(*row)

    _console.print(table)

    for r in reports:
        if r.get("error"):
            _console.print(f"[red]{r['project_id']}: {r['error']}[/red]")
        for failure in r.get("failures") or []:
            _console.print(
                f"[red]{r['project_id']} raw_traces session={failure['session_id']} "
                f"stage={failure['stage']}: {failure['error']}[/red]"
            )
        for failure in r.get("analysis_failures") or []:
            _console.print(
                f"[red]{r['project_id']} analysis session={failure['session_id']} "
                f"stage={failure['stage']}: {failure['error']}[/red]"
            )


__all__ = ["app"]
