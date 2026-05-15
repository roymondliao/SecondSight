"""`secondsight analyze` — manual trigger for session analysis (GUR-103 P2-15).

Runs the analysis pipeline for a specific session, either by forwarding the
request to a running `secondsight serve` API server (preferred) or by
running the orchestrator in-process (fallback).

Decision D9: Manual CLI prefers server-mode (HTTPX → API server), falls back
to in-process orchestrator if server is down. Path taken is logged at INFO.

Decision D14: Trigger.dispatch() does NOT pre-insert analysis_runs rows.
Only the orchestrator's start_run() does (preserves DC-1 audit contract).

Exit codes:
  0 — analysis dispatched successfully (or is in progress).
  1 — orchestrator raised an exception (pipeline failure).
  2 — session already analyzed and --force not passed.

Usage:
  secondsight analyze --session SESSION_ID [--project PROJECT_ID] [--force]
  secondsight analyze --session SESSION_ID --no-server  # force in-process

Server-mode default:
  If `--no-server` is not passed, the CLI first tries to POST to the API
  server at the configured address. On ConnectError (server not running),
  it falls back to in-process and logs at INFO.

  NOTE: POST /api/analyze is a future endpoint. In v1, the server-mode path
  is attempted but the server does not implement this endpoint yet. The
  ConnectError fallback handles the "server running but no /api/analyze"
  case via httpx.HTTPStatusError (non-2xx → treated as server-mode failure,
  falls back to in-process). This is documented in the scar report.

In-process path:
  Constructs all dependencies (repos, tools, router, agent, orchestrator),
  creates a Trigger, calls dispatch(), awaits the dispatched task, then
  streams stage transitions to STDOUT.

  Assumption: all repositories live under the default SecondSight home
  (~/.secondsight/projects/<project_id>/). If the project directory does
  not exist, the repos will fail on schema access; the error is caught and
  reported as exit code 1.
"""

from __future__ import annotations

import asyncio
from loguru import logger
from pathlib import Path
from typing import Optional

import typer

import httpx

from secondsight.cli._home import secondsight_home as resolve_secondsight_home
from secondsight.sdk.trigger import DispatchResult, Trigger

app = typer.Typer(
    name="analyze",
    help=(
        "Trigger analysis for a session. Prefers API server mode; "
        "falls back to in-process if the server is unreachable."
    ),
)

# Default server URL for server-mode dispatch.
_DEFAULT_SERVER_URL = "http://127.0.0.1:8420"


