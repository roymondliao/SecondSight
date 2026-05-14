"""E2E smoke tests for mode=sdk analysis dispatch.

GATED: These tests require SECONDSIGHT_TEST_REAL_LLM=1 and a valid
$ANTHROPIC_API_KEY in environment. They are NOT run in PR CI.

Intended pipeline: nightly, release, or manual operator probe.

Test design:
- Spins up `secondsight serve` in a temp home directory with sdk_mode_config.toml.
- Waits for GET /health to return 200 (server ready).
- POSTs a session_end event via HTTP.
- Polls the analysis_outputs table for a result row.
- Asserts: dispatched_via="sdk", primary_model=<configured>, status="success".

Also covers:
- DC4 (reroute): invalid primary + valid fallback → fallback_used=True, status="success".
- Invalid key: sk-invalid → status="failure", error_details contains upstream error.

Silent failure conditions for this module:
  - If ANTHROPIC_API_KEY is set but invalid: individual test must fail with
    status="failure" and error_details containing upstream API error.
  - If server does not start within STARTUP_TIMEOUT_S: explicit timeout failure.
  - If analysis_outputs row not written within POLL_TIMEOUT_S: explicit failure.
  - DC4 fallback test MUST use distinct primary+fallback models to actually test
    the fallback path — same model for both would not verify routing.
"""

from __future__ import annotations

import os
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
# Gate: skip unless SECONDSIGHT_TEST_REAL_LLM=1
# ---------------------------------------------------------------------------

