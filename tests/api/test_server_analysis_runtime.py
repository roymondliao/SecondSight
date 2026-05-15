"""Server analysis runtime wiring tests.

Death tests first:
- DT-SAR-1: `session_end` through the server path must create an analysis_outputs row.
- DT-SAR-2: stale-session sweep must dispatch timeout recovery (analysis_outputs row created).
- DT-SAR-3: event path + timeout path must not create duplicate analysis_outputs rows.
- DT-SAR-4: terminal session sweep must log at DEBUG, not INFO.

Task 6 review note: DT-SAR-1/2/3 were updated to check analysis_outputs table
(new production path via ModeAwareDispatch) instead of analysis_runs (legacy SDK
orchestrator path). The dispatch path now is:
    Trigger.dispatch() → ModeAwareDispatch.dispatch() → AnalysisOutputsRepository

The tests mock ModeAwareDispatch.dispatch() to return a noop AnalysisOutput
without spawning CLI processes or calling LLM APIs.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from _pytest.logging import LogCaptureFixture
from fastapi.testclient import TestClient

from secondsight.analysis.output import AnalysisOutput
from secondsight.analysis.prompts.aggregate import AggregateOutput
from secondsight.analysis.prompts.summary import SummaryOutput
from secondsight.analysis.schemas import SegmentAnalysis
from secondsight.api.registry import ProjectRegistry
from secondsight.api.server import _ServerSweepCoordinator, create_app
from secondsight.event import Event, EventType

_NOW = datetime(2026, 5, 13, 8, 0, 0, tzinfo=timezone.utc)


class _NoopAnalysisAgent:
    """Fast fake agent for server-side wiring tests."""

    def __init__(self, *, delay_s: float = 0.0) -> None:
        self._delay_s = delay_s

    async def analyze_segments(self, prompts: list[str]) -> list[SegmentAnalysis]:
        if self._delay_s > 0:
            await asyncio.sleep(self._delay_s)
        return [
            SegmentAnalysis(
                segment_summary="No notable issues.",
                flags=[],
                total_events=1,
                flagged_events=0,
            )
            for _ in prompts
        ]

    async def aggregate_flag_type(self, prompt: str) -> AggregateOutput:
        if self._delay_s > 0:
            await asyncio.sleep(self._delay_s)
        return AggregateOutput(patterns=[])

    async def summarize_session(self, prompt: str) -> SummaryOutput:
        if self._delay_s > 0:
            await asyncio.sleep(self._delay_s)
        return SummaryOutput(
            headline="Session analyzed",
            key_findings=[],
            body="No notable issues.",
        )


def _make_valid_payload(
    *,
    project_id: str,
    session_id: str,
    event_id: str,
    seq: int,
    event_ts: datetime,
) -> dict[str, object]:
    return {
        "project_id": project_id,
        "session_id": session_id,
        "agent": "test",
        "event_id": event_id,
        "timestamp": event_ts.isoformat(),
        "sequence_number": seq,
        "payload": {},
    }


def _count_analysis_runs(db_path: Path, session_id: str) -> int:
    with sqlite3.connect(str(db_path)) as conn:
        cursor = conn.execute(
            "SELECT COUNT(*) FROM analysis_runs WHERE session_id = ?",
            (session_id,),
        )
        return int(cursor.fetchone()[0])


def _count_analysis_outputs(db_path: Path, session_id: str) -> int:
    """Count rows in analysis_outputs table (new mode-aware dispatch path).

    Returns 0 if the table does not exist yet (before first dispatch).
    """
    with sqlite3.connect(str(db_path)) as conn:
        # analysis_outputs table is created on first use — check if it exists
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='analysis_outputs'"
        )
        if cursor.fetchone() is None:
            return 0
        cursor = conn.execute(
            "SELECT COUNT(*) FROM analysis_outputs WHERE session_id = ?",
            (session_id,),
        )
        return int(cursor.fetchone()[0])


def _wait_for_count(
    *,
    db_path: Path,
    session_id: str,
    expected: int,
    timeout_s: float = 2.0,
) -> None:
    """Wait for expected number of analysis_runs rows (legacy SDK orchestrator path)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if db_path.exists() and _count_analysis_runs(db_path, session_id) == expected:
            return
        time.sleep(0.05)
    actual = _count_analysis_runs(db_path, session_id) if db_path.exists() else -1
    raise AssertionError(
        f"Expected {expected} analysis_runs rows for session_id={session_id!r}, got {actual}."
    )


