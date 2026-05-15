"""Shared helpers and constants for E2E tests.

Shared location for helpers used by multiple E2E test files.

Port choice:
    _SERVER_PORT = 8420 is the default port in ServerConfig. Tests use this
    value to match the server's bind address without additional configuration.

    Port conflict constraint: CLI gated tests (test_mode_toggle_cli.py) and SDK
    gated tests (test_mode_toggle_sdk.py) both use port 8420. They MUST NOT run
    concurrently. pytest collects and runs tests serially by default (no -n flag),
    so this is safe in standard CI. If pytest-xdist is used with --numprocesses > 1,
    these test files must be marked to run in the same worker group or serialized
    via --forked.

    The legacy-config tests (test_legacy_config_upgrade.py) do NOT spin up a real
    HTTP server — they use Typer's CliRunner and in-process invocations. They are
    safe to run concurrently with the gated tests.

Silent failure conditions:
    - If two gated test runs overlap (different gate variables both set), both will
      attempt to bind port 8420 and one will fail with "address already in use".
      The error will be caught by _wait_for_server's timeout, producing a clear
      assertion failure ("Server did not become ready within Xs").
    - _poll_analysis_outputs_table distinguishes OperationalError (table not yet
      created — transient, continue polling) from unexpected DB errors (permanent,
      re-raise). This prevents 120s of silent polling for a DB-level bug.
"""

from __future__ import annotations

import time
from pathlib import Path

import sqlalchemy.exc

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_SERVER_HOST = "127.0.0.1"
_SERVER_PORT = 8420  # Default secondsight serve port (hardcoded in ServerConfig)
_SERVER_URL = f"http://{_SERVER_HOST}:{_SERVER_PORT}"

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def wait_for_server(url: str, timeout_s: float) -> bool:
    """Poll GET /health until 200 or timeout. Returns True if ready.

    Uses GET /health endpoint. Retries every 0.5s until timeout.
    Ignores connection errors and HTTP timeouts during startup.
    Returns False if server is not ready within timeout_s.
    """
    import requests

    deadline = time.monotonic() + timeout_s
    health_url = f"{url}/health"
    while time.monotonic() < deadline:
        try:
            resp = requests.get(health_url, timeout=2.0)
            if resp.status_code == 200:
                return True
        except requests.ConnectionError, requests.Timeout:
            pass
        time.sleep(0.5)
    return False


def poll_analysis_outputs_table(
    db_path: Path,
    session_id: str,
    timeout_s: float,
    interval_s: float = 2.0,
) -> dict | None:
    """Poll analysis_outputs table for a row matching session_id.

    Returns the row as a dict or None if not found within timeout.
    Imports SQLAlchemy only when called (avoids import-time overhead in skip scenarios).

    Exception handling:
        - sqlalchemy.exc.OperationalError: table may not exist yet during early server
          startup. Treated as transient — log at DEBUG and continue polling.
        - All other exceptions: unexpected DB error (disk full, corrupted DB, schema
          mismatch). Re-raised immediately so the test fails with actionable info
          rather than silently polling for the full timeout.

    This distinction is critical: a bare `except Exception: pass` would mask DB
    connection failures, making "dispatch never wrote to DB" indistinguishable from
    "DB was unreachable for the full 120s" in CI diagnostics.
    """
    import sqlalchemy as sa
    from loguru import logger

    engine = sa.create_engine(f"sqlite:///{db_path}")
    deadline = time.monotonic() + timeout_s

    while time.monotonic() < deadline:
        try:
            with engine.connect() as conn:
                result = conn.execute(
                    sa.text("SELECT * FROM analysis_outputs WHERE session_id = :sid LIMIT 1"),
                    {"sid": session_id},
                )
                row = result.mappings().first()
                if row is not None:
                    return dict(row)
        except sqlalchemy.exc.OperationalError as exc:
            # Table may not exist yet during early startup — transient, continue polling
            logger.debug(f"poll_analysis_outputs_table: table not ready yet: {exc}")
        except Exception:
            # Unexpected DB error — re-raise so test fails with actionable info
            raise
        time.sleep(interval_s)

    return None
