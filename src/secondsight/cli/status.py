"""`secondsight status` — server + per-project overview (P1-12).

Shows whether the daemon is running (delegates to ``daemon.daemon_status``)
plus a per-project snapshot: events in DB, sessions on disk, and any sync_log
backlog. SD §9.1 dual-persona output: human-readable table by default,
``--format json`` for agents.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from secondsight.api.registry import ProjectRegistry
from secondsight.cli._home import secondsight_home as resolve_secondsight_home
from secondsight.daemon import daemon_status

app = typer.Typer(name="status", help="Print SecondSight daemon + project status.")
_console = Console()


@app.callback(invoke_without_command=True)
def status(
    ctx: typer.Context,
    home: str = typer.Option(
        "",
        "--home",
        help="Override SecondSight home (default: $SECONDSIGHT_HOME or ~/.secondsight).",
    ),
    output_format: str = typer.Option(
        "text",
        "--format",
        help="Output format: 'text' (default, Rich) or 'json'.",
    ),
) -> None:
    """Print server + per-project status."""
    if ctx.invoked_subcommand is not None:  # pragma: no cover
        return

    home_path = resolve_secondsight_home(home)
    pid_path = home_path / "server.pid"

    daemon = daemon_status(pid_path)
    server_payload = {
        "running": daemon.running,
        "pid": daemon.pid,
        "cmdline_match": daemon.cmdline_match,
        "pid_file": str(pid_path),
    }

    project_payloads = _gather_project_status(home_path)

    if output_format == "json":
        typer.echo(
            json.dumps(
                {"server": server_payload, "projects": project_payloads},
                indent=2,
                sort_keys=True,
            )
        )
        return

    _render_text(server_payload, project_payloads)


def _gather_project_status(home: Path) -> list[dict[str, Any]]:
    projects_dir = home / "projects"
    if not projects_dir.is_dir():
        return []

    # Build a registry only so we go through the same validation path as the
    # server. Each project's resources are loaded synchronously below.
    registry = ProjectRegistry(secondsight_home=home)

    out: list[dict[str, Any]] = []
    for child in sorted(projects_dir.iterdir()):
        if not child.is_dir():
            continue
        pid = child.name
        try:
            resources = registry._build_resources(pid)  # noqa: SLF001
        except Exception as exc:  # noqa: BLE001 — surface to operator
            out.append(
                {
                    "project_id": pid,
                    "error": f"{type(exc).__name__}: {exc}",
                    "events_in_db": None,
                    "sessions_on_disk": None,
                    "sync_log_pending": None,
                }
            )
            continue
        try:
            events_in_db = _count_events(resources.events_repository)
            sessions_on_disk = _count_sessions(child / "sessions")
            sync_pending = sum(1 for _ in resources.sync_log.iter_pending())
            out.append(
                {
                    "project_id": pid,
                    "error": None,
                    "events_in_db": events_in_db,
                    "sessions_on_disk": sessions_on_disk,
                    "sync_log_pending": sync_pending,
                }
            )
        finally:
            resources.db_engine.dispose()
    return out


def _count_events(repo: Any) -> int:
    """Count rows in the events table.

    EventsRepository has no public `count()` today (out of P1-3 scope), so
    we use a small SELECT COUNT(*) directly. Safe — single column, no user
    input. Adding a real method to the repo is deferred to avoid expanding
    GUR-98's blast radius.
    """
    import sqlalchemy as sa

    from secondsight.storage.events_table import events

    stmt = sa.select(sa.func.count()).select_from(events)
    with repo._db.engine.connect() as conn:  # noqa: SLF001 — minimal helper
        return int(conn.execute(stmt).scalar() or 0)


def _count_sessions(sessions_dir: Path) -> int:
    if not sessions_dir.is_dir():
        return 0
    return sum(1 for child in sessions_dir.iterdir() if child.is_dir())


def _render_text(server: dict[str, Any], projects: list[dict[str, Any]]) -> None:
    if server["running"] and server["cmdline_match"]:
        _console.print(
            f"[green]server running[/green]  pid={server['pid']}  pid_file={server['pid_file']}"
        )
    elif server["running"]:
        _console.print(
            f"[yellow]pid file points at non-secondsight process[/yellow]  "
            f"pid={server['pid']}  pid_file={server['pid_file']}"
        )
    else:
        _console.print(f"[red]server not running[/red]  pid_file={server['pid_file']}")

    if not projects:
        _console.print("[dim]no projects under <home>/projects/[/dim]")
        return

    table = Table(title="projects")
    table.add_column("project_id")
    table.add_column("events in DB", justify="right")
    table.add_column("sessions on disk", justify="right")
    table.add_column("sync_log pending", justify="right")
    for p in projects:
        if p.get("error"):
            table.add_row(p["project_id"], "?", "?", "?")
            continue
        table.add_row(
            p["project_id"],
            str(p["events_in_db"]),
            str(p["sessions_on_disk"]),
            str(p["sync_log_pending"]),
        )
    _console.print(table)
    for p in projects:
        if p.get("error"):
            _console.print(f"[yellow]{p['project_id']}: error[/yellow] {p['error']}")


__all__ = ["app"]
