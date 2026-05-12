"""Death tests and unit tests for POST /hook/{event_type} endpoint (P1-5, Task-3).

IdentityAdapter / AdapterRegistry behavioural unit tests live in
`tests/adapters/test_identity.py` after the task-3 migration of phase1-adapters.
This file now focuses on the route-level concerns (HTTP status codes, fire-
and-forget latency, path-safety validation) that the adapter layer cannot
exercise on its own.

Death tests (must go RED before production code):
  DT-2: Unknown event_type silently routed — must return 422 (enum closed).
  DT-4: Path traversal in event_type — must return 404 or 422, never 200.
  DT-5: Missing project_id silently fills default — must return 422.
  DT-6: Sequence_number reuse — second call returns 200 but DB shows integrity
         error logged (ON CONFLICT on sequence_number raises; done_callback logs).
  DT-8: Duplicate event_id idempotency — same id twice → 200 both; one DB row.

Unit tests:
  - Happy path: valid envelope → 200 {"status": "ok"}; after settle, DB has row.
  - GET /health works after hooks router mounted.
  - Missing agent field → 422.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_valid_payload(
    *,
    project_id: str = "proj-hook-test",
    session_id: str = "sess-hook-001",
    agent: str = "test",
    event_id: str = "evt-hook-001",
    seq: int = 0,
    extra_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a minimal valid hook envelope payload."""
    payload: dict[str, Any] = {
        "project_id": project_id,
        "session_id": session_id,
        "agent": agent,
        "event_id": event_id,
        "timestamp": datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc).isoformat(),
        "sequence_number": seq,
        "payload": {},
    }
    if extra_fields:
        payload.update(extra_fields)
    return payload


def _make_app(tmp_secondsight_home: Path):  # type: ignore[return]
    """Create a fresh app with a real registry for integration tests."""
    from secondsight.api.server import create_app

    return create_app(secondsight_home=tmp_secondsight_home)


# ---------------------------------------------------------------------------
# DT-2: Unknown event_type — must return 422 (enum is closed)
# ---------------------------------------------------------------------------


def test_death_unknown_event_type_returns_422(tmp_secondsight_home: Path) -> None:
    """DEATH TEST: POST /hook/totally_invented_type must return 422.

    The route handler MUST validate event_type against the EventType enum.
    If it silently routes unknowns to a default normalizer, this test fails.
    """
    app = _make_app(tmp_secondsight_home)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/hook/totally_invented_type",
            json=_make_valid_payload(),
        )
    assert response.status_code == 422, (
        f"DEATH: Expected 422 for unknown event_type, got {response.status_code}. "
        f"Body: {response.text}"
    )


# ---------------------------------------------------------------------------
# DT-4: Path traversal in event_type — must never return 200
# ---------------------------------------------------------------------------


def test_death_path_traversal_in_event_type_rejected(tmp_secondsight_home: Path) -> None:
    """DEATH TEST: Path traversal in event_type must return 404 or 422, never 200.

    POST /hook/..%2F..%2Fetc%2Fpasswd — if decoded and treated as a string,
    the enum validation must reject it. If kept as a literal URL, FastAPI
    normalizes it and produces 404.
    """
    app = _make_app(tmp_secondsight_home)
    with TestClient(app, raise_server_exceptions=False) as client:
        # Try the percent-encoded traversal
        response = client.post(
            "/hook/..%2F..%2Fetc%2Fpasswd",
            json=_make_valid_payload(),
        )
    assert response.status_code in (404, 422), (
        f"DEATH: Expected 404 or 422 for path traversal, got {response.status_code}. "
        f"Body: {response.text}"
    )
    assert response.status_code != 200, "DEATH: Path traversal must never return 200."


def test_death_literal_dotdot_in_event_type_rejected(tmp_secondsight_home: Path) -> None:
    """DEATH TEST: Literal '..' in event_type must return 422.

    If the path component arrives as the literal string '..', the enum
    validator must reject it as not a valid EventType value.
    """
    app = _make_app(tmp_secondsight_home)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/hook/..",
            json=_make_valid_payload(),
        )
    assert response.status_code in (404, 405, 422), (
        f"DEATH: Expected 404/405/422 for literal '..', got {response.status_code}. "
        "405 is valid when Starlette normalizes /hook/.. → / (dashboard GET-only route)."
    )


