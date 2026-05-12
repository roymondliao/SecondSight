"""Daemon control utilities for the SecondSight server (P1-6).

POSIX-only.  Windows is explicitly out of scope for Phase 1.

This module assumes:
- The server process is identified by the presence of "secondsight" in
  its cmdline (verified via /proc/<pid>/cmdline on Linux or ps on macOS).
  If a future refactor renames the binary, cmdline_match will fail false-
  positively for old PID files — this is acceptable (fail-safe direction).
- PID files are stored in a directory that is not world-writable.
  No PID file locking is performed beyond atomic write (tmp+rename).
  Two concurrent `serve --daemon` calls can race and the second silently
  overwrites the first PID (documented in scar report).

Silent failure conditions covered here:
- Half-written PID file: write_pidfile_atomic uses tmp+rename; if rename
  fails (OSError), the tmp file is cleaned up and pid_path never exists.
- Stale-PID kill: stop_daemon checks cmdline_match before sending SIGTERM.
- Orphaned tmp file: finally block in write_pidfile_atomic removes tmp.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from loguru import logger


# ---------------------------------------------------------------------------
# StopOutcome enum
# ---------------------------------------------------------------------------


class StopOutcome(str, Enum):
    """Result of stop_daemon().

    Encodes the distinct outcomes so callers can print honest messages
    without re-querying daemon_status (which would introduce a TOCTOU window).
    """

    NOT_RUNNING = "not_running"
    """No PID file or PID file points at no live process."""

    STOPPED_GRACEFUL = "stopped_graceful"
    """SIGTERM was honored within the grace period."""

    STOPPED_SIGKILL = "stopped_sigkill"
    """SIGTERM was ignored; SIGKILL was sent and the process is now dead."""

    REFUSED_STALE = "refused_stale"
    """PID file points at a process whose cmdline does NOT match secondsight serve."""


# ---------------------------------------------------------------------------
# PID file helpers
# ---------------------------------------------------------------------------


def write_pidfile_atomic(pid_path: Path, pid: int) -> None:
    """Write `pid` to `pid_path` atomically using tmp+rename.

    If os.replace raises (simulated crash), the tmp file is removed and
    pid_path is guaranteed to NOT exist (or to retain its previous content
    if it existed before).

    Raises:
        OSError: on any filesystem failure.
    """
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path = pid_path.parent / f".tmp_pid_{pid}_{os.getpid()}"
    # Track whether cleanup is needed.  Set to False after successful rename.
    _needs_cleanup = True
    try:
        tmp_path.write_text(str(pid) + "\n", encoding="utf-8")
        os.replace(str(tmp_path), str(pid_path))
        _needs_cleanup = False  # ownership transferred; do not clean up in finally
    finally:
        if _needs_cleanup and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass  # best-effort cleanup


def read_pidfile(pid_path: Path) -> int | None:
    """Read and parse a PID file.  Returns None if missing or non-integer."""
    try:
        text = pid_path.read_text(encoding="utf-8").strip()
        return int(text)
    except FileNotFoundError, ValueError:
        return None
    except OSError:
        return None


# ---------------------------------------------------------------------------
# Process identity check
# ---------------------------------------------------------------------------


def _get_process_cmdline(pid: int) -> str | None:
    """Return the cmdline string for `pid`, or None if unreadable.

    Uses /proc on Linux, `ps` on macOS (and other POSIX).
    """
    proc_path = Path(f"/proc/{pid}/cmdline")
    if proc_path.exists():
        try:
            # /proc/<pid>/cmdline is NUL-separated
            raw = proc_path.read_bytes()
            return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        except OSError:
            return None

    # macOS / BSD: use ps
    try:
        import subprocess  # noqa: PLC0415

        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            return result.stdout.strip()
        return None
    except OSError, subprocess.TimeoutExpired:
        return None


def _is_secondsight_cmdline(cmdline: str | None) -> bool:
    """Return True iff cmdline looks like a secondsight server process.

    We check for both:
    - `secondsight serve` (CLI invocation)
    - `uvicorn` with secondsight in args (direct uvicorn invocation)

    We explicitly do NOT match on "secondsight" alone — that would
    match any process running inside the secondsight project (e.g. pytest),
    creating a false-positive cmdline_match and allowing stale-PID kills
    against the test runner.
    """
    if cmdline is None:
        return False
    cmdline_lower = cmdline.lower()
    # Match `secondsight serve` or `secondsight-serve` invocations
    if "secondsight" in cmdline_lower and "serve" in cmdline_lower:
        return True
    # Match direct uvicorn invocations serving secondsight
    if "uvicorn" in cmdline_lower and "secondsight" in cmdline_lower:
        return True
    return False


# ---------------------------------------------------------------------------
# DaemonStatus
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DaemonStatus:
    """Result of daemon_status()."""

    running: bool
    pid: int | None
    cmdline_match: bool
    uptime_seconds: int | None


def daemon_status(pid_path: Path) -> DaemonStatus:
    """Check the daemon's status.

    1. Read pid_path.  If missing → not running.
    2. Check os.kill(pid, 0).  If fails → not running.
    3. Read cmdline for that pid.  If not secondsight → cmdline_match=False.

    Note: uptime_seconds is not tracked in Phase 1 (no start-time file).
    Returns None for uptime_seconds; deferred to Phase 2.
    """
    pid = read_pidfile(pid_path)
    if pid is None:
        return DaemonStatus(running=False, pid=None, cmdline_match=False, uptime_seconds=None)

    # Check process liveness
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return DaemonStatus(running=False, pid=pid, cmdline_match=False, uptime_seconds=None)
    except PermissionError:
        # Process exists but we can't signal it (different user).
        # Treat as running but cmdline unknown.
        cmdline = _get_process_cmdline(pid)
        return DaemonStatus(
            running=True,
            pid=pid,
            cmdline_match=_is_secondsight_cmdline(cmdline),
            uptime_seconds=None,
        )

    cmdline = _get_process_cmdline(pid)
    return DaemonStatus(
        running=True,
        pid=pid,
        cmdline_match=_is_secondsight_cmdline(cmdline),
        uptime_seconds=None,
    )


# ---------------------------------------------------------------------------
# stop_daemon
# ---------------------------------------------------------------------------


def stop_daemon(pid_path: Path, *, grace_seconds: float = 5.0) -> StopOutcome:
    """Stop the daemon referenced by pid_path.

    Safety check: verifies cmdline_match before sending any signal.

    Returns a StopOutcome enum value that encodes exactly what happened:
    - NOT_RUNNING:      No PID file or process at that PID is already gone.
    - STOPPED_GRACEFUL: SIGTERM was honored within grace_seconds.
    - STOPPED_SIGKILL:  SIGTERM ignored; SIGKILL sent and process is dead.
    - REFUSED_STALE:    PID file points at a non-secondsight process.

    This design assumption: "secondsight" appearing in the process cmdline
    is a sufficient identity check.  If it stops holding (e.g. a DIFFERENT
    secondsight process occupies the PID), the kill is still semantically
    safe (we're killing a SecondSight process, just maybe not the intended
    one).  The truly dangerous case — killing a COMPLETELY UNRELATED process
    — is blocked by this check.
    """
    status = daemon_status(pid_path)

    if not status.running:
        logger.info("stop_daemon: no running daemon found at {path}", path=pid_path)
        return StopOutcome.NOT_RUNNING

    if not status.cmdline_match:
        logger.warning(
            "stop_daemon: PID {pid} does not match secondsight cmdline — "
            "refusing to kill (stale PID file?)",
            pid=status.pid,
        )
        return StopOutcome.REFUSED_STALE

    pid = status.pid
    if pid is None:
        raise RuntimeError("expected non-None pid after running=True path")

    logger.info("stop_daemon: sending SIGTERM to PID {pid}", pid=pid)
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        # Raced: process exited between status check and kill.
        logger.info("stop_daemon: PID {pid} already gone before SIGTERM", pid=pid)
        try:
            pid_path.unlink(missing_ok=True)
        except OSError:
            pass
        return StopOutcome.STOPPED_GRACEFUL

    # Wait up to grace_seconds for the process to exit.
    deadline = time.monotonic() + grace_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            # Process exited cleanly.
            logger.info("stop_daemon: PID {pid} exited cleanly", pid=pid)
            try:
                pid_path.unlink(missing_ok=True)
            except OSError:
                pass
            return StopOutcome.STOPPED_GRACEFUL
        time.sleep(0.1)

    # Grace period expired — SIGKILL.
    logger.warning(
        "stop_daemon: PID {pid} did not exit within {grace}s — sending SIGKILL",
        pid=pid,
        grace=grace_seconds,
    )
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass  # Already gone

    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass

    return StopOutcome.STOPPED_SIGKILL


# ---------------------------------------------------------------------------
# daemonize — double-fork
# ---------------------------------------------------------------------------


def daemonize(
    *,
    pid_path: Path,
    log_path: Path,
    on_child: Callable[[], None],
) -> None:
    """Double-fork into background.  Parent returns; child runs `on_child()`.

    POSIX-only.  Raises NotImplementedError on Windows.

    Contract:
    - Parent returns immediately after the first fork.
    - Intermediate child (first-fork child) becomes a new session leader
      and forks again, then exits.  This ensures the final daemon child
      is NOT a session leader and cannot acquire a controlling terminal.
    - Daemon child (second-fork child):
      * Changes working directory to '/'.
      * Redirects stdin to /dev/null.
      * Redirects stdout and stderr to log_path (append mode).
      * Closes all file descriptors > 2 (fd leak prevention).
      * Writes its PID atomically to pid_path.
      * Calls on_child().
    - Writes pid_path atomically (tmp+rename) from the daemon child.

    Assumption: on_child() is expected to start the server loop and never
    return normally.  If on_child() returns or raises, the daemon child
    calls os._exit(1) to prevent any atexit/cleanup from running in the
    unexpected-exit path.

    If daemonize() is called in an asyncio context, the event loop in the
    parent is NOT valid in the child (forking with active event loop is
    undefined behavior on some platforms).  Call daemonize() BEFORE
    starting the event loop.
    """
    if sys.platform == "win32":
        raise NotImplementedError("daemonize() is POSIX-only")

    # Ensure the log directory exists before forking.
    log_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.parent.mkdir(parents=True, exist_ok=True)

    # First fork
    try:
        pid = os.fork()
    except OSError as exc:
        raise RuntimeError(f"daemonize: first fork failed: {exc}") from exc

    if pid > 0:
        # Parent: return immediately.
        return

    # --- First-fork child ---
    # Become a new session leader (decouple from controlling terminal).
    os.setsid()

    # Second fork — ensures the daemon child is not a session leader and
    # cannot reacquire a controlling terminal.
    try:
        pid = os.fork()
    except OSError as exc:
        logger.error("daemonize: second fork failed: {exc}", exc=exc)
        os._exit(1)

    if pid > 0:
        # Intermediate child: exit cleanly.
        os._exit(0)

    # --- Daemon child (second-fork child) ---
    # Change working directory to root so we don't hold any mount points.
    os.chdir("/")

    # Open the log file before closing fds so we have a valid target.
    # NOTE: stderr is still the original parent's stderr at this point —
    # fd-redirection happens AFTER this block.  Writing to sys.stderr here
    # surfaces the diagnostic to the terminal that ran `serve --daemon`.
    try:
        log_fd = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    except OSError as log_err:
        print(
            f"daemonize: WARNING: cannot open log {log_path}: {log_err}; "
            "redirecting to /dev/null — the daemon will run but produce no output",
            file=sys.stderr,
        )
        log_fd = os.open(os.devnull, os.O_RDWR)

    # Redirect stdin to /dev/null.
    null_fd = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null_fd, 0)  # stdin
    os.close(null_fd)

    # Redirect stdout and stderr to the log file.
    os.dup2(log_fd, 1)  # stdout
    os.dup2(log_fd, 2)  # stderr
    os.close(log_fd)

    # Close all file descriptors > 2 to prevent fd leaks from the parent.
    # We use a conservative upper bound; getrlimit gives us the actual limit.
    try:
        import resource  # noqa: PLC0415

        max_fd = resource.getrlimit(resource.RLIMIT_NOFILE)[0]
    except ImportError, ValueError:
        max_fd = 1024

    # Close fds 3..max_fd-1.  Ignore errors (most fds won't be open).
    # Use os.closerange if available (Python 3.12+).
    if hasattr(os, "closerange"):
        os.closerange(3, max_fd)
    else:
        for fd_num in range(3, max_fd):
            try:
                os.close(fd_num)
            except OSError:
                pass

    # Write PID file atomically.
    try:
        write_pidfile_atomic(pid_path, os.getpid())
    except OSError as exc:
        # Can't write PID file — log and continue (server can still run,
        # status/stop commands will just not work).
        print(f"daemonize: WARNING: could not write PID file {pid_path}: {exc}", file=sys.stderr)

    # Run the server.
    try:
        on_child()
    except Exception as exc:
        print(f"daemonize: on_child() raised: {exc}", file=sys.stderr)
        os._exit(1)

    os._exit(0)


__all__ = [
    "DaemonStatus",
    "StopOutcome",
    "daemon_status",
    "daemonize",
    "read_pidfile",
    "stop_daemon",
    "write_pidfile_atomic",
]
