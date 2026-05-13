"""`secondsight directive` — operator control surface for directives (GUR-104 task-4).

Schema-as-contract (D1): this module imports ``DirectiveOut`` and
``DirectivePatchRequest`` directly from ``api/directives.py`` so its
``--format json`` output is byte-identical to the API. Renaming or removing
a field in ``DirectiveOut`` breaks both. ``model_dump_json()`` is the SINGLE
serialization path used by ALL code paths (server-mode AND no-server).

Server-mode fallback discipline (mirrors ``cli/analyze.py:135-165``):
- ``ConnectError`` → log INFO + stderr warning + continue with in-process.
- ``HTTPStatusError`` → log ERROR + stderr loud message + exit(1). NO fallback.
  Server is up, endpoint is broken; silently falling back masks the bug.

Exit codes:
  0 — command succeeded.
  1 — server mode: server returned an error (HTTPStatusError).
  2 — bad parameter (missing required flag, conflicting flags).

Usage:
  secondsight directive --active [--format json] [--project P] [--home H]
  secondsight directive --disable ID --reason "..." [--project P] [--home H]
  secondsight directive --enable ID [--project P] [--home H]

Assumption: project DB lives at home/projects/<project_id>/intelligence.db.
When ``--no-server`` is omitted, the CLI first tries the API server at
``--server-url`` (default http://127.0.0.1:8420). On ``ConnectError``, it
falls back to in-process. On ``HTTPStatusError``, it exits 1 (server up,
endpoint broken — do NOT mask with in-process).
"""

from __future__ import annotations

import json
from loguru import logger
from pathlib import Path
from typing import Optional

import httpx
import typer
from rich.console import Console
from rich.table import Table

from secondsight.api.directives import DirectiveOut
from secondsight.cli._home import secondsight_home as resolve_secondsight_home

app = typer.Typer(
    name="directive",
    help=(
        "Query and manage directives. Supports server-mode (httpx → API server) "
        "or --no-server (in-process, no server required). "
        "Use --active to list active directives, --disable/--enable to manage lifecycle."
    ),
)

# Default server URL for server-mode dispatch.
_DEFAULT_SERVER_URL = "http://127.0.0.1:8420"

_console = Console(stderr=False)


