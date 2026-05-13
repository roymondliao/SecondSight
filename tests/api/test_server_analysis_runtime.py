"""Server analysis runtime wiring tests.

Death tests first:
- DT-SAR-1: `session_end` through the server path must create an analysis run.
- DT-SAR-2: stale-session sweep must dispatch timeout recovery instead of warning-only no-op.
- DT-SAR-3: event path + timeout path must not create duplicate runs for the same session.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

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


def _wait_for_count(
    *,
    db_path: Path,
    session_id: str,
    expected: int,
    timeout_s: float = 2.0,
) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if db_path.exists() and _count_analysis_runs(db_path, session_id) == expected:
            return
        time.sleep(0.05)
    actual = _count_analysis_runs(db_path, session_id) if db_path.exists() else -1
    raise AssertionError(
        f"Expected {expected} analysis_runs rows for session_id={session_id!r}, got {actual}."
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
async def test_dt_sar_1_session_end_creates_analysis_run(
    tmp_secondsight_home: Path,
) -> None:
    """DEATH TEST: persisted `session_end` must not leave analysis_runs empty."""
    registry = ProjectRegistry(secondsight_home=tmp_secondsight_home)
    db_path = tmp_secondsight_home / "projects" / "proj-runtime" / "intelligence.db"

    with patch(
        "secondsight.analysis.runtime._build_analysis_agent",
        return_value=_NoopAnalysisAgent(),
    ):
        app = create_app(secondsight_home=tmp_secondsight_home, registry=registry)
        with TestClient(app, raise_server_exceptions=False) as client:
            start = client.post(
                "/hook/session_start",
                json=_make_valid_payload(
                    project_id="proj-runtime",
                    session_id="sess-runtime-001",
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
                    session_id="sess-runtime-001",
                    event_id="evt-runtime-end",
                    seq=1,
                    event_ts=_NOW + timedelta(seconds=1),
                ),
            )
            assert end.status_code == 200, end.text

            _wait_for_count(
                db_path=db_path,
                session_id="sess-runtime-001",
                expected=1,
            )


@pytest.mark.asyncio
async def test_dt_sar_2_sweeper_dispatches_timeout_recovery(
    tmp_secondsight_home: Path,
) -> None:
    """DEATH TEST: stale-session sweep must dispatch recovery, not just warn."""
    registry = ProjectRegistry(secondsight_home=tmp_secondsight_home)
    db_path = tmp_secondsight_home / "projects" / "proj-sweeper" / "intelligence.db"

    with patch(
        "secondsight.analysis.runtime._build_analysis_agent",
        return_value=_NoopAnalysisAgent(),
    ):
        resources = await registry.get("proj-sweeper")
        stale_ts = _NOW - timedelta(minutes=45)
        resources.events_repository.insert(
            _make_event(
                project_id="proj-sweeper",
                session_id="sess-stale-001",
                event_id="evt-stale-start",
                seq=0,
                event_type=EventType.SESSION_START,
                event_ts=stale_ts,
            )
        )
        resources.events_repository.insert(
            _make_event(
                project_id="proj-sweeper",
                session_id="sess-stale-001",
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
            if db_path.exists() and _count_analysis_runs(db_path, "sess-stale-001") == 1:
                break
            await asyncio.sleep(0.05)
        else:
            count = _count_analysis_runs(db_path, "sess-stale-001") if db_path.exists() else -1
            raise AssertionError(
                f"Sweeper did not dispatch timeout recovery. analysis_runs count={count}"
            )


@pytest.mark.asyncio
async def test_dt_sar_3_event_and_timeout_share_trigger_without_duplicate_runs(
    tmp_secondsight_home: Path,
) -> None:
    """DEATH TEST: event path + timeout path must not create duplicate runs."""
    registry = ProjectRegistry(secondsight_home=tmp_secondsight_home)
    db_path = tmp_secondsight_home / "projects" / "proj-race" / "intelligence.db"

    with patch(
        "secondsight.analysis.runtime._build_analysis_agent",
        return_value=_NoopAnalysisAgent(delay_s=0.05),
    ):
        resources = await registry.get("proj-race")
        assert resources.analysis_runtime is not None

        stale_ts = _NOW - timedelta(minutes=45)
        resources.events_repository.insert(
            _make_event(
                project_id="proj-race",
                session_id="sess-race-001",
                event_id="evt-race-start",
                seq=0,
                event_type=EventType.SESSION_START,
                event_ts=stale_ts,
            )
        )
        resources.events_repository.insert(
            _make_event(
                project_id="proj-race",
                session_id="sess-race-001",
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
                "sess-race-001",
                source="event",
            ),
            coordinator._sweep_all_projects(cutoff),
        )

        deadline = time.time() + 2.0
        while time.time() < deadline:
            if db_path.exists() and _count_analysis_runs(db_path, "sess-race-001") == 1:
                break
            await asyncio.sleep(0.05)
        else:
            count = _count_analysis_runs(db_path, "sess-race-001") if db_path.exists() else -1
            raise AssertionError(
                "Expected exactly one analysis_run after concurrent event+timeout dispatch. "
                f"count={count}"
            )
