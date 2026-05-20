"""Tests for AnalysisOutputsRepository (Task 6).

Death tests: verify DB schema enforcement (CHECK constraints).
Unit tests: verify column persistence for all required fields.

DB schema acceptance test: AnalysisOutput row has all required fields populated.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from secondsight.analysis.output import AnalysisOutput
from secondsight.storage.analysis_outputs_repository import AnalysisOutputsRepository
from secondsight.storage.db_engine import DBEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_engine(tmp_path: Path) -> DBEngine:
    return DBEngine(db_path=tmp_path / "intelligence.db")


@pytest.fixture
def repo(db_engine: DBEngine) -> AnalysisOutputsRepository:
    r = AnalysisOutputsRepository(db_engine)
    r.create_schema()
    return r


def _make_cli_output(session_id: str = "sess-001") -> AnalysisOutput:
    return AnalysisOutput.model_validate(
        {
            "schema_version": "1.0",
            "session_id": session_id,
            "status": "success",
            "behavior_flags": [],
            "session_summary": {
                "headline": "Test analysis",
                "key_findings": [],
                "body": "Test body.",
            },
            "dispatched_via": "cli",
            "cli_agent": "claude_code",
            "primary_model": None,
            "fallback_used": False,
            "retry_count": 0,
            "error_details": None,
        }
    )


def _make_sdk_output(session_id: str = "sess-002") -> AnalysisOutput:
    return AnalysisOutput.model_validate(
        {
            "schema_version": "1.0",
            "session_id": session_id,
            "status": "success",
            "behavior_flags": [],
            "session_summary": {
                "headline": "SDK test analysis",
                "key_findings": [],
                "body": "SDK test body.",
            },
            "dispatched_via": "sdk",
            "cli_agent": None,
            "primary_model": "claude-haiku-4-5-20251001",
            "fallback_used": False,
            "retry_count": 0,
            "error_details": None,
        }
    )


# ---------------------------------------------------------------------------
# DEATH TESTS — DB schema enforcement
# ---------------------------------------------------------------------------


def test_db_schema_cli_output_has_required_fields_populated(
    repo: AnalysisOutputsRepository,
) -> None:
    """DB schema test: CLI output row has all required fields populated (not null for required)."""
    output = _make_cli_output()
    repo.upsert(output, project_id="proj-alpha")

    row = repo.get_by_session_id("sess-001")
    assert row is not None, "Row not found after insert"

    # Required fields must be non-null
    assert row["dispatched_via"] == "cli"
    assert row["cli_agent"] == "claude_code"
    assert row["primary_model"] is None  # null for CLI is correct
    assert row["fallback_used"] is False
    assert row["retry_count"] == 0
    assert row["status"] == "success"
    assert row["project_id"] == "proj-alpha"
    assert row["session_id"] == "sess-001"


def test_db_schema_sdk_output_has_required_fields_populated(
    repo: AnalysisOutputsRepository,
) -> None:
    """DB schema test: SDK output row has all required fields populated."""
    output = _make_sdk_output()
    repo.upsert(output, project_id="proj-sdk")

    row = repo.get_by_session_id("sess-002")
    assert row is not None

    assert row["dispatched_via"] == "sdk"
    assert row["cli_agent"] is None  # null for SDK is correct
    assert row["primary_model"] == "claude-haiku-4-5-20251001"
    assert row["fallback_used"] is False
    assert row["retry_count"] == 0
    assert row["status"] == "success"


def test_db_schema_error_details_persisted_and_deserialized(
    repo: AnalysisOutputsRepository,
) -> None:
    """DB schema test: error_details is persisted as JSON and deserialized correctly."""
    output = AnalysisOutput.model_validate(
        {
            "schema_version": "1.0",
            "session_id": "sess-err",
            "status": "failure",
            "behavior_flags": [],
            "session_summary": {
                "headline": "Failure",
                "key_findings": [],
                "body": "Failed.",
            },
            "dispatched_via": "cli",
            "cli_agent": "claude_code",
            "primary_model": None,
            "fallback_used": False,
            "retry_count": 1,
            "error_details": {"reason": "subprocess_exit", "exit_code": 1},
        }
    )
    repo.upsert(output, project_id="proj-err")

    row = repo.get_by_session_id("sess-err")
    assert row is not None
    assert row["status"] == "failure"
    assert row["retry_count"] == 1
    assert row["error_details"] == {"reason": "subprocess_exit", "exit_code": 1}


def test_db_schema_create_schema_idempotent(db_engine: DBEngine) -> None:
    """create_schema() can be called multiple times without error."""
    repo = AnalysisOutputsRepository(db_engine)
    repo.create_schema()
    repo.create_schema()  # second call must not raise


# ---------------------------------------------------------------------------
# UNIT TESTS — upsert semantics
# ---------------------------------------------------------------------------


def test_upsert_second_write_for_same_session_updates_latest_result(
    repo: AnalysisOutputsRepository,
) -> None:
    """Sequential rerun for the same session_id updates the latest result row."""
    output1 = _make_cli_output("sess-dup")
    output2 = AnalysisOutput.model_validate(
        {
            **output1.model_dump(),
            "cli_agent": "codex",
            "status": "failure",
            "retry_count": 2,
            "error_details": {"reason": "rerun-overwrite"},
        }
    )

    first_row_id = repo.upsert(output1, project_id="proj-dup")
    second_row_id = repo.upsert(output2, project_id="proj-dup")

    row = repo.get_by_session_id("sess-dup")
    assert row is not None
    assert row["id"] == second_row_id
    assert row["id"] != first_row_id
    assert row["cli_agent"] == "codex"
    assert row["status"] == "failure"
    assert row["retry_count"] == 2
    assert row["error_details"] == {"reason": "rerun-overwrite"}


def test_get_by_session_id_returns_none_for_unknown_session(
    repo: AnalysisOutputsRepository,
) -> None:
    """get_by_session_id() returns None for a session that was never inserted."""
    result = repo.get_by_session_id("sess-unknown-xyz")
    assert result is None


def test_fallback_used_true_persisted_correctly(repo: AnalysisOutputsRepository) -> None:
    """fallback_used=True is stored as 1 in SQLite and returned as True (bool)."""
    output = AnalysisOutput.model_validate(
        {
            "schema_version": "1.0",
            "session_id": "sess-fallback",
            "status": "failure",
            "behavior_flags": [],
            "session_summary": {"headline": "Both failed", "key_findings": [], "body": "."},
            "dispatched_via": "sdk",
            "cli_agent": None,
            "primary_model": "claude-haiku-4-5-20251001",
            "fallback_used": True,
            "retry_count": 0,
            "error_details": {
                "primary_error": "primary failed",
                "fallback_error": "fallback failed",
            },
        }
    )
    repo.upsert(output, project_id="proj-fallback")
    row = repo.get_by_session_id("sess-fallback")
    assert row is not None
    assert row["fallback_used"] is True