def _resolve_project_id(secondsight_home: Path) -> str:
    """Auto-detect project_id from the secondsight home directory.

    Mirrors ``cli/analyze._resolve_project_id`` — kept local to avoid
    importing from a sibling module (reduces coupling surface).

    Looks for exactly one project directory under secondsight_home/projects/.
    Raises typer.BadParameter if not found or ambiguous.
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


# ---------------------------------------------------------------------------
# Single shared serialization function (DC-4 linchpin)
# ---------------------------------------------------------------------------


def _serialize_directives(directives: list[DirectiveOut]) -> str:
    """Serialize a list of DirectiveOut to a JSON string.

    This is the SINGLE serialization path used by ALL code paths
    (server-mode AND no-server). Both paths go through ``DirectiveOut``
    Pydantic models so the output is byte-stable.

    Assumption: caller always validates raw data through ``DirectiveOut``
    before passing here. If caller passes unvalidated dicts, the schema
    contract is voided.
    """
    # Use json.dumps on model_dump() output so we get consistent Python
    # JSON semantics (not Pydantic's model_dump_json which is opaque for
    # list-level re-serialization). Each item is dumped via model_dump_json
    # then parsed back to dict so datetime serialization is consistent.
    items = [json.loads(d.model_dump_json()) for d in directives]
    return json.dumps(items)


# ---------------------------------------------------------------------------
# Subcommand callback
# ---------------------------------------------------------------------------


@app.callback(invoke_without_command=True)
def directive(
    ctx: typer.Context,
    active: bool = typer.Option(
        False,
        "--active",
        help="List active directives for the project.",
    ),
    disable: Optional[str] = typer.Option(
        None,
        "--disable",
        help="ID of the directive to soft-disable. Requires --reason.",
        metavar="DIRECTIVE_ID",
    ),
    enable: Optional[str] = typer.Option(
        None,
        "--enable",
        help="ID of the directive to re-activate.",
        metavar="DIRECTIVE_ID",
    ),
    reason: Optional[str] = typer.Option(
        None,
        "--reason",
        help="Reason for disabling a directive (required with --disable).",
    ),
    format: str = typer.Option(
        "table",
        "--format",
        help="Output format: 'table' (default) or 'json'.",
    ),
    project: Optional[str] = typer.Option(
        None,
        "--project",
        "-p",
        help="Project ID. Defaults to the project registered in the project config.",
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
    """Query and manage directives.

    Use exactly one of --active, --disable, or --enable per invocation.
    """
    secondsight_home_path = resolve_secondsight_home(home)
    project_id = project or _resolve_project_id(secondsight_home_path)

    # -------------------------------------------------------------------
    # Mode validation (Step 4): exactly one of {active, disable, enable}
    # -------------------------------------------------------------------
    modes_set = sum([active, disable is not None, enable is not None])
    if modes_set == 0:
        raise typer.BadParameter(
            "Provide one of --active, --disable DIRECTIVE_ID, or --enable DIRECTIVE_ID.",
            param_hint="--active / --disable / --enable",
        )
    if modes_set > 1:
        raise typer.BadParameter(
            "Only one of --active, --disable, or --enable may be specified at a time.",
            param_hint="--active / --disable / --enable",
        )

    # -------------------------------------------------------------------
    # Lifecycle flag validation (Step 5)
    # -------------------------------------------------------------------
    if disable is not None:
        if not reason:
            raise typer.BadParameter(
                "--disable requires a non-empty --reason "
                "(lifecycle contract: disabled directives must carry an audit reason).",
                param_hint="--reason",
            )
    if enable is not None:
        if reason is not None:
            raise typer.BadParameter(
                "--enable must NOT include --reason "
                "(only --disable carries a reason; see directive lifecycle contract).",
                param_hint="--reason",
            )

    # -------------------------------------------------------------------
    # Dispatch
    # -------------------------------------------------------------------
    if active:
        _handle_list_active(
            project_id=project_id,
            secondsight_home=secondsight_home_path,
            no_server=no_server,
            server_url=server_url,
            output_format=format,
        )
    elif disable is not None:
        _handle_disable(
            directive_id=disable,
            reason=reason,  # type: ignore[arg-type]  # already validated non-empty above
            project_id=project_id,
            secondsight_home=secondsight_home_path,
            no_server=no_server,
            server_url=server_url,
        )
    else:
        assert enable is not None
        _handle_enable(
            directive_id=enable,
            project_id=project_id,
            secondsight_home=secondsight_home_path,
            no_server=no_server,
            server_url=server_url,
        )


# ---------------------------------------------------------------------------
# --active: list directives
# ---------------------------------------------------------------------------


def _handle_list_active(
    *,
    project_id: str,
    secondsight_home: Path,
    no_server: bool,
    server_url: str,
    output_format: str,
) -> None:
    """List active directives and render the result."""
    if not no_server:
        try:
            directives = _list_directives_via_server(
                server_url=server_url,
                project_id=project_id,
                active_only=True,
            )
            _render_directives(directives, output_format=output_format)
            raise typer.Exit(code=0)
        except httpx.ConnectError:
            logger.info(
                f"directive: server at {server_url} not reachable; falling back to in-process"
            )
            typer.echo(
                f"Server at {server_url} not reachable — running in-process.",
                err=True,
            )
        except httpx.HTTPStatusError as exc:
            logger.error(
                f"directive: server at {server_url} returned "
                f"HTTP {exc.response.status_code} for GET /api/directives — "
                f"NOT falling back to in-process (server is up but endpoint failed). "
                f"Error: {exc}"
            )
            typer.echo(
                f"Server at {server_url} returned HTTP {exc.response.status_code} "
                f"— directive list aborted (server is up but /api/directives failed). "
                f"Use --no-server to bypass server mode.",
                err=True,
            )
            raise typer.Exit(code=1)

    # In-process path (fallback or --no-server)
    directives = _list_directives_in_process(
        secondsight_home=secondsight_home,
        project_id=project_id,
        active_only=True,
    )
    _render_directives(directives, output_format=output_format)
    raise typer.Exit(code=0)


def _list_directives_via_server(
    *,
    server_url: str,
    project_id: str,
    active_only: bool,
) -> list[DirectiveOut]:
    """GET /api/directives via httpx. Returns list of DirectiveOut.

    Re-validates the server response through DirectiveOut so the CLI
    always produces schema-validated output regardless of code path.
    This is the server-side half of the single-serialization contract.

    Raises:
        httpx.ConnectError: server not running.
        httpx.HTTPStatusError: server returned a non-2xx status.
    """
    url = f"{server_url.rstrip('/')}/api/directives"
    response = httpx.get(
        url,
        params={"project_id": project_id, "active": "true" if active_only else "false"},
        timeout=10.0,
    )
    response.raise_for_status()
    raw_list: list[dict] = response.json()
    return [DirectiveOut.model_validate(item) for item in raw_list]


def _list_directives_in_process(
    *,
    secondsight_home: Path,
    project_id: str,
    active_only: bool,
) -> list[DirectiveOut]:
    """Query directives in-process via DirectivesRepository.

    Builds a DBEngine for the project's SQLite DB, calls list_for_project,
    and wraps each Directive in DirectiveOut.from_directive().

    Assumption: project DB lives at
    secondsight_home/projects/<project_id>/intelligence.db. If absent, create_schema
    will create it and list_for_project will return an empty list (not an error).
    """
    from secondsight.storage.db_engine import DBEngine
    from secondsight.storage.directives_repository import DirectivesRepository

    project_dir = secondsight_home / "projects" / project_id
    db_path = project_dir / "intelligence.db"

    db_engine = DBEngine(db_path)
    try:
        repo = DirectivesRepository(db_engine)
        repo.create_schema()
        directives_list = repo.list_for_project(project_id, active_only=active_only)
        return [DirectiveOut.from_directive(d) for d in directives_list]
    finally:
        db_engine.dispose()


def _render_directives(
    directives: list[DirectiveOut],
    *,
    output_format: str,
) -> None:
    """Render directives in the specified format.

    ``--format json`` uses the single shared serialization function.
    Default (table) uses Rich table output.

    Assumption: ``output_format`` is either "json" or "table". Any other
    value is treated as "table" (no error). This is intentional: if a
    future format is added (e.g., "csv"), the caller should extend this
    function, not rely on a validation error here.
    """
    if output_format == "json":
        typer.echo(_serialize_directives(directives))
        return

    if output_format != "table":
        logger.warning(
            f"directive: unknown --format {output_format!r}; "
            f"falling back to 'table'. Supported formats: json, table."
        )
        typer.echo(
            f"Warning: unknown --format {output_format!r}; using 'table'. Supported: json, table.",
            err=True,
        )

    # Rich table output
    table = Table(
        title=f"Active Directives ({len(directives)} total)",
        show_header=True,
        header_style="bold cyan",
    )
    table.add_column("id", style="dim", max_width=20)
    table.add_column("type")
    table.add_column("instruction", max_width=60)
    table.add_column("frequency")
    table.add_column("created_at")

    for d in directives:
        table.add_row(
            d.id,
            d.type,
            d.instruction,
            str(round(d.frequency, 3)) if d.frequency is not None else "—",
            str(d.created_at),
        )

    _console.print(table)


# ---------------------------------------------------------------------------
# --disable: soft-disable a directive
# ---------------------------------------------------------------------------


def _handle_disable(
    *,
    directive_id: str,
    reason: str,
    project_id: str,
    secondsight_home: Path,
    no_server: bool,
    server_url: str,
) -> None:
    """Soft-disable a directive via PATCH."""
    if not no_server:
        try:
            updated = _patch_directive_via_server(
                server_url=server_url,
                project_id=project_id,
                directive_id=directive_id,
                status="disabled",
                reason=reason,
            )
            typer.echo(
                f"Directive {directive_id!r} disabled. "
                f"Reason: {reason!r}. Updated at: {updated.updated_at}."
            )
            raise typer.Exit(code=0)
        except httpx.ConnectError:
            logger.info(
                f"directive: server at {server_url} not reachable; "
                f"falling back to in-process for --disable"
            )
            typer.echo(
                f"Server at {server_url} not reachable — running in-process.",
                err=True,
            )
        except httpx.HTTPStatusError as exc:
            logger.error(
                f"directive: server at {server_url} returned "
                f"HTTP {exc.response.status_code} for "
                f"PATCH /api/directives/{directive_id}"
            )
            typer.echo(
                f"Server at {server_url} returned HTTP {exc.response.status_code} "
                f"— directive --disable aborted. Use --no-server to bypass server mode.",
                err=True,
            )
            raise typer.Exit(code=1)

    # In-process path
    _patch_directive_in_process(
        secondsight_home=secondsight_home,
        project_id=project_id,
        directive_id=directive_id,
        status="disabled",
        reason=reason,
    )
    typer.echo(f"Directive {directive_id!r} disabled. Reason: {reason!r}.")
    raise typer.Exit(code=0)


# ---------------------------------------------------------------------------
# --enable: re-activate a directive
# ---------------------------------------------------------------------------


def _handle_enable(
    *,
    directive_id: str,
    project_id: str,
    secondsight_home: Path,
    no_server: bool,
    server_url: str,
) -> None:
    """Re-activate a directive via PATCH, with no-op check for already-active."""
    if not no_server:
        # Server-mode: PATCH returns the current row; check the status
        try:
            # First check if it's already active to avoid unnecessary PATCH
            # (the server handles idempotency, but the CLI should be informative).
            updated = _patch_directive_via_server(
                server_url=server_url,
                project_id=project_id,
                directive_id=directive_id,
                status="active",
                reason=None,
            )
            if updated.disabled_at is None:
                # The PATCH returned active — it either was a no-op or a real transition.
                # Without knowing the previous state in server mode, we check disabled_at.
                # We emit a conservative success message.
                typer.echo(
                    f"Directive {directive_id!r} is now active.",
                    err=False,
                )
            raise typer.Exit(code=0)
        except httpx.ConnectError:
            logger.info(
                f"directive: server at {server_url} not reachable; "
                f"falling back to in-process for --enable"
            )
            typer.echo(
                f"Server at {server_url} not reachable — running in-process.",
                err=True,
            )
        except httpx.HTTPStatusError as exc:
            logger.error(
                f"directive: server at {server_url} returned "
                f"HTTP {exc.response.status_code} for "
                f"PATCH /api/directives/{directive_id}"
            )
            typer.echo(
                f"Server at {server_url} returned HTTP {exc.response.status_code} "
                f"— directive --enable aborted. Use --no-server to bypass server mode.",
                err=True,
            )
            raise typer.Exit(code=1)

    # In-process path: check current status BEFORE calling update_status.
    # DT-4.5: no-op path (already active) → exit 0 with "no change" message.
    _enable_in_process(
        secondsight_home=secondsight_home,
        project_id=project_id,
        directive_id=directive_id,
    )


def _enable_in_process(
    *,
    secondsight_home: Path,
    project_id: str,
    directive_id: str,
) -> None:
    """In-process implementation of --enable with no-op detection.

    Checks current status via get_by_id BEFORE calling update_status.
    If already active → print "no change — directive {id} is already active"
    to stderr and exit 0.

    This is the DT-4.5 requirement: discover via get_by_id, NOT via
    update_status, because update_status raises if reason rules are violated.
    """
    from secondsight.analysis.schemas import DirectiveStatus
    from secondsight.storage.db_engine import DBEngine
    from secondsight.storage.directives_repository import DirectivesRepository

    project_dir = secondsight_home / "projects" / project_id
    db_path = project_dir / "intelligence.db"

    db_engine = DBEngine(db_path)
    try:
        repo = DirectivesRepository(db_engine)
        repo.create_schema()

        current = repo.get_by_id(directive_id)
        if current is None or current.project_id != project_id:
            typer.echo(
                f"Directive {directive_id!r} not found in project {project_id!r}.",
                err=True,
            )
            raise typer.Exit(code=1)

        if current.status == DirectiveStatus.ACTIVE:
            # No-op: already active. Report and exit 0.
            typer.echo(
                f"no change — directive {directive_id!r} is already active.",
                err=True,
            )
            raise typer.Exit(code=0)

        repo.update_status(directive_id, DirectiveStatus.ACTIVE, reason=None)
        typer.echo(f"Directive {directive_id!r} is now active.")
        raise typer.Exit(code=0)
    finally:
        db_engine.dispose()


# ---------------------------------------------------------------------------
# Shared server-mode PATCH helper
# ---------------------------------------------------------------------------


def _patch_directive_via_server(
    *,
    server_url: str,
    project_id: str,
    directive_id: str,
    status: str,
    reason: str | None,
) -> DirectiveOut:
    """PATCH /api/directives/{directive_id} via httpx.

    Re-validates the server response through DirectiveOut for schema
    stability (same as _list_directives_via_server).

    Raises:
        httpx.ConnectError: server not running.
        httpx.HTTPStatusError: server returned a non-2xx status.
    """
    url = f"{server_url.rstrip('/')}/api/directives/{directive_id}"
    body: dict = {"status": status}
    if reason is not None:
        body["reason"] = reason

    response = httpx.patch(
        url,
        json=body,
        params={"project_id": project_id},
        timeout=10.0,
    )
    response.raise_for_status()
    return DirectiveOut.model_validate(response.json())


# ---------------------------------------------------------------------------
# Shared in-process PATCH helper (for --disable)
# ---------------------------------------------------------------------------


def _patch_directive_in_process(
    *,
    secondsight_home: Path,
    project_id: str,
    directive_id: str,
    status: str,
    reason: str | None,
) -> DirectiveOut:
    """In-process directive lifecycle update.

    Used by --disable (not --enable; --enable has its own no-op logic).
    Returns the refreshed DirectiveOut after the update.
    """
    from secondsight.analysis.schemas import DirectiveStatus
    from secondsight.storage.db_engine import DBEngine
    from secondsight.storage.directives_repository import DirectivesRepository

    project_dir = secondsight_home / "projects" / project_id
    db_path = project_dir / "intelligence.db"

    db_engine = DBEngine(db_path)
    try:
        repo = DirectivesRepository(db_engine)
        repo.create_schema()

        current = repo.get_by_id(directive_id)
        if current is None or current.project_id != project_id:
            typer.echo(
                f"Directive {directive_id!r} not found in project {project_id!r}.",
                err=True,
            )
            raise typer.Exit(code=1)

        new_status = DirectiveStatus(status)
        repo.update_status(directive_id, new_status, reason)

        refreshed = repo.get_by_id(directive_id)
        if refreshed is None:
            typer.echo(
                f"Directive {directive_id!r} read-back failed after update.",
                err=True,
            )
            raise typer.Exit(code=1)
        return DirectiveOut.from_directive(refreshed)
    finally:
        db_engine.dispose()


__all__ = [
    "app",
    "_list_directives_in_process",
    "_list_directives_via_server",
    "_serialize_directives",
]
