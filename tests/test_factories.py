"""Shared test factory functions for SDK + CLI tests (GUR-103 fix-loop #13).

These factories were previously duplicated in tests/sdk/test_trigger.py and
tests/cli/test_analyze.py with slightly different module-level default constants.
Moving them here eliminates the duplication.

NOTE: The functions below require all required arguments to be passed explicitly.
The original test modules had module-level constants for project_id/session_id that
differed between the two files. Callers must provide those constants themselves.
"""

from __future__ import annotations

from datetime import datetime, timezone

from secondsight.event import Event, EventType
from secondsight.storage.analysis_runs_repository import AnalysisRunsRepository


def make_event(
    seq: int,
    *,
    session_id: str,
    project_id: str,
    event_type: EventType = EventType.TOOL_USE_START,
    timestamp: datetime | None = None,
) -> Event:
    """Construct a minimal valid Event for trigger/analyze tests.

    Args:
        seq: Sequence number (must be unique within a session).
        session_id: The session this event belongs to.
        project_id: The project this event belongs to.
        event_type: Event type (defaults to TOOL_USE_START).
        timestamp: Timestamp (defaults to a fixed UTC datetime for determinism).
    """
    ts = timestamp or datetime(2026, 5, 8, 10, 0, 0, tzinfo=timezone.utc)
    return Event(
        id=f"evt-{session_id}-{seq}",
        session_id=session_id,
        project_id=project_id,
        event_type=event_type,
        timestamp=ts,
        sequence_number=seq,
        segment_index=0,
    )


def seed_terminal_run(
    runs_repo: AnalysisRunsRepository,
    *,
    session_id: str,
    project_id: str,
    stage: str = "summary_written",
) -> str:
    """Insert a terminal analysis_run row via repo methods. Returns run_id.

    Args:
        runs_repo: The AnalysisRunsRepository to write to.
        session_id: The session_id for the run.
        project_id: The project_id for the run.
        stage: Terminal stage to advance to (default: "summary_written").

    Returns:
        The run_id of the inserted run.
    """
    run_id = runs_repo.start_run(project_id, session_id)
    runs_repo.advance_stage(run_id, stage)
    return run_id


__all__ = ["make_event", "seed_terminal_run"]