@app.callback(invoke_without_command=True)
def analyze(
    ctx: typer.Context,
    session: Optional[str] = typer.Option(
        None,
        "--session",
        "-s",
        help="Session ID to analyze.",
    ),
    project: Optional[str] = typer.Option(
        None,
        "--project",
        "-p",
        help="Project ID. Defaults to the project registered in the project config.",
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Re-run analysis even if the session was already analyzed.",
    ),
    retry_failed: bool = typer.Option(
        False,
        "--retry-failed",
        help="Re-analyze all sessions with failed analysis runs (GUR-108, P3B-6).",
    ),
    no_server: bool = typer.Option(
        False,
        "--no-server",
        help="Skip server-mode and run in-process directly.",
    ),
    home: str = typer.Option(
        "",
        "--home",
        help="Override SecondSight home directory.",
        envvar="SECONDSIGHT_HOME",
    ),
    server_url: str = typer.Option(
        _DEFAULT_SERVER_URL,
        "--server-url",
        help="Base URL of the API server for server-mode dispatch.",
        envvar="SECONDSIGHT_SERVER_URL",
    ),
) -> None:
    """Trigger analysis for a session.

    Prefers server-mode (POST to API server); falls back to in-process
    if the server is unreachable. The path taken is logged at INFO.

    Exit codes:
      0 — analysis dispatched or already in progress.
      2 — session already analyzed (pass --force to re-run).
      1 — analysis pipeline failed.
    """
    secondsight_home_path = resolve_secondsight_home(home)
    project_id = project or _resolve_project_id(secondsight_home_path)

    from secondsight.api._id_safety import is_safe_id

    if project and not is_safe_id(project):
        raise typer.BadParameter(
            f"project {project!r} contains unsafe path characters.",
            param_hint="--project",
        )

    if retry_failed:
        _handle_retry_failed(
            project_id=project_id,
            secondsight_home=secondsight_home_path,
        )
        return

    if session is None:
        raise typer.BadParameter(
            "Provide --session SESSION_ID or --retry-failed.",
            param_hint="--session",
        )

    if not no_server:
        # Try server-mode first.
        try:
            _dispatch_via_server(
                server_url=server_url,
                session_id=session,
                project_id=project_id,
                force=force,
            )
            typer.echo(f"Analysis dispatched for session {session!r} via server.")
            raise typer.Exit(code=0)
        except httpx.ConnectError:
            # Server is not running — in-process fallback is the correct path.
            logger.info(
                f"analyze: server at {server_url} not reachable; "
                f"falling back to in-process dispatch"
            )
            typer.echo(
                f"Server at {server_url} not reachable — running in-process.",
                err=True,
            )
        except httpx.HTTPStatusError as exc:
            # Server IS running but returned an error (e.g., 500, 404).
            # Do NOT silently fall back to in-process: the server endpoint exists
            # but is broken. Silent fallback hides real server-side failures from
            # the operator. Log at ERROR and exit with code 1.
            logger.error(
                f"analyze: server at {server_url} returned "
                f"HTTP {exc.response.status_code} for /api/analyze — "
                f"NOT falling back to in-process (server is up but endpoint failed). "
                f"Error: {exc}"
            )
            typer.echo(
                f"Server at {server_url} returned HTTP {exc.response.status_code} "
                f"— analysis aborted (server is up but /api/analyze failed). "
                f"Use --no-server to bypass server mode.",
                err=True,
            )
            raise typer.Exit(code=1)

    # In-process path.
    try:
        trigger = _build_in_process_trigger(
            secondsight_home=secondsight_home_path,
            project_id=project_id,
        )
        result = _run_in_process_dispatch(
            trigger=trigger,
            project_id=project_id,
            session_id=session,
            force=force,
        )
    except Exception as exc:
        logger.error(
            f"analyze: in-process dispatch failed for session_id={session!r}: "
            f"{type(exc).__name__}: {exc}"
        )
        typer.echo(
            f"Analysis failed: {type(exc).__name__}: {exc}",
            err=True,
        )
        raise typer.Exit(code=1)

    _handle_dispatch_result(result, session_id=session)


def _handle_retry_failed(
    *,
    project_id: str,
    secondsight_home: Path,
) -> None:
    """Re-analyze all sessions with failed analysis runs (GUR-108, P3B-6).

    Queries the DB for failed runs, extracts unique session IDs, and
    re-triggers analysis with force=True for each.
    """
    from secondsight.storage.analysis_runs_repository import AnalysisRunsRepository
    from secondsight.storage.db_engine import DBEngine

    project_dir = secondsight_home / "projects" / project_id
    db_path = project_dir / "intelligence.db"

    if not db_path.exists():
        typer.echo(f"No database found at {db_path}. Nothing to retry.", err=True)
        raise typer.Exit(code=0)

    db_engine = DBEngine(db_path)
    try:
        runs_repo = AnalysisRunsRepository(db_engine)
        runs_repo.create_schema()
        failed_runs = runs_repo.get_failed_runs(project_id)
    finally:
        db_engine.dispose()

    if not failed_runs:
        typer.echo("No failed analysis runs found. Nothing to retry.")
        raise typer.Exit(code=0)

    # Deduplicate by session_id (multiple failed runs for the same session
    # should only trigger one retry).
    seen_sessions: set[str] = set()
    unique_sessions: list[str] = []
    for run in failed_runs:
        if run.session_id not in seen_sessions:
            seen_sessions.add(run.session_id)
            unique_sessions.append(run.session_id)

    typer.echo(
        f"Found {len(failed_runs)} failed run(s) across "
        f"{len(unique_sessions)} session(s). Retrying..."
    )

    succeeded = 0
    failed = 0
    for sid in unique_sessions:
        try:
            trigger = _build_in_process_trigger(
                secondsight_home=secondsight_home,
                project_id=project_id,
            )
            result = _run_in_process_dispatch(
                trigger=trigger,
                project_id=project_id,
                session_id=sid,
                force=True,
            )
            if result.dispatched and result.reason != "timed-out":
                succeeded += 1
                typer.echo(f"  [ok] session {sid!r} re-analyzed.")
            else:
                failed += 1
                typer.echo(f"  [fail] session {sid!r}: {result.reason}", err=True)
        except Exception as exc:
            failed += 1
            typer.echo(
                f"  [fail] session {sid!r}: {type(exc).__name__}: {exc}",
                err=True,
            )

    typer.echo(f"Retry complete: {succeeded} succeeded, {failed} failed.")
    raise typer.Exit(code=1 if failed > 0 else 0)