def _wait_for_outputs_count(
    *,
    db_path: Path,
    session_id: str,
    expected: int,
    timeout_s: float = 2.0,
) -> None:
    """Wait for expected number of analysis_outputs rows (new mode-aware dispatch path)."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if db_path.exists() and _count_analysis_outputs(db_path, session_id) == expected:
            return
        time.sleep(0.05)
    actual = _count_analysis_outputs(db_path, session_id) if db_path.exists() else -1
    raise AssertionError(
        f"Expected {expected} analysis_outputs rows for session_id={session_id!r}, got {actual}. "
        f"The ModeAwareDispatch dispatch path must write to analysis_outputs after dispatch."
    )


def _make_noop_output(session_id: str) -> AnalysisOutput:
    """Return a noop AnalysisOutput for use in mock dispatchers."""
    return AnalysisOutput.model_validate(
        {
            "schema_version": "1.0",
            "session_id": session_id,
            "status": "success",
            "behavior_flags": [],
            "session_summary": {
                "headline": "No issues",
                "key_findings": [],
                "body": "Noop analysis.",
            },
            "dispatched_via": "sdk",
            "cli_agent": None,
            "primary_model": "claude-haiku-4-5-20251001",
            "fallback_used": False,
            "retry_count": 0,
            "error_details": None,
        }
    )


def _make_event(
    *,
    project_id: str,
    session_id: str,
    event_id: str,
    seq: int,
    event_type: EventType,
    event_ts: datetime,
) -> Event:
    return Event(
        id=event_id,
        session_id=session_id,
        project_id=project_id,
        event_type=event_type,
        timestamp=event_ts,
        sequence_number=seq,
        segment_index=0,
    )


@pytest.mark.asyncio
async def test_dt_sar_1_session_end_creates_dashboard_visible_analysis(
    tmp_secondsight_home: Path,
) -> None:
    """DEATH TEST: persisted `session_end` must create dashboard-visible analysis artifacts.

    After CRITICAL FIX 1: dispatch goes through Trigger → ModeAwareDispatch →
    AnalysisOutputsRepository. The check is now against analysis_outputs table,
    not analysis_runs (which was the legacy SDK orchestrator path).

    SDKAnalysisDispatcher.dispatch() is mocked to avoid real LLM API calls.
    ModeAwareDispatch.dispatch() runs fully (including repository write).
    """
    registry = ProjectRegistry(secondsight_home=tmp_secondsight_home)
    db_path = tmp_secondsight_home / "projects" / "proj-runtime" / "intelligence.db"
    session_id = "sess-runtime-001"

    with (
        patch(
            "secondsight.analysis.runtime._build_analysis_agent",
            return_value=_NoopAnalysisAgent(),
        ),
        patch(
            "secondsight.analysis.sdk_dispatcher.SDKAnalysisDispatcher.dispatch",
            new_callable=AsyncMock,
        ) as mock_sdk_dispatch,
    ):
        mock_sdk_dispatch.side_effect = lambda s_id, *args, **kwargs: _make_noop_output(s_id)
        app = create_app(secondsight_home=tmp_secondsight_home, registry=registry)
        with TestClient(app, raise_server_exceptions=False) as client:
            start = client.post(
                "/hook/session_start",
                json=_make_valid_payload(
                    project_id="proj-runtime",
                    session_id=session_id,
                    event_id="evt-runtime-start",
                    seq=0,
                    event_ts=_NOW,
                ),
            )
            assert start.status_code == 200, start.text

            end = client.post(
                "/hook/session_end",
                json=_make_valid_payload(
                    project_id="proj-runtime",
                    session_id=session_id,
                    event_id="evt-runtime-end",
                    seq=1,
                    event_ts=_NOW + timedelta(seconds=1),
                ),
            )
            assert end.status_code == 200, end.text

            # Wait for analysis_outputs row to appear (production contract)
            _wait_for_outputs_count(
                db_path=db_path,
                session_id=session_id,
                expected=1,
            )

            summary = client.get(
                "/api/analysis/summary",
                params={"project_id": "proj-runtime"},
            )
            assert summary.status_code == 200, summary.text
            assert summary.json()["analyzed_session_count"] == 1

            sessions = client.get(
                "/api/analysis/sessions",
                params={"project_id": "proj-runtime"},
            )
            assert sessions.status_code == 200, sessions.text
            assert len(sessions.json()["items"]) == 1


@pytest.mark.asyncio
async def test_dt_sar_2_sweeper_dispatches_timeout_recovery(
    tmp_secondsight_home: Path,
) -> None:
    """DEATH TEST: stale-session sweep must dispatch recovery, not just warn.

    After CRITICAL FIX 1: checks analysis_outputs (new dispatch path) not
    analysis_runs (legacy SDK path). SDKAnalysisDispatcher.dispatch() is mocked.
    """
    registry = ProjectRegistry(secondsight_home=tmp_secondsight_home)
    db_path = tmp_secondsight_home / "projects" / "proj-sweeper" / "intelligence.db"
    session_id = "sess-stale-001"

    with (
        patch(
            "secondsight.analysis.runtime._build_analysis_agent",
            return_value=_NoopAnalysisAgent(),
        ),
        patch(
            "secondsight.analysis.sdk_dispatcher.SDKAnalysisDispatcher.dispatch",
            new_callable=AsyncMock,
        ) as mock_sdk_dispatch_2,
    ):
        mock_sdk_dispatch_2.side_effect = lambda s_id, *args, **kwargs: _make_noop_output(s_id)
        resources = await registry.get("proj-sweeper")
        stale_ts = _NOW - timedelta(minutes=45)
        resources.events_repository.insert(
            _make_event(
                project_id="proj-sweeper",
                session_id=session_id,
                event_id="evt-stale-start",
                seq=0,
                event_type=EventType.SESSION_START,
                event_ts=stale_ts,
            )
        )
        resources.events_repository.insert(
            _make_event(
                project_id="proj-sweeper",
                session_id=session_id,
                event_id="evt-stale-end",
                seq=1,
                event_type=EventType.SESSION_END,
                event_ts=stale_ts + timedelta(seconds=5),
            )
        )

        coordinator = _ServerSweepCoordinator(
            registry=registry,
            secondsight_home=tmp_secondsight_home,
        )
        cutoff = _NOW - timedelta(minutes=30)
        await coordinator._sweep_all_projects(cutoff)

        deadline = time.time() + 2.0
        while time.time() < deadline:
            if db_path.exists() and _count_analysis_outputs(db_path, session_id) == 1:
                break
            await asyncio.sleep(0.05)
        else:
            count = _count_analysis_outputs(db_path, session_id) if db_path.exists() else -1
            raise AssertionError(
                f"Sweeper did not dispatch timeout recovery. "
                f"analysis_outputs count={count} (expected 1). "
                f"Trigger must route through ModeAwareDispatch (CRITICAL FIX 1)."
            )


@pytest.mark.asyncio
async def test_dt_sar_3_event_and_timeout_share_trigger_without_duplicate_runs(
    tmp_secondsight_home: Path,
) -> None:
    """DEATH TEST: event path + timeout path must not create duplicate analysis_outputs rows.

    After CRITICAL FIX 1: checks analysis_outputs (new dispatch path).
    SDKAnalysisDispatcher.dispatch() is mocked with delay to increase race probability.
    DC10 deduplication: Trigger's LockRegistry (outer guard) + ModeAwareDispatch's
    asyncio.Lock (inner guard) + DB UNIQUE constraint (safety net).
    """
    registry = ProjectRegistry(secondsight_home=tmp_secondsight_home)
    db_path = tmp_secondsight_home / "projects" / "proj-race" / "intelligence.db"
    session_id = "sess-race-001"

    with (
        patch(
            "secondsight.analysis.runtime._build_analysis_agent",
            return_value=_NoopAnalysisAgent(delay_s=0.05),
        ),
        patch(
            "secondsight.analysis.sdk_dispatcher.SDKAnalysisDispatcher.dispatch",
            new_callable=AsyncMock,
        ) as mock_sdk_dispatch_3,
    ):

        async def _slow_noop(s_id, *args, **kwargs):
            await asyncio.sleep(0.05)
            return _make_noop_output(s_id)

        mock_sdk_dispatch_3.side_effect = _slow_noop
        resources = await registry.get("proj-race")
        assert resources.analysis_runtime is not None

        stale_ts = _NOW - timedelta(minutes=45)
        resources.events_repository.insert(
            _make_event(
                project_id="proj-race",
                session_id=session_id,
                event_id="evt-race-start",
                seq=0,
                event_type=EventType.SESSION_START,
                event_ts=stale_ts,
            )
        )
        resources.events_repository.insert(
            _make_event(
                project_id="proj-race",
                session_id=session_id,
                event_id="evt-race-end",
                seq=1,
                event_type=EventType.SESSION_END,
                event_ts=stale_ts + timedelta(seconds=2),
            )
        )

        coordinator = _ServerSweepCoordinator(
            registry=registry,
            secondsight_home=tmp_secondsight_home,
        )
        cutoff = _NOW - timedelta(minutes=30)

        await asyncio.gather(
            resources.analysis_runtime.trigger.dispatch(
                "proj-race",
                session_id,
                source="event",
            ),
            coordinator._sweep_all_projects(cutoff),
        )

        deadline = time.time() + 2.0
        while time.time() < deadline:
            if db_path.exists() and _count_analysis_outputs(db_path, session_id) >= 1:
                break
            await asyncio.sleep(0.05)

        count = _count_analysis_outputs(db_path, session_id) if db_path.exists() else -1
        assert count == 1, (
            "Expected exactly one analysis_outputs row after concurrent event+timeout dispatch. "
            f"count={count}. DC10 deduplication failed."
        )


@pytest.mark.asyncio
async def test_dt_sar_4_terminal_session_sweep_logs_at_debug_not_info(
    tmp_secondsight_home: Path,
    caplog: LogCaptureFixture,
) -> None:
    """DEATH TEST: sweep tick for terminal session must emit DEBUG, not INFO.

    Regression guard: if 'Sweeper: stale candidate already terminal' reverts
    to logger.info(), this test fails — the exact regression that generated
    per-minute INFO spam for finished sessions. If the log is removed entirely,
    the DEBUG assertion catches that too.
    """
    import logging

    registry = ProjectRegistry(secondsight_home=tmp_secondsight_home)

    with patch(
        "secondsight.analysis.runtime._build_analysis_agent",
        return_value=_NoopAnalysisAgent(),
    ):
        resources = await registry.get("proj-terminal")
        stale_ts = _NOW - timedelta(minutes=45)

        resources.events_repository.insert(
            _make_event(
                project_id="proj-terminal",
                session_id="sess-terminal-001",
                event_id="evt-term-start",
                seq=0,
                event_type=EventType.SESSION_START,
                event_ts=stale_ts,
            )
        )

        assert resources.analysis_runtime is not None
        runs_repo = resources.analysis_runtime.analysis_runs_repository
        run_id = runs_repo.start_run("proj-terminal", "sess-terminal-001")
        runs_repo.record_failure(run_id, "simulated failure for death test")

        coordinator = _ServerSweepCoordinator(
            registry=registry,
            secondsight_home=tmp_secondsight_home,
        )
        cutoff = _NOW - timedelta(minutes=30)

        with caplog.at_level(logging.DEBUG):
            await coordinator._sweep_all_projects(cutoff)

    # Death clause: must NOT appear at INFO or above.
    info_spam = [
        r for r in caplog.records if r.levelno >= logging.INFO and "already terminal" in r.message
    ]
    assert not info_spam, (
        f"'already terminal' must be DEBUG, not INFO. "
        f"Got {len(info_spam)} INFO+ record(s): {info_spam[0].message!r}"
    )

    # Must still appear at DEBUG (not silently removed).
    debug_records = [
        r for r in caplog.records if r.levelno == logging.DEBUG and "already terminal" in r.message
    ]
    assert debug_records, (
        "Expected a DEBUG record for 'already terminal' sweep skip — "
        "log was removed entirely, losing the diagnostic."
    )
