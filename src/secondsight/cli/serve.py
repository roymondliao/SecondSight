"""Typer command group for server lifecycle management (P1-6 + GUR-98).

CLI surface:
  secondsight serve            # foreground (blocking, for dev)
  secondsight serve --daemon   # double-fork into background
  secondsight serve --stop     # send SIGTERM; SIGKILL on timeout

`secondsight status` (separate subcommand under cli/status.py) supersedes
the historical `serve status` form.

Design assumptions:
- DEFAULT_HOME is ~/.secondsight.  Override via SECONDSIGHT_HOME env var
  (checked at invocation time, not at import time).
- daemonize() is called BEFORE uvicorn starts, so there is no asyncio
  event loop in the parent process that could be corrupted by os.fork().
- On macOS, forking after asyncio has started is undefined behavior.
  We guard against this by calling daemonize() from the CLI command
  (before any event loop startup).

Silent failure conditions:
- If `--daemon` is run twice, the second call overwrites the PID file.
  The old daemon process keeps running but stop/status can no longer
  address it by PID.  Documented in scar report; no guard in Phase 1.
- If `serve` is run without write access to SECONDSIGHT_HOME, the error
  surfaces during DBEngine construction on the first request (not at CLI
  startup).  Deferred — validated only at registry init for now.
"""

from __future__ import annotations

from pathlib import Path

import typer
import uvicorn
from loguru import logger

from secondsight.api.server import ServerConfig, create_app
from secondsight.cli._home import secondsight_home as resolve_secondsight_home
from secondsight.daemon import StopOutcome, daemon_status, daemonize, stop_daemon

app = typer.Typer(name="serve", help="Manage the SecondSight daemon server.")


def _pid_path(home: Path) -> Path:
    return home / "server.pid"


def _log_path(home: Path) -> Path:
    return home / "logs" / "server.log"


def _run_server(home: Path) -> None:
    """Start uvicorn in the current process.  Blocking call.

    This is the shared entry point for both foreground and daemon modes.

    workers=1 is explicit because the asyncio.Lock-based ProjectRegistry is
    single-process only.  Without this, WEB_CONCURRENCY could silently set
    workers > 1 and break the registry's per-project locking invariant.
    """
    cfg = ServerConfig()
    server_app = create_app(secondsight_home=home, config=cfg)
    uvicorn.run(server_app, host=cfg.host, port=cfg.port, workers=1)


@app.callback(invoke_without_command=True)
def serve(
    ctx: typer.Context,
    daemon: bool = typer.Option(False, "--daemon", help="Run as a background daemon."),
    stop: bool = typer.Option(False, "--stop", help="Stop the running daemon."),
    home: str = typer.Option("", "--home", help="SecondSight home directory."),
) -> None:
    """Start (or stop) the SecondSight server.

    Without flags: run in foreground (blocking).
    --daemon: double-fork into background.
    --stop: send SIGTERM to the running daemon.
    """
    if ctx.invoked_subcommand is not None:  # pragma: no cover — no subcmds
        return

    resolved_home: Path = resolve_secondsight_home(home)

    if stop:
        _do_stop(resolved_home)
        return

    if daemon:
        _do_daemon(resolved_home)
        return

    # Foreground mode
    logger.info("Starting SecondSight server in foreground (home={h})", h=resolved_home)
    _run_server(resolved_home)


def _do_stop(home: Path) -> None:
    pid = _pid_path(home)
    typer.echo(f"Stopping SecondSight daemon (pid file: {pid})...")
    outcome = stop_daemon(pid, grace_seconds=5.0)
    if outcome is StopOutcome.NOT_RUNNING:
        typer.echo("Daemon was not running.")
    elif outcome is StopOutcome.STOPPED_GRACEFUL:
        typer.echo("Daemon stopped gracefully.")
    elif outcome is StopOutcome.STOPPED_SIGKILL:
        typer.echo(
            "Daemon did not respond to SIGTERM within 5.0s; sent SIGKILL.",
            err=True,
        )
        raise typer.Exit(1)
    elif outcome is StopOutcome.REFUSED_STALE:
        typer.echo(
            f"PID file points at process whose cmdline does not match "
            f"secondsight serve; refusing to kill. "
            f"Remove {pid} manually if stale.",
            err=True,
        )
        raise typer.Exit(1)


def _do_daemon(home: Path) -> None:
    pid = _pid_path(home)
    log = _log_path(home)

    # Check if already running
    status = daemon_status(pid)
    if status.running and status.cmdline_match:
        typer.echo(
            f"SecondSight daemon is already running (PID {status.pid}).",
            err=True,
        )
        raise typer.Exit(1)

    typer.echo(f"Starting SecondSight daemon (home={home}, log={log})...")

    def on_child() -> None:
        _run_server(home)

    daemonize(pid_path=pid, log_path=log, on_child=on_child)
    typer.echo("Daemon started.")


__all__ = ["app"]