def _dispatch_via_server(
    *,
    server_url: str,
    session_id: str,
    project_id: str,
    force: bool,
) -> None:
    """POST to the API server's /api/analyze endpoint.

    NOTE: /api/analyze is not implemented in v1. This call will get a 404
    or ConnectError when the server is down. Both are handled by the caller.
    The server path is included for v2 readiness (D9).

    Raises:
        httpx.ConnectError: server not running.
        httpx.HTTPStatusError: server returned a non-2xx status.
    """
    url = f"{server_url.rstrip('/')}/api/analyze"
    response = httpx.post(
        url,
        json={
            "session_id": session_id,
            "project_id": project_id,
            "force": force,
        },
        timeout=10.0,
    )
    response.raise_for_status()


def _resolve_project_id(secondsight_home: Path) -> str:
    """Attempt to discover the project_id from the config.

    Looks for a single project directory under secondsight_home/projects/.
    If there is exactly one project, uses it. Otherwise raises UsageError.

    This is a best-effort fallback for users who don't pass --project.
    """
    projects_dir = secondsight_home / "projects"
    if not projects_dir.exists():
        raise typer.BadParameter(
            f"No projects found under {projects_dir}. Pass --project PROJECT_ID explicitly.",
            param_hint="--project",
        )
    project_dirs = [d for d in projects_dir.iterdir() if d.is_dir()]
    if len(project_dirs) == 1:
        return project_dirs[0].name
    if len(project_dirs) == 0:
        raise typer.BadParameter(
            f"No project directories found under {projects_dir}.",
            param_hint="--project",
        )
    raise typer.BadParameter(
        f"Multiple projects found: {[d.name for d in project_dirs]}. "
        "Pass --project PROJECT_ID to specify which one.",
        param_hint="--project",
    )


def _build_in_process_trigger(
    *,
    secondsight_home: Path,
    project_id: str,
) -> "Trigger":
    """Construct Trigger and all dependencies for in-process dispatch.

    Constructs:
      - DBEngine (for the project's sqlite DB)
      - EventsRepository, AnalysisRunsRepository
      - Orchestrator (loaded lazily — we only need it if dispatch proceeds)

    The Orchestrator construction (with LLMRouter + PydanticAIAnalysisAgent)
    is deferred until dispatch() is called to avoid paying the model
    initialization cost when the session is already analyzed.

    Assumption: project DB lives at
    secondsight_home/projects/<project_id>/intelligence.db.
    If that file does not exist yet, repo.create_schema() will create it.
    This means a fresh project with no events will still get a DB; the
    orchestrator's _verify_session_complete_and_get_project_id() will raise
    SessionIncompleteError, which propagates as exit code 1.
    """
    from secondsight.analysis.runtime import build_project_analysis_runtime
    from secondsight.storage.db_engine import DBEngine
    from secondsight.storage.events_repository import EventsRepository
    from secondsight.storage.raw_trace_store import RawTraceStore

    project_dir = secondsight_home / "projects" / project_id
    db_path = project_dir / "intelligence.db"

    db_engine = DBEngine(db_path)
    events_repo = EventsRepository(db_engine)
    events_repo.create_schema()
    runtime = build_project_analysis_runtime(
        secondsight_home=secondsight_home,
        project_id=project_id,
        db_engine=db_engine,
        events_repository=events_repo,
        raw_trace_store=RawTraceStore(project_root=project_dir),
    )
    return runtime.trigger


