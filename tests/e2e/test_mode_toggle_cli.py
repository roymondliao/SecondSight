"""E2E smoke tests for mode=cli analysis dispatch.

GATED: These tests require SECONDSIGHT_TEST_REAL_CLI=1 and a real `claude`
or `codex` binary in PATH with valid auth. They are NOT run in PR CI.

Intended pipeline: nightly, release, or manual operator probe.

Test design:
- Spins up `secondsight serve` as a subprocess in a temp home directory.
- Waits for GET /health to return 200 (server ready).
- POSTs a session_end event via HTTP.
- Polls the analysis_outputs table (via SQLAlchemy) for a result row.
- Asserts row fields: dispatched_via="cli", cli_agent=<configured>,
  status="success" within a timeout.

Assumption: state.json is pre-written (bypasses `secondsight init` hook install).
Assumption: CI environment has no real `claude` binary — tests skip cleanly.

Silent failure conditions for this module:
  - If SECONDSIGHT_TEST_REAL_CLI=1 is set but claude binary is absent: skips
    individual tests with a clear message rather than failing obscurely.
  - If server does not start within STARTUP_TIMEOUT_S: test fails with
    explicit timeout message (not a hanging test).
  - If DB row is not written within POLL_TIMEOUT_S: test fails explicitly
    (dispatch may have silently dropped the event).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import requests

from tests.e2e.conftest import (
    _SERVER_PORT,  # noqa: F401 (used by downstream for reference)
    _SERVER_URL,
    poll_analysis_outputs_table as _shared_poll_analysis_outputs_table,
    wait_for_server as _wait_for_server_shared,
)

# ---------------------------------------------------------------------------
# Gate: skip unless SECONDSIGHT_TEST_REAL_CLI=1
# ---------------------------------------------------------------------------

requires_real_cli = pytest.mark.skipif(
    os.environ.get("SECONDSIGHT_TEST_REAL_CLI") != "1",
    reason=(
        "Requires SECONDSIGHT_TEST_REAL_CLI=1 + real claude/codex binary in PATH + valid auth. "
        "Set SECONDSIGHT_TEST_REAL_CLI=1 to run these tests. "
        "Intended pipeline: nightly, release, or manual operator probe."
    ),
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SERVER_HOST = "127.0.0.1"
# _SERVER_PORT and _SERVER_URL are imported from conftest (shared with SDK tests).
# Both CLI and SDK gated tests use port 8420. DO NOT run them concurrently.
# See tests/e2e/conftest.py for the port conflict constraint documentation.
_STARTUP_TIMEOUT_S = 30.0
_POLL_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 2.0

_FIXTURES_DIR = Path(__file__).parent / "fixtures"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_temp_ss_home(tmp_path: Path, agent: str = "claude_code") -> Path:
    """Create a minimal temp ~/.secondsight home for CLI E2E tests."""
    ss_home = tmp_path / ".secondsight"
    ss_home.mkdir(parents=True, exist_ok=True)
    (ss_home / "logs").mkdir(exist_ok=True)

    # Write CLI mode config
    cli_config = _FIXTURES_DIR / "cli_mode_config.toml"
    (ss_home / "config.toml").write_text(cli_config.read_text(encoding="utf-8"), encoding="utf-8")

    # Write state.json directly (bypass hook install for E2E purposes)
    state_data = {
        "schema_version": "1.0",
        "init_agent": agent,
        "init_at": "2026-05-14T00:00:00+00:00",
        "secondsight_version": "test-e2e",
    }
    (ss_home / "state.json").write_text(json.dumps(state_data, indent=2), encoding="utf-8")

    return ss_home


def _wait_for_server(url: str, timeout_s: float) -> bool:
    """Poll GET /health until 200 or timeout. Returns True if ready.

    Delegates to shared helper in tests/e2e/conftest.py.
    """
    return _wait_for_server_shared(url, timeout_s)


def _make_session_start_payload(
    project_id: str = "e2e-proj-cli",
    session_id: str = "e2e-sess-cli-001",
    agent: str = "test",
    seq: int = 0,
) -> dict[str, Any]:
    """Build a minimal valid session_start hook payload (generic endpoint)."""
    return {
        "project_id": project_id,
        "session_id": session_id,
        "agent": agent,
        "event_id": f"evt-{session_id}-start-{seq}",
        "timestamp": "2026-05-14T12:00:00+00:00",
        "sequence_number": seq,
        "payload": {},
    }


def _make_session_end_payload(
    project_id: str = "e2e-proj-cli",
    session_id: str = "e2e-sess-cli-001",
    agent: str = "test",
    seq: int = 1,
) -> dict[str, Any]:
    """Build a minimal valid session_end hook payload (generic endpoint).

    Uses the /hook/session_end endpoint with IdentityAdapter format
    (project_id + session_id at top level, not inside payload).
    This avoids needing hook_event_name which is Claude Code adapter specific.
    """
    return {
        "project_id": project_id,
        "session_id": session_id,
        "agent": agent,
        "event_id": f"evt-{session_id}-end-{seq}",
        "timestamp": "2026-05-14T12:01:00+00:00",
        "sequence_number": seq,
        "payload": {},
    }


def _poll_analysis_outputs_table(
    db_path: Path,
    session_id: str,
    timeout_s: float,
    interval_s: float = _POLL_INTERVAL_S,
) -> dict | None:
    """Poll analysis_outputs table for a row matching session_id.

    Delegates to shared helper in tests/e2e/conftest.py which provides
    correct exception handling (OperationalError = transient, re-raise others).
    """
    return _shared_poll_analysis_outputs_table(
        db_path=db_path,
        session_id=session_id,
        timeout_s=timeout_s,
        interval_s=interval_s,
    )


# ---------------------------------------------------------------------------
# DEATH TESTS (gated)
# ---------------------------------------------------------------------------


@requires_real_cli
def test_cli_mode_e2e_dispatch_creates_analysis_row(tmp_path: Path) -> None:
    """DEATH TEST (gated): mode=cli full ingress → dispatch → storage chain.

    Verifies the complete path:
    1. secondsight serve starts with cli_mode_config.toml + state.json
    2. POST /hook/session_end with fixture session event
    3. analysis_outputs table receives a row with:
       - dispatched_via = "cli"
       - cli_agent = "claude_code"
       - status = "success"

    Silent failure path: if dispatch silently drops session_end events or the
    analysis_outputs table is never written, this test fails — the bug would
    otherwise only be discovered in production.
    """
    # Pre-check: require real claude binary
    if shutil.which("claude") is None:
        pytest.skip(
            "claude binary not found in PATH. Install Claude Code CLI to run CLI E2E tests."
        )

    ss_home = _make_temp_ss_home(tmp_path, agent="claude_code")
    # analysis_outputs table lives in per-project DB, not in root ss_home
    db_path = ss_home / "projects" / "e2e-proj-cli" / "intelligence.db"

    env = {**os.environ, "SECONDSIGHT_HOME": str(ss_home)}

    # Start server subprocess
    server_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "secondsight",
            "serve",
            "--home",
            str(ss_home),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        ready = _wait_for_server(_SERVER_URL, _STARTUP_TIMEOUT_S)
        assert ready, (
            f"Server did not become ready within {_STARTUP_TIMEOUT_S}s. "
            f"Check server logs at {ss_home / 'logs'!r}."
        )

        # POST session_start then session_end to trigger dispatch
        start_payload = _make_session_start_payload(
            project_id="e2e-proj-cli",
            session_id="e2e-sess-cli-001",
            seq=0,
        )
        start_resp = requests.post(
            f"{_SERVER_URL}/hook/session_start",
            json=start_payload,
            timeout=10.0,
        )
        assert start_resp.status_code == 200, (
            f"Expected 200 from /hook/session_start. "
            f"Got {start_resp.status_code}: {start_resp.text!r}."
        )

        end_payload = _make_session_end_payload(
            project_id="e2e-proj-cli",
            session_id="e2e-sess-cli-001",
            seq=1,
        )
        response = requests.post(
            f"{_SERVER_URL}/hook/session_end",
            json=end_payload,
            timeout=10.0,
        )
        assert response.status_code == 200, (
            f"Expected 200 from /hook/session_end. Got {response.status_code}: {response.text!r}."
        )

        # Poll for analysis_outputs row
        row = _poll_analysis_outputs_table(
            db_path=db_path,
            session_id="e2e-sess-cli-001",
            timeout_s=_POLL_TIMEOUT_S,
        )

        assert row is not None, (
            f"No analysis_outputs row found for session_id='e2e-sess-cli-001' "
            f"within {_POLL_TIMEOUT_S}s. "
            f"Either dispatch did not fire or the DB row was not written."
        )
        assert row.get("dispatched_via") == "cli", (
            f"Expected dispatched_via='cli'. Got: {row.get('dispatched_via')!r}."
        )
        assert row.get("cli_agent") == "claude_code", (
            f"Expected cli_agent='claude_code'. Got: {row.get('cli_agent')!r}."
        )
        assert row.get("status") == "success", (
            f"Expected status='success'. Got: {row.get('status')!r}. "
            f"error_details: {row.get('error_details')!r}."
        )

    finally:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server_proc.kill()


@requires_real_cli
def test_cli_mode_e2e_claude_binary_is_invoked(tmp_path: Path) -> None:
    """DEATH TEST (gated): verify real `claude` process was invoked during dispatch.

    Validates the subprocess invocation chain — not just that a row was written,
    but that the row's data indicates a real CLI call (not a mock or shortcut).

    Proxy: row.error_details must be None (no subprocess errors) for a successful run.
    """
    if shutil.which("claude") is None:
        pytest.skip("claude binary not found in PATH.")

    ss_home = _make_temp_ss_home(tmp_path, agent="claude_code")
    db_path = ss_home / "projects" / "e2e-proj-cli" / "intelligence.db"

    env = {**os.environ, "SECONDSIGHT_HOME": str(ss_home)}

    server_proc = subprocess.Popen(
        [sys.executable, "-m", "secondsight", "serve", "--home", str(ss_home)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        ready = _wait_for_server(_SERVER_URL, _STARTUP_TIMEOUT_S)
        assert ready, f"Server not ready within {_STARTUP_TIMEOUT_S}s."

        requests.post(
            f"{_SERVER_URL}/hook/session_start",
            json=_make_session_start_payload(session_id="e2e-sess-cli-binary-check", seq=0),
            timeout=10.0,
        )
        requests.post(
            f"{_SERVER_URL}/hook/session_end",
            json=_make_session_end_payload(session_id="e2e-sess-cli-binary-check", seq=1),
            timeout=10.0,
        )

        row = _poll_analysis_outputs_table(
            db_path=db_path,
            session_id="e2e-sess-cli-binary-check",
            timeout_s=_POLL_TIMEOUT_S,
        )

        assert row is not None, "No analysis row found within timeout."
        assert row.get("status") == "success", (
            f"Expected success. Got status={row.get('status')!r}, "
            f"error_details={row.get('error_details')!r}. "
            f"This may indicate the claude binary was not invoked or returned an error."
        )
        # If error_details is set, something went wrong in subprocess invocation
        assert not row.get("error_details"), (
            f"error_details must be empty for successful CLI dispatch. "
            f"Got: {row.get('error_details')!r}. "
            f"This suggests the subprocess failed or was not the real claude binary."
        )

    finally:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server_proc.kill()