# ---------------------------------------------------------------------------
# DT-5: Missing project_id silently fills default — must return 422
# ---------------------------------------------------------------------------


def test_death_missing_project_id_returns_422(tmp_secondsight_home: Path) -> None:
    """DEATH TEST: POST without project_id must return 422 with field-level error.

    The tracker/registry MUST never see a None or empty project_id.
    """
    app = _make_app(tmp_secondsight_home)
    payload = _make_valid_payload()
    del payload["project_id"]

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/hook/session_start", json=payload)

    assert response.status_code == 422, (
        f"DEATH: Expected 422 for missing project_id, got {response.status_code}"
    )
    data = response.json()
    # Pydantic v2 validation error shape: {"detail": [{"loc": [...], "msg": ..., "type": ...}]}
    assert "detail" in data
    loc_paths = [".".join(str(x) for x in err.get("loc", [])) for err in data["detail"]]
    assert any("project_id" in loc for loc in loc_paths), (
        f"DEATH: 422 error must reference 'project_id' field. Got locs: {loc_paths}"
    )


def test_death_empty_project_id_returns_422(tmp_secondsight_home: Path) -> None:
    """DEATH TEST: Empty string project_id must return 422 (min_length=1)."""
    app = _make_app(tmp_secondsight_home)
    payload = _make_valid_payload()
    payload["project_id"] = ""

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/hook/session_start", json=payload)

    assert response.status_code == 422, (
        f"DEATH: Expected 422 for empty project_id, got {response.status_code}"
    )


# ---------------------------------------------------------------------------
# DT-6: Sequence_number reuse logs integrity error via done_callback
# ---------------------------------------------------------------------------