def _run_in_process_dispatch(
    *,
    trigger: "Trigger",
    project_id: str,
    session_id: str,
    force: bool,
) -> DispatchResult:
    """Run dispatch() in a new event loop and await the background task.

    Streams stage transitions via polling on analysis_runs after dispatch.
    Returns the DispatchResult from trigger.dispatch().

    Note: the background analysis task is awaited here (not detached) so
    the CLI blocks until analysis completes. This is intentional for the
    manual CLI path — the operator wants to see the result.
    """

    async def _dispatch_and_wait() -> DispatchResult:
        result = await trigger.dispatch(
            project_id,
            session_id,
            source="manual",
            force=force,
        )
        if result.dispatched:
            # Wait for all pending tasks (the analysis task) to complete.
            # We gather all tasks except ourselves.
            pending = [
                t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()
            ]
            if pending:
                typer.echo(f"Waiting for analysis of session {session_id!r}...")
                done, still_pending = await asyncio.wait(
                    pending,
                    timeout=300.0,  # 5-minute timeout for in-process analysis
                )
                if still_pending:
                    logger.warning(
                        f"analyze: {len(still_pending)} task(s) did not complete "
                        f"within 300s — cancelling and reporting timeout"
                    )
                    for t in still_pending:
                        t.cancel()
                    # Report timeout honestly: do NOT report "Analysis complete"
                    # when the analysis was actually cancelled due to timeout.
                    return DispatchResult(
                        dispatched=True,
                        reason="timed-out",
                        run_id=result.run_id,
                    )
                # Check completed tasks for exceptions. The done callback
                # (_on_task_done) already logged the exception; we surface
                # it here so the CLI exits 1 instead of printing "Analysis
                # complete" when the analysis pipeline actually failed.
                failed = [t for t in done if not t.cancelled() and t.exception() is not None]
                if failed:
                    exc = failed[0].exception()
                    logger.error(
                        f"analyze: analysis task completed with exception "
                        f"session_id={session_id!r}: {type(exc).__name__}: {exc}"
                    )
                    return DispatchResult(
                        dispatched=True,
                        reason="task-failed",
                        run_id=result.run_id,
                    )
        return result

    return asyncio.run(_dispatch_and_wait())


def _handle_dispatch_result(result: DispatchResult, *, session_id: str) -> None:
    """Translate DispatchResult to CLI output and exit code.

    dispatched (reason="dispatched") → exit 0.
    dispatched (reason="analysis-failed") → exit 1 with failure warning.
    dispatched (reason="analysis-unknown") → exit 1 with unknown-outcome warning.
    dispatched (reason="timed-out") → exit 1 with timeout warning.
    already-analyzed → exit 2 with skip message + --force hint.
    other non-dispatched → exit 1.
    """
    if result.dispatched:
        if result.reason == "timed-out":
            # Analysis was dispatched but timed out before completion.
            # Do NOT report "Analysis complete" — the analysis was cancelled.
            typer.echo(
                f"Analysis for session {session_id!r} timed out after 300s. "
                "The analysis task was cancelled. Check logs for progress.",
                err=True,
            )
            raise typer.Exit(code=1)
        if result.reason == "task-failed":
            # Analysis task completed but raised an exception.
            # The exception was already logged by _on_task_done and the
            # error handler in _dispatch_and_wait. Surface a clean exit 1.
            typer.echo(
                f"Analysis failed for session {session_id!r}. "
                "See logs above for the exception detail.",
                err=True,
            )
            raise typer.Exit(code=1)
        if result.reason == "analysis-failed":
            typer.echo(
                f"Analysis failed for session {session_id!r}. "
                "See logs above for the provider or CLI error detail.",
                err=True,
            )
            raise typer.Exit(code=1)
        if result.reason == "analysis-unknown":
            typer.echo(
                f"Analysis outcome unknown for session {session_id!r}. "
                "Check logs above for timeout or provider diagnostics.",
                err=True,
            )
            raise typer.Exit(code=1)
        typer.echo(f"Analysis complete for session {session_id!r}.")
        raise typer.Exit(code=0)

    if result.reason == "already-analyzed":
        when = (
            result.existing_completed_at.isoformat() if result.existing_completed_at else "unknown"
        )
        msg = (
            f"Skipped: session {session_id!r} already analyzed at {when} "
            f"(stage={result.existing_stage!r}). "
            "pass --force to re-run"
        )
        typer.echo(msg, err=True)
        raise typer.Exit(code=2)

    # Other non-dispatch reasons (lock-held, another-run-in-flight).
    typer.echo(
        f"Dispatch skipped: {result.reason}. Try again in a moment.",
        err=True,
    )
    raise typer.Exit(code=1)


__all__ = [
    "app",
    "_build_in_process_trigger",
    "_run_in_process_dispatch",
]
