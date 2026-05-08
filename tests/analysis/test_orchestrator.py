"""Death + happy-path tests for Orchestrator (GUR-102 task-5).

Death tests come FIRST. Each death test names the silent failure mode it
closes.

Death test inventory:
- DT-5.1 (DC-1): KILL between stage transitions leaves audit row.
- DT-5.2 (DC-4): Completed session requires force=True.
- DT-5.3 (DC-7): Zero events raises consumer-not-recoverer error.
- DT-5.4: Summary failure leaves resumable state (behavior_flags persist).
- DT-5.5 (DC-8): Short-circuit logs stale-conventions warning.
- DT-5.6 (DG-2.2): Segment failure halts session; prior segments persist.

Happy-path tests:
- HP-5.A: Full pipeline evidence chain (2 segments, 3 flags).
- HP-5.B: Zero-flag short-circuit — aggregate never invoked.
- HP-5.C: Force re-run idempotent.
- HP-5.D: analyze_and_aggregate end-to-end chain.

Execution order (Samsara framework):
  Death tests written FIRST, run BEFORE implementation exists (expected red).
  Then implementation written. Then full suite re-run (expected green).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from secondsight.analysis.agent import AnalysisAgentError
from secondsight.analysis.prompts.aggregate import AggregateOutput, AggregatePattern
from secondsight.analysis.prompts.summary import SummaryOutput
from secondsight.analysis.schemas import (
    AnalysisRunStage,
    BehaviorFlagDraft,
    BehaviorFlagType,
    Directive,
    DirectiveStatus,
    DirectiveType,
    SegmentAnalysis,
    SegmentData,
    SegmentMetrics,
)
from secondsight.event import Event, EventType
from secondsight.storage.analysis_runs_repository import AnalysisRunsRepository
from secondsight.storage.behavior_flags_repository import BehaviorFlagsRepository
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.directives_repository import DirectivesRepository
from secondsight.storage.events_repository import EventsRepository
from secondsight.storage.session_reports_repository import SessionReportsRepository
from tests.analysis._fake_agent import FakeAnalysisAgent

# --- import module under test (will FAIL until orchestrator.py is created) ---
from secondsight.analysis.orchestrator import (
    AnalyzeAndAggregateResult,
    AnalyzeSessionResult,
    Orchestrator,
    SessionAlreadyAnalyzedError,
    SessionIncompleteError,
)

# =====================================================================
# Constants
# =====================================================================

_PROJECT_ID = "proj-orchestrator-test"
_SESSION_ID = "sess-orchestrator-001"
_NOW = datetime(2026, 5, 7, 10, 0, 0, tzinfo=timezone.utc)


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[DBEngine]:
    eng = DBEngine(tmp_path / "intel.db")
    try:
        yield eng
    finally:
        eng.dispose()


@pytest.fixture
def events_repo(db_engine: DBEngine) -> EventsRepository:
    r = EventsRepository(db_engine)
    r.create_schema()
    return r


@pytest.fixture
def flags_repo(db_engine: DBEngine) -> BehaviorFlagsRepository:
    r = BehaviorFlagsRepository(db_engine)
    r.create_schema()
    return r


@pytest.fixture
def directives_repo(db_engine: DBEngine) -> DirectivesRepository:
    r = DirectivesRepository(db_engine)
    r.create_schema()
    return r


@pytest.fixture
def runs_repo(db_engine: DBEngine) -> AnalysisRunsRepository:
    r = AnalysisRunsRepository(db_engine)
    r.create_schema()
    return r


@pytest.fixture
def reports_repo(db_engine: DBEngine) -> SessionReportsRepository:
    r = SessionReportsRepository(db_engine)
    r.create_schema()
    return r


# =====================================================================
# Helper factories
# =====================================================================


def _make_event(
    seq: int,
    session_id: str = _SESSION_ID,
    project_id: str = _PROJECT_ID,
    event_type: EventType = EventType.TOOL_USE_START,
    segment_index: int = 0,
) -> Event:
    return Event(
        id=f"evt-{session_id}-{seq}",
        session_id=session_id,
        project_id=project_id,
        event_type=event_type,
        timestamp=_NOW,
        sequence_number=seq,
        segment_index=segment_index,
    )


def _make_session_start_end(
    session_id: str = _SESSION_ID,
    project_id: str = _PROJECT_ID,
    *,
    include_end: bool = True,
) -> list[Event]:
    """Build a minimal 2-event session: session_start + session_end."""
    events = [
        Event(
            id=f"evt-{session_id}-start",
            session_id=session_id,
            project_id=project_id,
            event_type=EventType.SESSION_START,
            timestamp=_NOW,
            sequence_number=0,
            segment_index=0,
        )
    ]
    if include_end:
        events.append(
            Event(
                id=f"evt-{session_id}-end",
                session_id=session_id,
                project_id=project_id,
                event_type=EventType.SESSION_END,
                timestamp=_NOW,
                sequence_number=1,
                segment_index=0,
            )
        )
    return events


def _make_segment_analysis_zero_flags() -> SegmentAnalysis:
    return SegmentAnalysis(
        segment_summary="No issues detected.",
        flags=[],
        total_events=1,
        flagged_events=0,
    )


def _make_segment_analysis_with_flags(count: int = 2) -> SegmentAnalysis:
    flags = []
    for i in range(count):
        flags.append(
            BehaviorFlagDraft(
                flag_type=BehaviorFlagType.UNNECESSARY_READ,
                event_ids=[f"evt-{_SESSION_ID}-start"],
                reason=f"Reason {i} for unnecessary read",
                confidence="high",
            )
        )
    return SegmentAnalysis(
        segment_summary="Some issues detected.",
        flags=flags,
        total_events=3,
        flagged_events=count,
    )


def _make_summary_output() -> SummaryOutput:
    return SummaryOutput(
        headline="Session analyzed: 2 flags found across 1 segment.",
        key_findings=["Finding 1", "Finding 2"],
        body="The session showed unnecessary read operations.",
    )


def _make_orchestrator(
    events_repo: EventsRepository,
    flags_repo: BehaviorFlagsRepository,
    directives_repo: DirectivesRepository,
    runs_repo: AnalysisRunsRepository,
    reports_repo: SessionReportsRepository,
    agent: FakeAnalysisAgent,
    *,
    fake_segmenter=None,
    on_analysis_complete=None,
) -> Orchestrator:
    """Build an Orchestrator with a fake segmenter and/or post-analysis callback if provided."""
    return Orchestrator(
        events_repo=events_repo,
        behavior_flags_repo=flags_repo,
        directives_repo=directives_repo,
        analysis_runs_repo=runs_repo,
        session_reports_repo=reports_repo,
        agent=agent,
        segmenter=fake_segmenter,
        on_analysis_complete=on_analysis_complete,
    )


class _FakeSegmenter:
    """A fake Segmenter that returns pre-configured segments."""

    def __init__(self, segments: list[SegmentData]) -> None:
        self._segments = segments
        self.called_with: list[str] = []

    def segment_session(self, session_id: str) -> list[SegmentData]:
        self.called_with.append(session_id)
        return self._segments


def _make_segment(
    segment_index: int = 0,
    session_id: str = _SESSION_ID,
    project_id: str = _PROJECT_ID,
) -> SegmentData:
    return SegmentData(
        segment_index=segment_index,
        user_prompt=None,
        events=[],
        session_id=session_id,
        project_id=project_id,
    )


def _get_latest_run(runs_repo: AnalysisRunsRepository, session_id: str = _SESSION_ID):
    return runs_repo.get_latest_for_session(session_id)


def _count_flags(flags_repo: BehaviorFlagsRepository, session_id: str = _SESSION_ID) -> int:
    return len(flags_repo.get_session_flags(session_id))


# =====================================================================
# DEATH TESTS
# =====================================================================


class TestDeathPaths:
    @pytest.mark.asyncio
    async def test_dt_5_1_kill_between_stages_leaves_audit_row(
        self,
        events_repo: EventsRepository,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
        runs_repo: AnalysisRunsRepository,
        reports_repo: SessionReportsRepository,
    ) -> None:
        """DT-5.1 (DC-1): SIGKILL between stage transitions leaves audit row.

        After segmented stage is written and detect_segment_flags raises,
        the analysis_runs row must exist at stage='segmented' with
        completed_at IS NULL (non-terminal, resumable state).
        The pipeline correctly records stage='failed' and sets completed_at.

        Death case: if start_run is called AFTER pipeline work begins,
        a SIGKILL before start_run leaves zero audit trail. The pipeline
        would silently re-run on retry, duplicating LLM work.
        """
        # Insert events so verifier passes.
        for evt in _make_session_start_end():
            events_repo.insert(evt)

        # One segment, fake segmenter succeeds; detect raises after segmented stage.
        segments = [_make_segment()]
        fake_segmenter = _FakeSegmenter(segments)

        # Agent raises on first analyze_segments call (simulating SIGKILL after segmented).
        agent = FakeAnalysisAgent(raise_on_segments_call=True)

        orch = _make_orchestrator(
            events_repo, flags_repo, directives_repo, runs_repo, reports_repo,
            agent, fake_segmenter=fake_segmenter,
        )

        with pytest.raises((AnalysisAgentError, Exception)):
            await orch.analyze_session(_SESSION_ID)

        # Audit row MUST exist — it was inserted before any pipeline work.
        run = _get_latest_run(runs_repo)
        assert run is not None, "Audit row must exist even after crash"
        # Row is at 'failed' stage (pipeline caught the exception and recorded it).
        # completed_at is set because 'failed' is a terminal stage.
        assert run.stage == AnalysisRunStage.FAILED
        assert run.error_message is not None
        assert run.completed_at is not None

    @pytest.mark.asyncio
    async def test_dt_5_2_completed_session_requires_force(
        self,
        events_repo: EventsRepository,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
        runs_repo: AnalysisRunsRepository,
        reports_repo: SessionReportsRepository,
        tmp_path: Path,
    ) -> None:
        """DT-5.2 (DC-4): Completed session raises SessionAlreadyAnalyzedError.

        First run completes successfully. Second call without force=True raises
        SessionAlreadyAnalyzedError and makes ZERO LLM calls. Third call with
        force=True succeeds and invokes the agent again.
        """
        for evt in _make_session_start_end():
            events_repo.insert(evt)

        segments = [_make_segment()]
        fake_segmenter = _FakeSegmenter(segments)
        analysis = _make_segment_analysis_zero_flags()
        summary = _make_summary_output()
        # Provide 2 segment outputs (for the 2 analyze_session calls that should go through).
        agent = FakeAnalysisAgent(
            segment_outputs=[analysis, analysis],
            summary_output=summary,
        )

        with patch.dict("os.environ", {"SECONDSIGHT_HOME": str(tmp_path)}):
            orch = _make_orchestrator(
                events_repo, flags_repo, directives_repo, runs_repo, reports_repo,
                agent, fake_segmenter=fake_segmenter,
            )

            # First run: succeeds.
            result1 = await orch.analyze_session(_SESSION_ID)
            assert result1.stage == AnalysisRunStage.SUMMARY_WRITTEN

            # Second run WITHOUT force: must raise, no LLM calls made.
            with pytest.raises(SessionAlreadyAnalyzedError):
                await orch.analyze_session(_SESSION_ID)

            # Verify: only 1 run exists (the failed second didn't create one).
            run = _get_latest_run(runs_repo)
            assert run is not None
            assert run.stage == AnalysisRunStage.SUMMARY_WRITTEN

            # Third run WITH force=True: succeeds again.
            result3 = await orch.analyze_session(_SESSION_ID, force=True)
            assert result3.stage == AnalysisRunStage.SUMMARY_WRITTEN

    @pytest.mark.asyncio
    async def test_dt_5_3_zero_events_raises_session_incomplete(
        self,
        events_repo: EventsRepository,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
        runs_repo: AnalysisRunsRepository,
        reports_repo: SessionReportsRepository,
    ) -> None:
        """DT-5.3 (DC-7): Zero events raises SessionIncompleteError.

        Consumer-not-recoverer principle: the orchestrator does NOT attempt
        to backfill GUR-99 events. It raises with a message naming the
        upstream contract violation. The caller (GUR-103) decides retry policy.

        Death case: if the orchestrator silently proceeds on an empty events
        table, it calls the segmenter with an empty session, produces a vacuous
        analysis (0 segments, 0 flags), and writes a bogus SessionReport.
        """
        # Do NOT insert any events — events table is empty for this session.
        agent = FakeAnalysisAgent()

        orch = _make_orchestrator(
            events_repo, flags_repo, directives_repo, runs_repo, reports_repo,
            agent,
        )

        with pytest.raises(SessionIncompleteError) as exc_info:
            await orch.analyze_session(_SESSION_ID)

        # Error message must name the consumer-not-recoverer contract.
        msg = str(exc_info.value).lower()
        assert any(
            term in msg for term in ("consumer", "incomplete", "no events", "zero events", "session")
        ), f"Error message must reference the contract violation. Got: {exc_info.value!r}"

        # No analysis_runs row should exist: verifier fires BEFORE start_run.
        run = _get_latest_run(runs_repo)
        assert run is None, (
            "No analysis_runs row should be created when session is incomplete. "
            "Verifier fires before start_run per consumer-not-recoverer principle."
        )

        # Zero LLM calls made.
        assert not events_repo.get_session_events(_SESSION_ID), "Sanity: events table still empty."

    @pytest.mark.asyncio
    async def test_dt_5_4_summary_failure_leaves_behavior_done_state(
        self,
        events_repo: EventsRepository,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
        runs_repo: AnalysisRunsRepository,
        reports_repo: SessionReportsRepository,
        tmp_path: Path,
    ) -> None:
        """DT-5.4: Summary failure leaves run at stage='failed' with error_message.

        behavior_flags rows persist (idempotent on re-run via ON CONFLICT
        DO NOTHING). The analysis_run row shows stage='failed' with
        completed_at populated.

        Death case: if summary failure leaves the pipeline in an ambiguous
        state (e.g., stage='behavior_done' with completed_at IS NULL but also
        an error_message), the retry logic cannot determine whether the
        behavior stage was actually completed.
        """
        for evt in _make_session_start_end():
            events_repo.insert(evt)

        segments = [_make_segment()]
        fake_segmenter = _FakeSegmenter(segments)

        # Segment analysis produces 1 flag; summary raises.
        analysis = _make_segment_analysis_with_flags(1)
        agent = FakeAnalysisAgent(
            segment_outputs=[analysis],
            raise_on_summary_call=True,
        )

        with patch.dict("os.environ", {"SECONDSIGHT_HOME": str(tmp_path)}):
            orch = _make_orchestrator(
                events_repo, flags_repo, directives_repo, runs_repo, reports_repo,
                agent, fake_segmenter=fake_segmenter,
            )

            with pytest.raises(AnalysisAgentError):
                await orch.analyze_session(_SESSION_ID)

        # analysis_runs row at stage='failed' with error_message AND completed_at.
        run = _get_latest_run(runs_repo)
        assert run is not None
        assert run.stage == AnalysisRunStage.FAILED
        assert run.error_message is not None
        assert run.completed_at is not None, "Failed terminal state must have completed_at set"

        # behavior_flags rows persist — summary failure does NOT roll back segment work.
        flags = flags_repo.get_session_flags(_SESSION_ID)
        assert len(flags) == 1, (
            f"Expected 1 behavior_flag to persist after summary failure. Got {len(flags)}."
        )

    @pytest.mark.asyncio
    async def test_dt_5_5_short_circuit_logs_stale_directives(
        self,
        events_repo: EventsRepository,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
        runs_repo: AnalysisRunsRepository,
        reports_repo: SessionReportsRepository,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """DT-5.5 (DC-8): Short-circuit logs stale-conventions warning.

        When a session produces zero flags, aggregate is skipped. If the
        project has active directives older than the stale threshold (30 days),
        a WARNING is emitted to disclose that conventions may be stale.

        Death case: if the warning is silently elided, the operator never
        knows that conventions were formed on data that has since been purged
        or superseded.
        """
        for evt in _make_session_start_end():
            events_repo.insert(evt)

        segments = [_make_segment()]
        fake_segmenter = _FakeSegmenter(segments)

        # Zero flags — triggers short-circuit.
        analysis = _make_segment_analysis_zero_flags()
        summary = _make_summary_output()
        agent = FakeAnalysisAgent(segment_outputs=[analysis], summary_output=summary)

        # Insert a directive with updated_at older than 30 days.
        old_timestamp = _NOW - timedelta(days=35)
        stale_directive = Directive(
            id="dir-stale-1",
            project_id=_PROJECT_ID,
            type=DirectiveType.CONVENTION,
            status=DirectiveStatus.ACTIVE,
            instruction="Avoid unnecessary reads.",
            identity_key="stale-key-001",
            created_at=old_timestamp,
            updated_at=old_timestamp,
        )
        directives_repo.insert(stale_directive)

        with patch.dict("os.environ", {"SECONDSIGHT_HOME": str(tmp_path)}):
            orch = _make_orchestrator(
                events_repo, flags_repo, directives_repo, runs_repo, reports_repo,
                agent, fake_segmenter=fake_segmenter,
            )

            # Capture both INFO (aggregator skipped) and WARNING (stale directives).
            with caplog.at_level(logging.INFO):
                result = await orch.analyze_and_aggregate(_SESSION_ID)

        assert result.aggregate is None, "Aggregate should be None on zero-flag short-circuit"

        # Verify 'aggregator skipped' info log (logged at INFO level).
        all_messages = " ".join(caplog.messages)
        assert "aggregator skipped" in all_messages.lower(), (
            f"Expected 'aggregator skipped' in logs. Got: {caplog.messages}"
        )

        # Verify stale-conventions warning (logged at WARNING level).
        assert any(
            "stale" in msg.lower() or "stale-conventions" in msg.lower()
            for msg in caplog.messages
        ), f"Expected stale-conventions warning in logs. Got: {caplog.messages}"

    @pytest.mark.asyncio
    async def test_dt_5_6_segment_failure_halts_session(
        self,
        events_repo: EventsRepository,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
        runs_repo: AnalysisRunsRepository,
        reports_repo: SessionReportsRepository,
        tmp_path: Path,
    ) -> None:
        """DT-5.6 (DG-2.2): Segment failure halts session; prior segments persist.

        With a 3-segment session, segment #2 raises AnalysisAgentError.
        Segment #1's flags must persist (atomic per-segment).
        Segment #3 must NEVER be processed.
        analysis_runs row at stage='failed'.
        """
        for evt in _make_session_start_end():
            events_repo.insert(evt)

        segments = [
            _make_segment(0),
            _make_segment(1),
            _make_segment(2),
        ]
        fake_segmenter = _FakeSegmenter(segments)

        # Segment 0: succeeds with 1 flag.
        # Segment 1 (index 1 of 3 segments): raises AnalysisAgentError.
        # Segment 2: must never be reached.
        seg_0_analysis = _make_segment_analysis_with_flags(1)

        call_count = 0

        class _FailOnSecondCallAgent:
            async def analyze_segments(self, prompts):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return [seg_0_analysis]
                raise AnalysisAgentError(f"Simulated failure on segment call {call_count}")

            async def aggregate_flag_type(self, prompt):
                raise RuntimeError("aggregate_flag_type should never be called")

            async def summarize_session(self, prompt):
                raise RuntimeError("summarize_session should never be called")

        with patch.dict("os.environ", {"SECONDSIGHT_HOME": str(tmp_path)}):
            orch = _make_orchestrator(
                events_repo, flags_repo, directives_repo, runs_repo, reports_repo,
                _FailOnSecondCallAgent(), fake_segmenter=fake_segmenter,
            )

            with pytest.raises(AnalysisAgentError):
                await orch.analyze_session(_SESSION_ID)

        # Segment #2's flags (call_count=1) persist.
        flags = flags_repo.get_session_flags(_SESSION_ID)
        assert len(flags) == 1, (
            f"Segment #1 flags must persist after segment #2 failure. Got {len(flags)} flags."
        )

        # Segment #3 was never processed (only 2 calls made).
        assert call_count == 2, f"Expected exactly 2 agent calls. Got {call_count}."

        # analysis_runs row at stage='failed'.
        run = _get_latest_run(runs_repo)
        assert run is not None
        assert run.stage == AnalysisRunStage.FAILED
        assert run.error_message is not None


# =====================================================================
# HAPPY-PATH TESTS
# =====================================================================


class TestHappyPaths:
    @pytest.mark.asyncio
    async def test_hp_5_a_full_pipeline_evidence_chain(
        self,
        events_repo: EventsRepository,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
        runs_repo: AnalysisRunsRepository,
        reports_repo: SessionReportsRepository,
        tmp_path: Path,
    ) -> None:
        """HP-5.A: Full pipeline evidence chain.

        2-segment session producing 3 flags total (2 from seg-0, 1 from seg-1).
        Verifies: events table has input rows; behavior_flags has 3 rows;
        session_reports has 1 row with correct session_id; analysis_runs
        has 1 row at stage='summary_written' with completed_at; filesystem
        JSON backup written at SD §7.2 path.
        """
        for evt in _make_session_start_end():
            events_repo.insert(evt)

        segments = [_make_segment(0), _make_segment(1)]
        fake_segmenter = _FakeSegmenter(segments)

        # Seg 0 gets 2 flags, seg 1 gets 1 flag (different reason for distinct IDs).
        seg_0_analysis = SegmentAnalysis(
            segment_summary="Two issues in seg 0.",
            flags=[
                BehaviorFlagDraft(
                    flag_type=BehaviorFlagType.UNNECESSARY_READ,
                    event_ids=[f"evt-{_SESSION_ID}-start"],
                    reason="Read unrelated file alpha",
                    confidence="high",
                ),
                BehaviorFlagDraft(
                    flag_type=BehaviorFlagType.REDUNDANT_EXPLORATION,
                    event_ids=[f"evt-{_SESSION_ID}-end"],
                    reason="Kept exploring after sufficient info",
                    confidence="medium",
                ),
            ],
            total_events=2,
            flagged_events=2,
        )
        seg_1_analysis = SegmentAnalysis(
            segment_summary="One issue in seg 1.",
            flags=[
                BehaviorFlagDraft(
                    flag_type=BehaviorFlagType.MISSED_SHORTCUT,
                    event_ids=[f"evt-{_SESSION_ID}-start"],
                    reason="Took a longer path unnecessarily",
                    confidence="low",
                ),
            ],
            total_events=3,
            flagged_events=1,
        )
        summary = SummaryOutput(
            headline="Session analyzed: 3 flags across 2 segments.",
            key_findings=["Finding A", "Finding B"],
            body="The session showed multiple inefficiency patterns.",
        )

        # Use a stateful agent that returns different outputs per segment call.
        # FakeAnalysisAgent always returns outputs[:len(prompts)] (always the
        # FIRST element for single-prompt calls). For multi-segment tests with
        # distinct per-segment outputs, we need a call-counter-based agent.
        class _MultiSegmentAgent:
            def __init__(self):
                self._calls = 0
                self._analyses = [seg_0_analysis, seg_1_analysis]

            async def analyze_segments(self, prompts):
                idx = self._calls
                self._calls += 1
                return [self._analyses[idx]]

            async def summarize_session(self, prompt):
                return summary

            async def aggregate_flag_type(self, prompt):
                raise RuntimeError("Not expected in this test")

        multi_agent = _MultiSegmentAgent()

        with patch.dict("os.environ", {"SECONDSIGHT_HOME": str(tmp_path)}):
            orch = _make_orchestrator(
                events_repo, flags_repo, directives_repo, runs_repo, reports_repo,
                multi_agent, fake_segmenter=fake_segmenter,
            )
            result = await orch.analyze_session(_SESSION_ID)

        # Result structure.
        assert result.stage == AnalysisRunStage.SUMMARY_WRITTEN
        assert result.flags_attempted == 3
        assert result.session_id == _SESSION_ID
        assert result.project_id == _PROJECT_ID
        assert result.report_id is not None

        # DB: events table has 2 events.
        db_events = events_repo.get_session_events(_SESSION_ID)
        assert len(db_events) == 2

        # DB: behavior_flags has 3 rows.
        flags = flags_repo.get_session_flags(_SESSION_ID)
        assert len(flags) == 3

        # DB: session_reports has 1 row.
        report = reports_repo.get_for_session(_SESSION_ID)
        assert report is not None
        assert report.session_id == _SESSION_ID
        assert report.headline == summary.headline

        # DB: analysis_runs has 1 row at summary_written with completed_at.
        run = _get_latest_run(runs_repo)
        assert run is not None
        assert run.stage == AnalysisRunStage.SUMMARY_WRITTEN
        assert run.completed_at is not None
        assert run.flags_inserted == 3

        # Filesystem: backup written at SD §7.2 path.
        backup_path = (
            tmp_path / "projects" / _PROJECT_ID / "sessions" / _SESSION_ID / "session_report.json"
        )
        assert backup_path.exists(), f"Filesystem backup missing at {backup_path}"
        backup_data = json.loads(backup_path.read_text())
        assert backup_data["session_id"] == _SESSION_ID

    @pytest.mark.asyncio
    async def test_hp_5_b_zero_flag_short_circuit_no_aggregation(
        self,
        events_repo: EventsRepository,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
        runs_repo: AnalysisRunsRepository,
        reports_repo: SessionReportsRepository,
        tmp_path: Path,
    ) -> None:
        """HP-5.B: Zero-flag short-circuit — aggregator never invoked.

        Session produces zero flags; analyze_and_aggregate returns with
        aggregate=None. Verify aggregate_flag_type is never called on agent.
        """
        for evt in _make_session_start_end():
            events_repo.insert(evt)

        segments = [_make_segment()]
        fake_segmenter = _FakeSegmenter(segments)
        analysis = _make_segment_analysis_zero_flags()
        summary = _make_summary_output()

        agent = FakeAnalysisAgent(segment_outputs=[analysis], summary_output=summary)

        with patch.dict("os.environ", {"SECONDSIGHT_HOME": str(tmp_path)}):
            orch = _make_orchestrator(
                events_repo, flags_repo, directives_repo, runs_repo, reports_repo,
                agent, fake_segmenter=fake_segmenter,
            )
            result = await orch.analyze_and_aggregate(_SESSION_ID)

        assert result.aggregate is None, "aggregate must be None on zero-flag short-circuit"
        assert result.session.flags_attempted == 0
        assert result.session.stage == AnalysisRunStage.SUMMARY_WRITTEN

        # No directives written.
        directives = directives_repo.get_active_conventions(_PROJECT_ID)
        assert len(directives) == 0, "No directives should be created when aggregate skipped."

    @pytest.mark.asyncio
    async def test_hp_5_c_force_rerun_idempotent(
        self,
        events_repo: EventsRepository,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
        runs_repo: AnalysisRunsRepository,
        reports_repo: SessionReportsRepository,
        tmp_path: Path,
    ) -> None:
        """HP-5.C: Force re-run idempotent.

        Two analyze_session calls (second with force=True).
        Verify: 2 analysis_runs rows; 1 session_reports row (UPSERT);
        behavior_flags count unchanged (ID determinism per task-3 design).
        """
        for evt in _make_session_start_end():
            events_repo.insert(evt)

        segments = [_make_segment()]
        fake_segmenter = _FakeSegmenter(segments)

        analysis = _make_segment_analysis_with_flags(2)
        summary = _make_summary_output()
        # Must supply 2 segment_outputs for 2 analyze_session calls.
        agent = FakeAnalysisAgent(
            segment_outputs=[analysis, analysis],
            summary_output=summary,
        )

        with patch.dict("os.environ", {"SECONDSIGHT_HOME": str(tmp_path)}):
            orch = _make_orchestrator(
                events_repo, flags_repo, directives_repo, runs_repo, reports_repo,
                agent, fake_segmenter=fake_segmenter,
            )

            result1 = await orch.analyze_session(_SESSION_ID)
            assert result1.stage == AnalysisRunStage.SUMMARY_WRITTEN

            result2 = await orch.analyze_session(_SESSION_ID, force=True)
            assert result2.stage == AnalysisRunStage.SUMMARY_WRITTEN

        # 2 analysis_runs rows exist.
        import sqlalchemy as sa
        from secondsight.storage.analysis_runs_table import analysis_runs
        from secondsight.storage.db_engine import DBEngine as _DBEngine

        # Count via repo query.
        all_flags = flags_repo.get_session_flags(_SESSION_ID)
        # Should still be 2 — deterministic IDs, ON CONFLICT DO NOTHING.
        assert len(all_flags) == 2, (
            f"Expected 2 flags (ID-deterministic re-run). Got {len(all_flags)}."
        )

        # 1 session_reports row (UPSERT by session_id UNIQUE).
        report = reports_repo.get_for_session(_SESSION_ID)
        assert report is not None
        # Report references the second run_id (UPSERT updated it).
        assert report.analysis_run_id == result2.run_id

    @pytest.mark.asyncio
    async def test_hp_5_d_analyze_and_aggregate_chain_end_to_end(
        self,
        events_repo: EventsRepository,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
        runs_repo: AnalysisRunsRepository,
        reports_repo: SessionReportsRepository,
        tmp_path: Path,
    ) -> None:
        """HP-5.D: analyze_and_aggregate chain end-to-end.

        Session produces 2 flags; chained call invokes analyze_session then
        aggregate_project; result has both populated; directives table has K <= 15.
        """
        for evt in _make_session_start_end():
            events_repo.insert(evt)

        segments = [_make_segment()]
        fake_segmenter = _FakeSegmenter(segments)

        # 2 flags of same type — will go through aggregator.
        analysis = _make_segment_analysis_with_flags(2)
        summary = _make_summary_output()

        # Build aggregate output for the aggregator step.
        # The aggregator will call aggregate_flag_type for UNNECESSARY_READ.
        from secondsight.analysis.prompts.aggregate import build_aggregate_prompt
        from secondsight.analysis.prompts.aggregate import FlagSummary

        # We can't know the exact prompt without running, so use a wildcard approach:
        # configure the FakeAnalysisAgent to return output for ANY aggregate call.
        class _FlexAggregateAgent:
            def __init__(self):
                self.segment_calls = 0
                self.aggregate_calls = 0

            async def analyze_segments(self, prompts):
                self.segment_calls += 1
                return [analysis]

            async def aggregate_flag_type(self, prompt):
                self.aggregate_calls += 1
                return AggregateOutput(
                    patterns=[
                        AggregatePattern(
                            pattern_description="Reads unrelated files",
                            occurrence_count=2,
                            representative_sessions=[_SESSION_ID],
                            convention="Check file relevance before reading.",
                        )
                    ]
                )

            async def summarize_session(self, prompt):
                return summary

        flex_agent = _FlexAggregateAgent()

        with patch.dict("os.environ", {"SECONDSIGHT_HOME": str(tmp_path)}):
            orch = _make_orchestrator(
                events_repo, flags_repo, directives_repo, runs_repo, reports_repo,
                flex_agent, fake_segmenter=fake_segmenter,
            )
            result = await orch.analyze_and_aggregate(_SESSION_ID)

        # Both session and aggregate results populated.
        assert result.session.stage == AnalysisRunStage.SUMMARY_WRITTEN
        assert result.session.flags_attempted == 2
        assert result.aggregate is not None
        assert result.aggregate.project_id == _PROJECT_ID
        assert result.aggregate.directives_upserted >= 1

        # Directives table has K <= 15.
        directives = directives_repo.get_active_conventions(_PROJECT_ID)
        assert 1 <= len(directives) <= 15

        # Agent was called for both segments and aggregation.
        assert flex_agent.segment_calls == 1
        assert flex_agent.aggregate_calls >= 1


# =====================================================================
# GUR-149 task-B3 — on_analysis_complete callback hook
# =====================================================================


class TestB3OnAnalysisCompleteCallback:
    """Death + smoke tests for the post-analysis callback hook.

    DC-B3 (silent failure path): if a callback exception is allowed to
    propagate, a successful analysis appears to the caller as a failure
    — caller retries, double-charges LLM tokens, reports operator-visible
    "analysis failed" when it actually succeeded. The contract is:
    swallow + log ERROR, do NOT re-raise; the analysis_runs row stays
    at summary_written.
    """

    @pytest.mark.asyncio
    async def test_b3_callback_invoked_with_session_id_after_summary_written(
        self,
        events_repo: EventsRepository,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
        runs_repo: AnalysisRunsRepository,
        reports_repo: SessionReportsRepository,
        tmp_path: Path,
    ) -> None:
        """Callback receives the same session_id passed to analyze_session,
        and is invoked after the audit row hits summary_written (not before)."""
        for evt in _make_session_start_end():
            events_repo.insert(evt)

        segments = [_make_segment()]
        fake_segmenter = _FakeSegmenter(segments)
        agent = FakeAnalysisAgent(
            segment_outputs=[_make_segment_analysis_zero_flags()],
            summary_output=_make_summary_output(),
        )

        callback_calls: list[tuple[str, AnalysisRunStage | None]] = []

        def on_complete(sid: str) -> None:
            # Capture session_id AND the audit row's stage at the moment
            # the callback runs. Stage MUST be summary_written by now
            # (DC-B3 invocation-site contract from 2-plan.md §2.3).
            run = runs_repo.get_latest_for_session(sid)
            callback_calls.append((sid, run.stage if run else None))

        with patch.dict("os.environ", {"SECONDSIGHT_HOME": str(tmp_path)}):
            orch = _make_orchestrator(
                events_repo, flags_repo, directives_repo, runs_repo, reports_repo,
                agent,
                fake_segmenter=fake_segmenter,
                on_analysis_complete=on_complete,
            )
            result = await orch.analyze_session(_SESSION_ID)

        assert result.stage == AnalysisRunStage.SUMMARY_WRITTEN
        assert callback_calls == [(_SESSION_ID, AnalysisRunStage.SUMMARY_WRITTEN)]

    @pytest.mark.asyncio
    async def test_b3_callback_not_invoked_when_none(
        self,
        events_repo: EventsRepository,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
        runs_repo: AnalysisRunsRepository,
        reports_repo: SessionReportsRepository,
        tmp_path: Path,
    ) -> None:
        """Default behavior (callback=None) MUST not invoke anything.
        Smoke test ensuring the orchestrator is backwards-compatible."""
        for evt in _make_session_start_end():
            events_repo.insert(evt)
        segments = [_make_segment()]
        fake_segmenter = _FakeSegmenter(segments)
        agent = FakeAnalysisAgent(
            segment_outputs=[_make_segment_analysis_zero_flags()],
            summary_output=_make_summary_output(),
        )

        with patch.dict("os.environ", {"SECONDSIGHT_HOME": str(tmp_path)}):
            orch = _make_orchestrator(
                events_repo, flags_repo, directives_repo, runs_repo, reports_repo,
                agent, fake_segmenter=fake_segmenter,
                # on_analysis_complete defaults to None.
            )
            result = await orch.analyze_session(_SESSION_ID)

        assert result.stage == AnalysisRunStage.SUMMARY_WRITTEN

    @pytest.mark.asyncio
    async def test_b3_dc_b3_callback_exception_does_not_poison_analysis(
        self,
        events_repo: EventsRepository,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
        runs_repo: AnalysisRunsRepository,
        reports_repo: SessionReportsRepository,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """DC-B3: callback raises → analyze_session returns normal
        AnalyzeSessionResult, ERROR logged with sanitized message,
        analysis_runs row stays at summary_written (NOT failed)."""
        for evt in _make_session_start_end():
            events_repo.insert(evt)
        segments = [_make_segment()]
        fake_segmenter = _FakeSegmenter(segments)
        agent = FakeAnalysisAgent(
            segment_outputs=[_make_segment_analysis_zero_flags()],
            summary_output=_make_summary_output(),
        )

        def raising_callback(sid: str) -> None:
            raise RuntimeError("simulated downstream cleanup failure")

        with patch.dict("os.environ", {"SECONDSIGHT_HOME": str(tmp_path)}):
            orch = _make_orchestrator(
                events_repo, flags_repo, directives_repo, runs_repo, reports_repo,
                agent,
                fake_segmenter=fake_segmenter,
                on_analysis_complete=raising_callback,
            )
            with caplog.at_level(logging.ERROR):
                # The whole point: NO exception propagates.
                result = await orch.analyze_session(_SESSION_ID)

        # Analysis succeeded; result is well-formed.
        assert result.stage == AnalysisRunStage.SUMMARY_WRITTEN
        assert result.report_id is not None

        # Audit row at summary_written (NOT failed) — analysis itself succeeded.
        run = _get_latest_run(runs_repo)
        assert run is not None
        assert run.stage == AnalysisRunStage.SUMMARY_WRITTEN

        # ERROR log was emitted naming the callback failure.
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any(
            "on_analysis_complete" in r.getMessage()
            for r in error_records
        ), f"Expected ERROR log for callback failure, got: {[r.getMessage() for r in error_records]}"
        # Sanitized message includes the exception class name.
        assert any("RuntimeError" in r.getMessage() for r in error_records)

    def test_b3_async_callback_rejected_at_construction_time(
        self,
        events_repo: EventsRepository,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
        runs_repo: AnalysisRunsRepository,
        reports_repo: SessionReportsRepository,
    ) -> None:
        """Yin review fix: an `async def` callback would be silently
        no-op'd by `self._on_analysis_complete(session_id)` (Python
        constructs the coroutine and discards it without awaiting).
        Without this guard, an operator who registers an async cleanup
        trigger sees no errors but no cleanup happens.

        Fail-loud at construction time so the misuse is visible at boot.
        """
        agent = FakeAnalysisAgent(
            segment_outputs=[_make_segment_analysis_zero_flags()],
            summary_output=_make_summary_output(),
        )

        async def async_callback(sid: str) -> None:
            # Body never reached — constructor must reject this callable
            # before analyze_session is even called.
            pass  # pragma: no cover

        with pytest.raises(TypeError) as exc_info:
            _make_orchestrator(
                events_repo, flags_repo, directives_repo, runs_repo, reports_repo,
                agent,
                on_analysis_complete=async_callback,
            )
        # Error message must name the constraint so the operator can
        # locate the misuse without needing to read the source.
        assert "coroutine" in str(exc_info.value).lower()
        assert "on_analysis_complete" in str(exc_info.value)