def test_death_sequence_number_reuse_logged_via_pipeline_warning(
    tmp_secondsight_home: Path,
    propagate_loguru_to_caplog: pytest.LogCaptureFixture,
) -> None:
    """DEATH TEST: Duplicate (session_id, sequence_number) must be logged.

    The events table has UNIQUE(session_id, sequence_number). If two events
    share the same sequence_number but different event_ids, the second INSERT
    raises IntegrityError. The pipeline catches this in _record_db_failure and
    logs it at WARNING level via loguru (NOT through the done_callback — the
    pipeline handles the error internally and the task completes without raising).

    Both HTTP calls must return 200 (fire-and-forget). The error surfaces only
    in the log via ObservationPipeline._record_db_failure → loguru.warning.
    """
    import logging

    caplog = propagate_loguru_to_caplog
    app = _make_app(tmp_secondsight_home)

    with caplog.at_level(logging.WARNING, logger="loguru"):
        with TestClient(app, raise_server_exceptions=False) as client:
            # First event
            r1 = client.post(
                "/hook/session_start",
                json=_make_valid_payload(
                    event_id="evt-seq-001",
                    seq=0,
                    session_id="sess-seqdup",
                ),
            )
            assert r1.status_code == 200, f"First request failed: {r1.text}"

            # Let the first ingest settle (it needs to hit the DB before the
            # second one tries the same sequence_number).
            time.sleep(0.2)

            # Second event — same sequence_number, different event_id.
            # This is a correctness bug (adapter reused sequence_number).
            r2 = client.post(
                "/hook/session_start",
                json=_make_valid_payload(
                    project_id="proj-hook-test",
                    session_id="sess-seqdup",
                    event_id="evt-seq-002-dup",
                    seq=0,  # SAME sequence_number
                ),
            )
            assert r2.status_code == 200, (
                f"DEATH: Second request must return 200 (fire-and-forget). Got: {r2.text}"
            )

            # Wait for done_callback to fire
            time.sleep(0.2)

    # Verify the integrity error was logged.
    # The pipeline._record_db_failure logs at WARNING level (not ERROR) because
    # the UNIQUE(session_id, sequence_number) constraint raises IntegrityError,
    # which is caught by ObservationPipeline and recorded to sync_log.
    # The done_callback sees no exception (pipeline handles it internally).
    # So we check for WARNING+ log mentioning the violation.
    all_msgs = " ".join(r.getMessage() for r in caplog.records)
    has_warning_or_error = any(r.levelno >= logging.WARNING for r in caplog.records)
    # The error message should mention the violation — either "IntegrityError"
    # or the event_id, or "UNIQUE"
    has_relevant_content = (
        "UNIQUE" in all_msgs
        or "IntegrityError" in all_msgs
        or "sequence" in all_msgs.lower()
        or "evt-seq-002-dup" in all_msgs
    )
    assert has_warning_or_error and has_relevant_content, (
        "DEATH: sequence_number reuse must be logged (WARNING+) via pipeline or done_callback. "
        f"Got records: {[(r.levelno, r.getMessage()) for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# DT-8: Duplicate event_id — idempotency; one DB row; no error log
# ---------------------------------------------------------------------------


def test_death_duplicate_event_id_idempotent(tmp_secondsight_home: Path) -> None:
    """DEATH TEST: Same event_id posted twice → both 200; exactly one DB row.

    ON CONFLICT DO NOTHING is the correct idempotency behavior at the storage
    layer. This test verifies:
    1. Both requests return 200.
    2. The DB has exactly one row for the event_id.
    3. No spurious error log appears (ON CONFLICT DO NOTHING is silent by design).
    """
    app = _make_app(tmp_secondsight_home)

    with TestClient(app, raise_server_exceptions=False) as client:
        payload = _make_valid_payload(event_id="evt-dup-iddmp", seq=0)
        r1 = client.post("/hook/session_start", json=payload)
        assert r1.status_code == 200

        time.sleep(0.15)  # Let first ingest settle

        # Post exact same event again
        r2 = client.post("/hook/session_start", json=payload)
        assert r2.status_code == 200, f"Second post must return 200: {r2.text}"

        time.sleep(0.15)  # Let second ingest settle

    # Verify DB has exactly one row — inspect the DB file directly.
    db_path = tmp_secondsight_home / "projects" / "proj-hook-test" / "intelligence.db"
    assert db_path.exists(), f"DB not found at {db_path}"
    row_count = _count_events_by_id_from_db(db_path, "evt-dup-iddmp")
    assert row_count == 1, f"DEATH: Expected 1 row for duplicate event_id, got {row_count}"


def _count_events_by_id_from_db(db_path: Path, event_id: str) -> int:
    """Count rows in events table with the given id, by opening the DB directly."""
    import sqlite3

    with sqlite3.connect(str(db_path)) as conn:
        cursor = conn.execute("SELECT COUNT(*) FROM events WHERE id = ?", (event_id,))
        return cursor.fetchone()[0]


# ---------------------------------------------------------------------------
# Unit: Happy path integration test
# ---------------------------------------------------------------------------


def test_unit_happy_path_valid_envelope_returns_ok(
    tmp_secondsight_home: Path,
) -> None:
    """Unit: POST valid envelope → 200 {"status": "ok"}.

    After settling, the DB has one row and the raw_trace_store has one file.
    Uses direct DB/filesystem inspection to avoid registry lifetime issues.
    """
    app = _make_app(tmp_secondsight_home)

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/hook/session_start",
            json=_make_valid_payload(
                project_id="proj-happy",
                session_id="sess-happy-001",
                event_id="evt-happy-001",
                seq=0,
            ),
        )
        assert response.status_code == 200, (
            f"Expected 200, got {response.status_code}: {response.text}"
        )
        assert response.json() == {"status": "ok"}

        # Give the fire-and-forget task time to complete
        time.sleep(0.3)

    # Verify storage — inspect DB and filesystem directly (registry is closed).
    db_path = tmp_secondsight_home / "projects" / "proj-happy" / "intelligence.db"
    assert db_path.exists(), f"DB not found at {db_path}"
    row_count = _count_events_by_id_from_db(db_path, "evt-happy-001")
    assert row_count == 1, f"Expected 1 DB row, got {row_count}"

    # Verify raw trace — RawTraceStore stores files at sessions/{sid}/events/*.json
    project_dir = tmp_secondsight_home / "projects" / "proj-happy"
    raw_files = list(project_dir.glob("sessions/**/*.json"))
    assert len(raw_files) >= 1, f"Expected at least 1 raw trace file, got {raw_files}"