requires_real_llm = pytest.mark.skipif(
    os.environ.get("SECONDSIGHT_TEST_REAL_LLM") != "1",
    reason=(
        "Requires SECONDSIGHT_TEST_REAL_LLM=1 + valid $ANTHROPIC_API_KEY. "
        "Set SECONDSIGHT_TEST_REAL_LLM=1 to run these tests. "
        "Intended pipeline: nightly, release, or manual operator probe."
    ),
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SERVER_HOST = "127.0.0.1"
# _SERVER_PORT and _SERVER_URL are imported from conftest (shared with CLI tests).
# Both CLI and SDK gated tests use port 8420. DO NOT run them concurrently.
# See tests/e2e/conftest.py for the port conflict constraint documentation.
_STARTUP_TIMEOUT_S = 30.0
_POLL_TIMEOUT_S = 180.0  # SDK calls may take longer than CLI
_POLL_INTERVAL_S = 3.0

_FIXTURES_DIR = Path(__file__).parent / "fixtures"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_temp_ss_home_sdk(
    tmp_path: Path,
    *,
    api_key: str | None = None,
    primary_model: str = "claude-haiku-4-5-20251001",
    fallback_model: str = "claude-3-5-haiku-20241022",
) -> Path:
    """Create a minimal temp ~/.secondsight home for SDK E2E tests."""
    ss_home = tmp_path / ".secondsight"
    ss_home.mkdir(parents=True, exist_ok=True)
    (ss_home / "logs").mkdir(exist_ok=True)

    # Resolve API key: use explicit override or real env var
    resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")

    # Write SDK config with the resolved key inline (not ${VAR} reference in
    # tests — we want to test the stored config path, not env-interpolation)
    config_content = (
        '[general]\n'
        'mode = "sdk"\n'
        '\n'
        '[analysis.sdk]\n'
        f'primary_model = "{primary_model}"\n'
        f'fallback_model = "{fallback_model}"\n'
        '\n'
        '[providers.anthropic]\n'
        f'ANTHROPIC_API_KEY = "{resolved_key}"\n'
    )
    (ss_home / "config.toml").write_text(config_content, encoding="utf-8")

    # No state.json needed for SDK mode (precheck for sdk doesn't require it)

    return ss_home


def _wait_for_server(url: str, timeout_s: float) -> bool:
    """Poll GET /health until 200 or timeout. Returns True if ready.

    Delegates to shared helper in tests/e2e/conftest.py.
    """
    return _wait_for_server_shared(url, timeout_s)


def _make_session_start_payload(
    project_id: str = "e2e-proj-sdk",
    session_id: str = "e2e-sess-sdk-001",
    seq: int = 0,
) -> dict[str, Any]:
    """Build a minimal valid session_start hook payload (generic endpoint)."""
    return {
        "project_id": project_id,
        "session_id": session_id,
        "agent": "test",
        "event_id": f"evt-{session_id}-start-{seq}",
        "timestamp": "2026-05-14T12:00:00+00:00",
        "sequence_number": seq,
        "payload": {},
    }


def _make_session_end_payload(
    project_id: str = "e2e-proj-sdk",
    session_id: str = "e2e-sess-sdk-001",
    seq: int = 1,
) -> dict[str, Any]:
    """Build a minimal valid session_end hook payload (generic endpoint).

    Uses the /hook/session_end endpoint with IdentityAdapter format.
    Avoids hook_event_name requirement (Claude Code adapter specific).
    """
    return {
        "project_id": project_id,
        "session_id": session_id,
        "agent": "test",
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


@requires_real_llm
def test_sdk_mode_e2e_dispatch_creates_analysis_row(tmp_path: Path) -> None:
    """DEATH TEST (gated): mode=sdk full ingress → dispatch → storage chain.

    Verifies:
    - secondsight serve starts with sdk_mode_config.toml
    - POST /hook/session_end fires
    - analysis_outputs row has dispatched_via="sdk", status="success"
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        pytest.skip("$ANTHROPIC_API_KEY not set. Cannot run SDK E2E test.")

    ss_home = _make_temp_ss_home_sdk(tmp_path, api_key=api_key)
    db_path = ss_home / "projects" / "e2e-proj-sdk" / "intelligence.db"

    env = {**os.environ, "SECONDSIGHT_HOME": str(ss_home)}

    server_proc = subprocess.Popen(
        [sys.executable, "-m", "secondsight", "serve", "--home", str(ss_home)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    try:
        ready = _wait_for_server(_SERVER_URL, _STARTUP_TIMEOUT_S)
        assert ready, (
            f"Server did not become ready within {_STARTUP_TIMEOUT_S}s. "
            f"Check logs at {ss_home / 'logs'!r}."
        )

        requests.post(
            f"{_SERVER_URL}/hook/session_start",
            json=_make_session_start_payload(project_id="e2e-proj-sdk", session_id="e2e-sess-sdk-001", seq=0),
            timeout=10.0,
        )
        response = requests.post(
            f"{_SERVER_URL}/hook/session_end",
            json=_make_session_end_payload(project_id="e2e-proj-sdk", session_id="e2e-sess-sdk-001", seq=1),
            timeout=10.0,
        )
        assert response.status_code == 200, (
            f"Expected 200 from /hook/session_end. "
            f"Got {response.status_code}: {response.text!r}."
        )

        row = _poll_analysis_outputs_table(
            db_path=db_path,
            session_id="e2e-sess-sdk-001",
            timeout_s=_POLL_TIMEOUT_S,
        )

        assert row is not None, (
            f"No analysis_outputs row found for session_id='e2e-sess-sdk-001' "
            f"within {_POLL_TIMEOUT_S}s."
        )
        assert row.get("dispatched_via") == "sdk", (
            f"Expected dispatched_via='sdk'. Got: {row.get('dispatched_via')!r}."
        )
        assert row.get("status") == "success", (
            f"Expected status='success'. Got: {row.get('status')!r}. "
            f"error_details: {row.get('error_details')!r}."
        )
        primary_model = row.get("primary_model")
        assert primary_model, (
            f"primary_model must be set for SDK dispatch. Got: {primary_model!r}."
        )

    finally:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server_proc.kill()


@requires_real_llm
def test_sdk_mode_invalid_key_produces_failure_row_with_error_details(
    tmp_path: Path,
) -> None:
    """DEATH TEST (gated): invalid API key → status="failure", error_details has upstream error.

    Silent failure path: if invalid key silently produces status="success" or
    error_details is generic/empty, we can't distinguish API errors from
    internal dispatch errors.
    """
    ss_home = _make_temp_ss_home_sdk(tmp_path, api_key="sk-ant-invalid-key-for-e2e-test")
    db_path = ss_home / "projects" / "e2e-proj-sdk-invalid" / "intelligence.db"

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
            json=_make_session_start_payload(project_id="e2e-proj-sdk-invalid", session_id="e2e-sess-sdk-invalid-001", seq=0),
            timeout=10.0,
        )
        requests.post(
            f"{_SERVER_URL}/hook/session_end",
            json=_make_session_end_payload(project_id="e2e-proj-sdk-invalid", session_id="e2e-sess-sdk-invalid-001", seq=1),
            timeout=10.0,
        )

        row = _poll_analysis_outputs_table(
            db_path=db_path,
            session_id="e2e-sess-sdk-invalid-001",
            timeout_s=_POLL_TIMEOUT_S,
        )

        assert row is not None, (
            f"No analysis_outputs row found for invalid-key test within {_POLL_TIMEOUT_S}s. "
            f"Expected a failure row to be written, not silence."
        )
        assert row.get("status") == "failure", (
            f"Expected status='failure' for invalid API key. "
            f"Got: {row.get('status')!r}. "
            f"If status='success', the invalid key was somehow accepted."
        )
        assert row.get("dispatched_via") == "sdk", (
            f"Expected dispatched_via='sdk'. Got: {row.get('dispatched_via')!r}."
        )
        error_details = row.get("error_details")
        assert error_details, (
            f"error_details must be non-empty for failure row. "
            f"Got: {error_details!r}. "
            f"Generic or empty error_details prevents diagnosing API errors."
        )
        # error_details should contain upstream API error context (not just "unknown error")
        error_str = str(error_details).lower()
        assert any(
            token in error_str
            for token in ("auth", "invalid", "key", "401", "403", "unauthorized", "api")
        ), (
            f"error_details must contain upstream API error context (auth/invalid/key/4xx). "
            f"Got: {error_details!r}. "
            f"A generic message here means upstream errors are being swallowed."
        )

    finally:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server_proc.kill()


@requires_real_llm
def test_dc4_sdk_invalid_primary_valid_fallback_uses_fallback(tmp_path: Path) -> None:
    """DEATH TEST (gated): DC4 reroute — invalid primary + valid fallback.

    Decision: primary and fallback MUST use different models.
    Same model for both would not verify the reroute path.

    This test uses:
    - primary_model: an invalid/unreachable model name that causes immediate failure
    - fallback_model: "claude-haiku-4-5-20251001" (valid, should succeed)

    Expected: status="success", fallback_used=True.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        pytest.skip("$ANTHROPIC_API_KEY not set. Cannot run DC4 fallback E2E test.")

    # primary is an invalid model that will cause an API error immediately
    # fallback is the real model — they MUST differ (task requirement)
    ss_home = _make_temp_ss_home_sdk(
        tmp_path,
        api_key=api_key,
        primary_model="claude-nonexistent-model-e2e-dc4-test",  # will fail
        fallback_model="claude-haiku-4-5-20251001",  # will succeed
    )
    db_path = ss_home / "projects" / "e2e-proj-sdk-dc4" / "intelligence.db"

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
            json=_make_session_start_payload(project_id="e2e-proj-sdk-dc4", session_id="e2e-sess-sdk-dc4-001", seq=0),
            timeout=10.0,
        )
        requests.post(
            f"{_SERVER_URL}/hook/session_end",
            json=_make_session_end_payload(project_id="e2e-proj-sdk-dc4", session_id="e2e-sess-sdk-dc4-001", seq=1),
            timeout=10.0,
        )

        row = _poll_analysis_outputs_table(
            db_path=db_path,
            session_id="e2e-sess-sdk-dc4-001",
            timeout_s=_POLL_TIMEOUT_S,
        )

        assert row is not None, (
            f"No analysis_outputs row found for DC4 test within {_POLL_TIMEOUT_S}s."
        )
        assert row.get("status") == "success", (
            f"Expected status='success' when fallback succeeds (DC4). "
            f"Got: {row.get('status')!r}. "
            f"error_details: {row.get('error_details')!r}."
        )
        assert row.get("fallback_used") in (True, 1, "1"), (
            f"Expected fallback_used=True when primary fails and fallback succeeds (DC4). "
            f"Got: {row.get('fallback_used')!r}. "
            f"If fallback_used is False here, the DC4 path was not exercised."
        )

    finally:
        server_proc.terminate()
        try:
            server_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server_proc.kill()
