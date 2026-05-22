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
from secondsight.cli import _home as cli_home
from secondsight.config import SecondSightConfig, SecondSightConfigError, load_global_config
from secondsight.daemon import StopOutcome, daemon_status, daemonize, stop_daemon
from secondsight.logging_utils import configure_logging

app = typer.Typer(name="serve", help="Manage the SecondSight daemon server.")


def _pid_path(home: Path) -> Path:
    return home / "server.pid"


def _log_path(home: Path) -> Path:
    return home / "logs" / "server.log"


def _run_precheck(home: Path) -> SecondSightConfig:
    """Run server startup pre-check. Exits non-zero on failure.

    Called BEFORE _run_server() to validate that the config is consistent
    and all required resources are available. If precheck fails, the server
    does NOT start in degraded mode — it exits with code 1.

    Design:
    - Config is loaded from the global config.toml (not per-project).
    - State is loaded from ~/.secondsight/state.json (may be None on fresh install).
    - If config loading itself fails (malformed TOML), logs the error and exits 1.

    DC5: catches missing state.json and missing CLI binaries.
    DC6: on CLI mode success, logs the resolved binary path for forensics.
    DC7: catches empty provider keys for SDK mode.
    """
    from secondsight.config.precheck import precheck
    from secondsight.state import SecondSightState, SecondSightStateError

    # Load config first — if this fails, we can't even run precheck
    try:
        config = load_global_config(home=home)
    except SecondSightConfigError as exc:
        logger.error(
            f"secondsight serve: config load failed: {exc}. "
            f"Fix config.toml before starting the server."
        )
        raise typer.Exit(code=1)
    except OSError as exc:
        logger.error(
            f"secondsight serve: cannot read config file: {exc}. "
            f"Check that config.toml exists and is readable."
        )
        raise typer.Exit(code=1)

    configure_logging(config.general.log_level)

    # Load state (may be None on fresh install — not an error at this layer)
    state_path = home / "state.json"
    state: SecondSightState | None = None
    try:
        state = SecondSightState.load(state_path)
    except SecondSightStateError as exc:
        logger.warning(
            f"secondsight serve: state.json load failed: {exc}. "
            f"Proceeding with state=None (precheck may fail for mode=cli + default_agent=auto)."
        )

    # Run precheck — returns PrecheckResult, never raises
    result = precheck(config=config, state=state)

    if not result.is_ok:
        logger.error(
            f"secondsight serve: startup pre-check FAILED. "
            f"reason={result.reason!r} message={result.message!r}. "
            f"The server will not start. Fix the configuration and try again."
        )
        typer.echo(
            f"Server startup pre-check failed: {result.message}",
            err=True,
        )
        raise typer.Exit(code=1)

    logger.info(f"secondsight serve: startup pre-check passed (mode={config.general.mode!r}).")
    return config


def _run_server(home: Path, config: SecondSightConfig | None = None) -> None:
    """Start uvicorn in the current process.  Blocking call.

    This is the shared entry point for both foreground and daemon modes.

    workers=1 is explicit because the asyncio.Lock-based ProjectRegistry is
    single-process only.  Without this, WEB_CONCURRENCY could silently set
    workers > 1 and break the registry's per-project locking invariant.
    """
    resolved_config = config
    if resolved_config is None:
        resolved_config = load_global_config(home=home)

    resolved_log_level = configure_logging(resolved_config.general.log_level)
    cfg = ServerConfig()
    server_app = create_app(secondsight_home=home, config=cfg)
    uvicorn.run(
        server_app,
        host=cfg.host,
        port=cfg.port,
        workers=1,
        log_level=resolved_log_level,
    )


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

    resolved_home: Path = cli_home.secondsight_home(home)

    if stop:
        _do_stop(resolved_home)
        return

    if daemon:
        _do_daemon(resolved_home)
        return

    # Foreground mode: precheck BEFORE server start (DC5/DC6/DC7)
    config = _run_precheck(resolved_home)
    logger.info("Starting SecondSight server in foreground (home={h})", h=resolved_home)
    _run_server(resolved_home, config)


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

    # Precheck BEFORE daemonizing (DC5/DC6/DC7): fail fast in the parent
    # process so the user sees the error. If precheck runs in the child,
    # the error goes to the log file and the user sees "Daemon started." — wrong.
    config = _run_precheck(home)

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
        _run_server(home, config)

    daemonize(pid_path=pid, log_path=log, on_child=on_child)
    typer.echo("Daemon started.")


__all__ = ["app"]