# Behavioural unit tests for IdentityAdapter / AdapterRegistry now live in
# `tests/adapters/test_identity.py` (post task-3 migration of phase1-adapters).
# AC-3 parity: every `IdentityNormalizer` test from this file is preserved
# there with the rename `IdentityNormalizer` → `IdentityAdapter` and the new
# `supported_event_types()` / DT-6 alignment assertions added.


# ---------------------------------------------------------------------------
# Unit: GET /health works after hooks router is mounted
# ---------------------------------------------------------------------------


def test_unit_health_works_after_hooks_router_mounted(tmp_secondsight_home: Path) -> None:
    """Unit: GET /health continues to work after hooks router is mounted."""
    app = _make_app(tmp_secondsight_home)
    with TestClient(app) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["liveness"] == "alive"


# ---------------------------------------------------------------------------
# Unit: Missing agent field → 422
# ---------------------------------------------------------------------------


def test_unit_missing_agent_field_returns_422(tmp_secondsight_home: Path) -> None:
    """Unit: Request without 'agent' field must fail with 422."""
    app = _make_app(tmp_secondsight_home)
    payload = _make_valid_payload()
    del payload["agent"]

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/hook/session_start", json=payload)

    assert response.status_code == 422, (
        f"Expected 422 for missing agent, got {response.status_code}: {response.text}"
    )
    data = response.json()
    assert "detail" in data
    loc_paths = [".".join(str(x) for x in err.get("loc", [])) for err in data["detail"]]
    assert any("agent" in loc for loc in loc_paths), (
        f"422 error must reference 'agent' field. Got locs: {loc_paths}"
    )


# ---------------------------------------------------------------------------
# Unit: _is_safe_id rejects control characters (I6 hardening)
# ---------------------------------------------------------------------------


def test_unit_is_safe_id_rejects_control_characters() -> None:
    """Unit: _is_safe_id must reject ASCII control chars and whitespace.

    I6 hardening: the comment previously claimed control chars are rejected
    but the implementation only rejected /, \\, null, and pure-dot sequences.
    After the fix, control chars (\x00-\x1f, \x7f) and whitespace (\t, \n, \r)
    are also rejected.
    """
    from secondsight.api.hooks import _is_safe_id

    # Must be rejected: control characters
    assert not _is_safe_id("\t"), "Tab must be rejected"
    assert not _is_safe_id("\n"), "Newline must be rejected"
    assert not _is_safe_id("\r"), "Carriage return must be rejected"
    assert not _is_safe_id("\x00"), "Null byte must be rejected"
    assert not _is_safe_id("\x01"), "SOH control char must be rejected"
    assert not _is_safe_id("\x1f"), "US control char must be rejected"
    assert not _is_safe_id("\x7f"), "DEL control char must be rejected"
    assert not _is_safe_id("proj\x00name"), "Embedded null must be rejected"
    assert not _is_safe_id("proj\nname"), "Embedded newline must be rejected"
    assert not _is_safe_id("proj\tname"), "Embedded tab must be rejected"

    # Must still be rejected: path traversal
    assert not _is_safe_id("/etc/passwd"), "Slash must be rejected"
    assert not _is_safe_id(".."), "Pure dots must be rejected"
    assert not _is_safe_id("."), "Single dot must be rejected"
    assert not _is_safe_id(""), "Empty string must be rejected"

    # Must still be allowed: valid IDs
    assert _is_safe_id("proj-alpha"), "Hyphen must be allowed"
    assert _is_safe_id("com.company.project"), "Dots in middle must be allowed"
    assert _is_safe_id("proj_001"), "Underscore must be allowed"
    assert _is_safe_id("A"), "Single char must be allowed"


# IdentityAdapter.normalize() unit tests live in tests/adapters/test_identity.py
# (post task-3 migration of phase1-adapters).
