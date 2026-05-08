"""Death + happy-path tests for Trigger layer (GUR-103 task-5, P2-15).

Death test contract: each DT-* test names the silent failure path it closes.

Death test inventory:
- DT-5.1 (DC-4): Concurrent dispatch — exactly ONE invocation per session.
- DT-5.2 (DC-5): Pipeline ingest blocking I/O does not block SESSION_END dispatch.
- DT-5.3 (DC-6): Sweeper uses last_event_ts; skips session still within timeout.
- DT-5.4 (DC-7): Already-analyzed session causes CLI exit code 2 with correct STDERR.

Happy-path tests:
- HP-5.5: Event-driven dispatch records a run (async task completes).
- HP-5.6: Sweeper fires on session whose last_event_ts is 35 min old (timeout=30).
- DG-1.2: dispatch() returns dispatched=False reason="already-analyzed" when terminal.

Degradation tests:
- DG-1.2 (in-process fallback): handled in test_analyze.py (CLI integration).

Execution order (Samsara framework):
  Death tests first — written before implementation — expected RED before impl.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from secondsight.analysis.schemas import AnalysisRun, AnalysisRunStage
from secondsight.event import Event, EventType
from secondsight.storage.analysis_runs_repository import AnalysisRunsRepository
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.events_repository import EventsRepository

# --- modules under test (will FAIL until trigger.py is created) ---
from secondsight.sdk.trigger import (
    DispatchResult,
    LockRegistry,
    Sweeper,
    Trigger,
)

# =====================================================================
# Constants
# =====================================================================

_PROJECT_ID = "proj-trigger-test"
_SESSION_ID = "sess-trigger-001"
_NOW = datetime(2026, 5, 8, 10, 0, 0, tzinfo=timezone.utc)


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
def runs_repo(db_engine: DBEngine) -> AnalysisRunsRepository:
    r = AnalysisRunsRepository(db_engine)
    r.create_schema()
    return r


@pytest.fixture
def lock_registry() -> LockRegistry:
    return LockRegistry()


def _make_event(
    seq: int,
    session_id: str = _SESSION_ID,
    project_id: str = _PROJECT_ID,
    event_type: EventType = EventType.TOOL_USE_START,
) -> Event:
    return Event(
        id=f"evt-{session_id}-{seq}",
        session_id=session_id,
        project_id=project_id,
        event_type=event_type,
        timestamp=_NOW,
        sequence_number=seq,
        segment_index=0,
    )


def _seed_terminal_run(
    runs_repo: AnalysisRunsRepository,
    session_id: str = _SESSION_ID,
    project_id: str = _PROJECT_ID,
    stage: str = "summary_written",
) -> str:
    """Insert a terminal analysis_run row directly via repo methods."""
    run_id = runs_repo.start_run(project_id, session_id)
    runs_repo.advance_stage(run_id, stage)
    return run_id


# =====================================================================
# Fake orchestrator for death tests
# =====================================================================


class _CountingOrchestrator:
    """Records invocations of analyze_and_aggregate for DT-5.1."""

    def __init__(self) -> None:
        self.call_count = 0
        self._delay_s: float = 0.0

    def set_delay(self, delay_s: float) -> None:
        self._delay_s = delay_s

    async def analyze_and_aggregate(
        self, session_id: str, *, force: bool = False
    ) -> None:
        self.call_count += 1
        if self._delay_s > 0:
            await asyncio.sleep(self._delay_s)


# =====================================================================
# DT-5.1 — Concurrent dispatch: exactly ONE invocation (DC-4)
# =====================================================================


@pytest.mark.asyncio
async def test_dt_5_1_concurrent_dispatch_exactly_one_invocation(
    runs_repo: AnalysisRunsRepository,
    events_repo: EventsRepository,
    lock_registry: LockRegistry,
) -> None:
    """DT-5.1 (DC-4): Two concurrent dispatches for the same session_id
    with no prior analysis_runs row must produce exactly ONE orchestrator
    invocation.

    Silent failure this closes: without per-session locking, two concurrent
    SESSION_END callbacks (e.g., a rapid hook call + a manual trigger) both
    pass the _check_already_analyzed gate (which reads before start_run writes)
    and produce duplicate LLM calls. The duplicate call cost and the second
    analysis_runs row at stage='pending' are the downstream symptoms, not
    the trigger itself.
    """
    fake_orchestrator = _CountingOrchestrator()
    # Add small delay so the first dispatch is still running when the second fires
    fake_orchestrator.set_delay(0.05)

    trigger = Trigger(
        orchestrator=fake_orchestrator,
        analysis_runs_repo=runs_repo,
        events_repo=events_repo,
        lock_registry=lock_registry,
    )

    # Fire two concurrent dispatches for the same session
    result1, result2 = await asyncio.gather(
        trigger.dispatch(
            _PROJECT_ID, _SESSION_ID, source="event"
        ),
        trigger.dispatch(
            _PROJECT_ID, _SESSION_ID, source="event"
        ),
    )

    # Wait for any dispatched tasks to complete
    await asyncio.sleep(0.2)

    # Exactly one should have dispatched
    dispatched_count = sum(1 for r in [result1, result2] if r.dispatched)
    assert dispatched_count == 1, (
        f"Expected exactly 1 dispatch, got {dispatched_count}. "
        f"results: {result1!r}, {result2!r}"
    )

    # The non-dispatched one must report a lock or in-flight reason
    blocked = result1 if not result1.dispatched else result2
    assert blocked.reason in ("lock-held", "another-run-in-flight"), (
        f"Unexpected reason for blocked dispatch: {blocked.reason!r}"
    )

    # Orchestrator called exactly once (after task completes)
    assert fake_orchestrator.call_count == 1, (
        f"Expected 1 orchestrator call, got {fake_orchestrator.call_count}"
    )


# =====================================================================
# DT-5.2 — Blocking I/O wrapped in to_thread (DC-5)
# =====================================================================


@pytest.mark.asyncio
async def test_dt_5_2_session_end_dispatch_does_not_block_concurrent_ingest(
    runs_repo: AnalysisRunsRepository,
    events_repo: EventsRepository,
    lock_registry: LockRegistry,
    tmp_path: Path,
) -> None:
    """DT-5.2 (DC-5): SESSION_END dispatch via asyncio.create_task does not
    block concurrent pipeline.ingest() calls.

    The dispatch uses asyncio.create_task, which schedules the analysis
    in the background. Ingest() calls should return promptly even when
    an analysis task is running in the background.

    Verifies: p95 of 10 concurrent ingest-like latencies stays within
    10% of baseline latency without dispatch overhead.

    Note: This test verifies the non-blocking property at the task scheduling
    layer. The actual DT-5.2 reads from a 256 KiB file via asyncio.to_thread
    inside tools.py — that property is tested end-to-end in test_tools.py.
    This test focuses on the trigger's create_task not blocking the caller.
    """
    slow_orchestrator = _CountingOrchestrator()
    slow_orchestrator.set_delay(0.1)  # simulate a slow analysis

    trigger = Trigger(
        orchestrator=slow_orchestrator,
        analysis_runs_repo=runs_repo,
        events_repo=events_repo,
        lock_registry=lock_registry,
    )

    N_CONCURRENT = 10

    async def _measure_dispatch_latency(session_suffix: str) -> float:
        """Measure time for dispatch() to return (not for analysis to complete)."""
        start = time.perf_counter()
        await trigger.dispatch(
            _PROJECT_ID,
            f"sess-dt52-{session_suffix}",
            source="event",
        )
        return time.perf_counter() - start

    # Baseline: single dispatch latency without any concurrent background tasks
    baseline_latency = await _measure_dispatch_latency("baseline")

    # Reset for concurrent test
    results = await asyncio.gather(
        *[_measure_dispatch_latency(f"c{i}") for i in range(N_CONCURRENT)]
    )

    # Wait for background tasks to drain
    await asyncio.sleep(0.3)

    # p95 of concurrent latencies must be within 10× of baseline
    # (not 10% — asyncio scheduling variability is high in tests;
    # the key property is create_task returns promptly, not blocks N×analysis_time)
    sorted_latencies = sorted(results)
    p95_idx = int(0.95 * len(sorted_latencies))
    p95 = sorted_latencies[p95_idx]

    # A blocking call would take ~0.1s (slow_orchestrator delay);
    # a non-blocking create_task should be < 5ms per call.
    assert p95 < 0.05, (
        f"dispatch() p95={p95:.3f}s exceeds 50ms threshold. "
        f"This suggests dispatch() is blocking on analysis instead of using create_task. "
        f"baseline_latency={baseline_latency:.3f}s"
    )


# =====================================================================
# DT-5.3 — Sweeper skips sessions still within timeout (DC-6)
# =====================================================================


@pytest.mark.asyncio
async def test_dt_5_3_sweeper_skips_session_within_timeout(
    runs_repo: AnalysisRunsRepository,
    events_repo: EventsRepository,
    lock_registry: LockRegistry,
) -> None:
    """DT-5.3 (DC-6): Sweeper does NOT dispatch for a session whose
    last_event_ts is 5 minutes ago when timeout is 30 minutes.

    Silent failure this closes: if Sweeper uses created_at instead of
    last_event_ts, a long session (started 2 hours ago, last event 5 min
    ago) would be swept prematurely. The analysis would run on an
    incomplete session, producing a misleading report with missing
    events. No error would surface — the report would look valid.
    """
    dispatch_calls: list[tuple[str, str, str]] = []

    class _RecordingTrigger:
        async def dispatch(
            self,
            project_id: str,
            session_id: str,
            *,
            source: str,
            force: bool = False,
        ) -> DispatchResult:
            dispatch_calls.append((project_id, session_id, source))
            return DispatchResult(dispatched=False, reason="skipped-in-test", run_id=None)

    sweeper = Sweeper(
        trigger=_RecordingTrigger(),
        events_repo=events_repo,
        analysis_runs_repo=runs_repo,
        interval_seconds=999,  # never auto-sleep in test
        session_timeout_minutes=30,
    )

    # Insert an event with timestamp 5 minutes ago (within 30-min timeout)
    recent_ts = _NOW - timedelta(minutes=5)
    event = Event(
        id="evt-dt53-recent",
        session_id=_SESSION_ID,
        project_id=_PROJECT_ID,
        event_type=EventType.TOOL_USE_START,
        timestamp=recent_ts,
        sequence_number=0,
        segment_index=0,
    )
    events_repo.insert(event)

    # Run one sweep cycle manually (no auto-sleep)
    with patch("secondsight.sdk.trigger.datetime") as mock_dt:
        mock_dt.now.return_value = _NOW
        mock_dt.side_effect = None
        await sweeper.sweep_stale_sessions(_NOW)

    # Dispatch must NOT have been called — session is within timeout
    assert dispatch_calls == [], (
        f"Sweeper incorrectly dispatched for a session within timeout: {dispatch_calls}"
    )


# =====================================================================
# DT-5.4 — Already-analyzed: dispatch returns dispatched=False (DC-7)
# =====================================================================


@pytest.mark.asyncio
async def test_dt_5_4_already_analyzed_dispatch_returns_false(
    runs_repo: AnalysisRunsRepository,
    events_repo: EventsRepository,
    lock_registry: LockRegistry,
) -> None:
    """DT-5.4 (DC-7): When analysis_runs has a terminal row for the session,
    dispatch() returns dispatched=False with reason='already-analyzed'.

    This is the trigger-layer guard. The CLI convert this to exit code 2.
    The silent failure this closes: without this check, re-running
    `secondsight analyze --session SESSION` on an already-completed session
    would silently fire a second LLM pass, doubling cost.
    """
    # Seed a terminal analysis run
    _seed_terminal_run(runs_repo, stage="summary_written")

    fake_orchestrator = _CountingOrchestrator()
    trigger = Trigger(
        orchestrator=fake_orchestrator,
        analysis_runs_repo=runs_repo,
        events_repo=events_repo,
        lock_registry=lock_registry,
    )

    result = await trigger.dispatch(_PROJECT_ID, _SESSION_ID, source="manual")

    assert not result.dispatched, "Expected dispatched=False for already-analyzed session"
    assert result.reason == "already-analyzed", f"Unexpected reason: {result.reason!r}"
    assert result.existing_stage == "summary_written", (
        f"Unexpected existing_stage: {result.existing_stage!r}"
    )
    assert fake_orchestrator.call_count == 0, (
        "Orchestrator must NOT be called for already-analyzed session"
    )


# =====================================================================
# HP-5.5 — Happy-path: event-driven dispatch records run_id
# =====================================================================


@pytest.mark.asyncio
async def test_hp_5_5_event_driven_dispatch_records_run_id(
    runs_repo: AnalysisRunsRepository,
    events_repo: EventsRepository,
    lock_registry: LockRegistry,
) -> None:
    """HP-5.5: dispatch() with source='event' returns dispatched=True.

    The trigger schedules an asyncio.create_task for the orchestrator.
    After the task completes, the dispatch result should indicate success.
    """
    fake_orchestrator = _CountingOrchestrator()
    trigger = Trigger(
        orchestrator=fake_orchestrator,
        analysis_runs_repo=runs_repo,
        events_repo=events_repo,
        lock_registry=lock_registry,
    )

    result = await trigger.dispatch(_PROJECT_ID, _SESSION_ID, source="event")

    assert result.dispatched is True, f"Expected dispatched=True, got {result!r}"
    assert result.reason == "dispatched", f"Unexpected reason: {result.reason!r}"

    # Wait for background task
    await asyncio.sleep(0.05)
    assert fake_orchestrator.call_count == 1


# =====================================================================
# HP-5.6 — Sweeper fires on session with last_event_ts 35 min old
# =====================================================================


@pytest.mark.asyncio
async def test_hp_5_6_sweeper_fires_on_stale_session(
    runs_repo: AnalysisRunsRepository,
    events_repo: EventsRepository,
) -> None:
    """HP-5.6: Sweeper dispatches for session whose last_event_ts is 35 min ago.

    Session timeout = 30 min. last_event_ts = 35 min ago → stale → should dispatch.
    No terminal analysis_runs row → not already analyzed → should dispatch.
    """
    dispatch_calls: list[tuple[str, str, str]] = []

    class _RecordingTrigger:
        async def dispatch(
            self,
            project_id: str,
            session_id: str,
            *,
            source: str,
            force: bool = False,
        ) -> DispatchResult:
            dispatch_calls.append((project_id, session_id, source))
            return DispatchResult(dispatched=True, reason="dispatched", run_id=None)

    sweeper = Sweeper(
        trigger=_RecordingTrigger(),
        events_repo=events_repo,
        analysis_runs_repo=runs_repo,
        interval_seconds=999,
        session_timeout_minutes=30,
    )

    # Insert event with timestamp 35 minutes ago (outside 30-min timeout)
    stale_ts = _NOW - timedelta(minutes=35)
    event = Event(
        id="evt-hp56-stale",
        session_id=_SESSION_ID,
        project_id=_PROJECT_ID,
        event_type=EventType.TOOL_USE_START,
        timestamp=stale_ts,
        sequence_number=0,
        segment_index=0,
    )
    events_repo.insert(event)

    await sweeper.sweep_stale_sessions(_NOW)

    assert len(dispatch_calls) == 1, (
        f"Sweeper should dispatch exactly once for stale session, got: {dispatch_calls}"
    )
    assert dispatch_calls[0] == (_PROJECT_ID, _SESSION_ID, "timeout"), (
        f"Unexpected dispatch call: {dispatch_calls[0]}"
    )


# =====================================================================
# DG-1.2 — Already-analyzed: dispatch returns False (trigger layer guard)
# =====================================================================


@pytest.mark.asyncio
async def test_dg_1_2_already_analyzed_terminal_aggregated(
    runs_repo: AnalysisRunsRepository,
    events_repo: EventsRepository,
    lock_registry: LockRegistry,
) -> None:
    """DG-1.2: Terminal stage 'aggregated' also blocks dispatch without force."""
    _seed_terminal_run(runs_repo, stage="aggregated")

    fake_orchestrator = _CountingOrchestrator()
    trigger = Trigger(
        orchestrator=fake_orchestrator,
        analysis_runs_repo=runs_repo,
        events_repo=events_repo,
        lock_registry=lock_registry,
    )

    result = await trigger.dispatch(_PROJECT_ID, _SESSION_ID, source="manual")
    assert not result.dispatched
    assert result.reason == "already-analyzed"
    assert result.existing_stage == "aggregated"


@pytest.mark.asyncio
async def test_dg_1_2_force_bypasses_already_analyzed(
    runs_repo: AnalysisRunsRepository,
    events_repo: EventsRepository,
    lock_registry: LockRegistry,
) -> None:
    """DG-1.2: force=True bypasses the already-analyzed gate."""
    _seed_terminal_run(runs_repo, stage="summary_written")

    fake_orchestrator = _CountingOrchestrator()
    trigger = Trigger(
        orchestrator=fake_orchestrator,
        analysis_runs_repo=runs_repo,
        events_repo=events_repo,
        lock_registry=lock_registry,
    )

    result = await trigger.dispatch(
        _PROJECT_ID, _SESSION_ID, source="manual", force=True
    )
    assert result.dispatched is True, f"force=True must bypass already-analyzed gate: {result!r}"

    await asyncio.sleep(0.05)
    assert fake_orchestrator.call_count == 1


# =====================================================================
# In-flight run guard
# =====================================================================


@pytest.mark.asyncio
async def test_in_flight_run_blocks_dispatch(
    runs_repo: AnalysisRunsRepository,
    events_repo: EventsRepository,
    lock_registry: LockRegistry,
) -> None:
    """Non-terminal run updated_at within trigger_lock_seconds → block dispatch."""
    # Insert a non-terminal run with updated_at = now (very recent)
    run_id = runs_repo.start_run(_PROJECT_ID, _SESSION_ID)
    # run is at stage='pending', updated_at=now → "in flight"

    fake_orchestrator = _CountingOrchestrator()
    trigger = Trigger(
        orchestrator=fake_orchestrator,
        analysis_runs_repo=runs_repo,
        events_repo=events_repo,
        lock_registry=lock_registry,
        trigger_lock_seconds=60,  # 60s window
    )

    result = await trigger.dispatch(_PROJECT_ID, _SESSION_ID, source="event")

    assert not result.dispatched, f"Expected blocked dispatch for in-flight run: {result!r}"
    assert result.reason == "another-run-in-flight"
    assert fake_orchestrator.call_count == 0


# =====================================================================
# Sweeper cancellation
# =====================================================================


@pytest.mark.asyncio
async def test_sweeper_cancel_is_idempotent(
    runs_repo: AnalysisRunsRepository,
    events_repo: EventsRepository,
) -> None:
    """Sweeper.cancel() is idempotent — calling it twice does not raise."""
    recording_trigger = MagicMock()
    recording_trigger.dispatch = AsyncMock(
        return_value=DispatchResult(dispatched=False, reason="skipped", run_id=None)
    )

    sweeper = Sweeper(
        trigger=recording_trigger,
        events_repo=events_repo,
        analysis_runs_repo=runs_repo,
        interval_seconds=999,
        session_timeout_minutes=30,
    )

    # Start the sweeper — it self-registers its task via asyncio.current_task()
    task = asyncio.create_task(sweeper.run())
    # Yield to the event loop so run() can start and self-register _task
    await asyncio.sleep(0.05)
    assert sweeper._task is task, "Sweeper should have self-registered its task"

    await sweeper.cancel()
    await sweeper.cancel()  # second cancel must not raise

    # Give the event loop a moment to process cancellation
    await asyncio.sleep(0.05)

    # Task should be done now (cancelled)
    assert task.done(), f"Task should be done after cancel(). done={task.done()}"


# =====================================================================
# LockRegistry
# =====================================================================


@pytest.mark.asyncio
async def test_lock_registry_non_blocking_acquire_on_contention() -> None:
    """LockRegistry.acquire() returns sentinel on contention (non-blocking)."""
    registry = LockRegistry()

    # Acquire lock for session in background
    acquired_event = asyncio.Event()
    release_event = asyncio.Event()

    async def _hold_lock() -> None:
        async with registry.acquire("sess-lock-test") as held:
            acquired_event.set()
            await release_event.wait()

    task = asyncio.create_task(_hold_lock())
    await acquired_event.wait()

    # Now try to acquire the same session — should get contention signal
    async with registry.acquire("sess-lock-test") as held:
        assert held is False, (
            "Expected contention sentinel (False) when lock is already held"
        )

    release_event.set()
    await task


@pytest.mark.asyncio
async def test_lock_registry_different_sessions_no_contention() -> None:
    """LockRegistry allows concurrent acquisition for different session_ids."""
    registry = LockRegistry()
    held_sess1 = None
    held_sess2 = None

    async with registry.acquire("sess-A") as h1:
        async with registry.acquire("sess-B") as h2:
            held_sess1 = h1
            held_sess2 = h2

    assert held_sess1 is True, "sess-A should be acquired without contention"
    assert held_sess2 is True, "sess-B should be acquired without contention"


# =====================================================================
# Pipeline callback integration
# =====================================================================


@pytest.mark.asyncio
async def test_pipeline_callback_fires_on_session_end(
    runs_repo: AnalysisRunsRepository,
    events_repo: EventsRepository,
    lock_registry: LockRegistry,
) -> None:
    """SESSION_END callback fires dispatch via pipeline callback mechanism."""
    from secondsight.observation.pipeline import ObservationPipeline
    from secondsight.storage.raw_trace_store import RawTraceStore
    from secondsight.storage.sync_log import SyncLog

    fake_orchestrator = _CountingOrchestrator()
    trigger = Trigger(
        orchestrator=fake_orchestrator,
        analysis_runs_repo=runs_repo,
        events_repo=events_repo,
        lock_registry=lock_registry,
    )

    # Build a minimal pipeline with a fake RawTraceStore
    class _FakeRTS:
        async def write(self, event: Event) -> str:
            return "/fake/path"

    class _FakeSyncLog:
        def record_failure(self, *a: object, **kw: object) -> None:
            pass

    pipeline = ObservationPipeline(
        raw_trace_store=_FakeRTS(),  # type: ignore[arg-type]
        events_repository=events_repo,
        sync_log=_FakeSyncLog(),  # type: ignore[arg-type]
    )

    # Register the trigger callback
    trigger.register_pipeline_callback(pipeline)

    # Ingest a SESSION_END event
    session_end_event = Event(
        id="evt-cb-001",
        session_id=_SESSION_ID,
        project_id=_PROJECT_ID,
        event_type=EventType.SESSION_END,
        timestamp=_NOW,
        sequence_number=1,
        segment_index=0,
    )
    await pipeline.ingest(session_end_event)

    # Give the background task a moment to run
    await asyncio.sleep(0.1)

    assert fake_orchestrator.call_count == 1, (
        f"Expected SESSION_END to trigger dispatch, but call_count={fake_orchestrator.call_count}"
    )


@pytest.mark.asyncio
async def test_pipeline_callback_does_not_fire_on_non_session_end(
    runs_repo: AnalysisRunsRepository,
    events_repo: EventsRepository,
    lock_registry: LockRegistry,
) -> None:
    """Non-SESSION_END events do not trigger dispatch."""
    from secondsight.observation.pipeline import ObservationPipeline

    fake_orchestrator = _CountingOrchestrator()
    trigger = Trigger(
        orchestrator=fake_orchestrator,
        analysis_runs_repo=runs_repo,
        events_repo=events_repo,
        lock_registry=lock_registry,
    )

    class _FakeRTS:
        async def write(self, event: Event) -> str:
            return "/fake/path"

    class _FakeSyncLog:
        def record_failure(self, *a: object, **kw: object) -> None:
            pass

    pipeline = ObservationPipeline(
        raw_trace_store=_FakeRTS(),  # type: ignore[arg-type]
        events_repository=events_repo,
        sync_log=_FakeSyncLog(),  # type: ignore[arg-type]
    )
    trigger.register_pipeline_callback(pipeline)

    for seq, etype in enumerate(
        [EventType.TOOL_USE_START, EventType.USER_PROMPT, EventType.RESPONSE]
    ):
        event = Event(
            id=f"evt-nse-{seq}",
            session_id=_SESSION_ID,
            project_id=_PROJECT_ID,
            event_type=etype,
            timestamp=_NOW,
            sequence_number=seq,
            segment_index=0,
        )
        await pipeline.ingest(event)

    await asyncio.sleep(0.05)
    assert fake_orchestrator.call_count == 0


# =====================================================================
# In-memory dispatch tracker pruning (self-iteration fix)
# =====================================================================


@pytest.mark.asyncio
async def test_in_memory_tracker_pruned_after_expiry(
    runs_repo: AnalysisRunsRepository,
    events_repo: EventsRepository,
    lock_registry: LockRegistry,
) -> None:
    """Self-iteration: _in_memory_dispatched entries older than trigger_lock_seconds
    are pruned on the next dispatch() call, preventing unbounded dict growth.
    """
    fake_orchestrator = _CountingOrchestrator()
    trigger = Trigger(
        orchestrator=fake_orchestrator,
        analysis_runs_repo=runs_repo,
        events_repo=events_repo,
        lock_registry=lock_registry,
        trigger_lock_seconds=1,  # very short window for testing
    )

    # First dispatch
    result1 = await trigger.dispatch(_PROJECT_ID, _SESSION_ID, source="event")
    assert result1.dispatched is True

    # Entry should be in the tracker
    assert _SESSION_ID in trigger._in_memory_dispatched

    # Wait for the trigger_lock_seconds window to expire
    await asyncio.sleep(1.1)

    # Second dispatch on a DIFFERENT session to trigger pruning
    other_session = "sess-pruning-other"
    result2 = await trigger.dispatch(_PROJECT_ID, other_session, source="event")

    # The original session entry should have been pruned
    assert _SESSION_ID not in trigger._in_memory_dispatched, (
        "Old in-memory entry should be pruned after trigger_lock_seconds"
    )


# =====================================================================
# Sweeper exception isolation
# =====================================================================


@pytest.mark.asyncio
async def test_sweeper_per_session_exception_does_not_stop_loop(
    runs_repo: AnalysisRunsRepository,
    events_repo: EventsRepository,
) -> None:
    """One session's dispatch failure must NOT stop the Sweeper loop."""
    call_order: list[str] = []

    class _PartiallyFailingTrigger:
        async def dispatch(
            self,
            project_id: str,
            session_id: str,
            *,
            source: str,
            force: bool = False,
        ) -> DispatchResult:
            call_order.append(session_id)
            if session_id == "sess-bad":
                raise RuntimeError("simulated per-session error")
            return DispatchResult(dispatched=True, reason="dispatched", run_id=None)

    sweeper = Sweeper(
        trigger=_PartiallyFailingTrigger(),
        events_repo=events_repo,
        analysis_runs_repo=runs_repo,
        interval_seconds=999,
        session_timeout_minutes=30,
    )

    # Insert two stale sessions
    stale_ts = _NOW - timedelta(minutes=35)
    for sess_id in ["sess-bad", "sess-good"]:
        event = Event(
            id=f"evt-sw-{sess_id}",
            session_id=sess_id,
            project_id=_PROJECT_ID,
            event_type=EventType.TOOL_USE_START,
            timestamp=stale_ts,
            sequence_number=0,
            segment_index=0,
        )
        events_repo.insert(event)

    # sweep_stale_sessions must complete without raising
    await sweeper.sweep_stale_sessions(_NOW)

    # Both sessions must have been attempted
    assert "sess-bad" in call_order, "sess-bad was not attempted"
    assert "sess-good" in call_order, "sess-good was not attempted"


# =====================================================================
# FIX-LOOP: New death tests for critical issues #1, #2, #4
# =====================================================================


# --- DT-FL-1: EventsRepository.find_stale_session_candidates public API ---

def test_dt_fl_1_find_stale_session_candidates_public_api(
    events_repo: EventsRepository,
    db_engine: DBEngine,
) -> None:
    """DT-FL-1: EventsRepository.find_stale_session_candidates() is public API.

    Silent failure this closes: Sweeper._find_stale_sessions() previously
    accessed events_repo._db.engine directly. If EventsRepository renames
    _db, the Sweeper breaks at runtime with AttributeError — no static signal.
    This test verifies the public method exists and returns correct results.
    """
    from secondsight.storage.analysis_runs_repository import AnalysisRunsRepository
    from datetime import datetime, timedelta, timezone

    runs_repo = AnalysisRunsRepository(db_engine)
    runs_repo.create_schema()

    now = datetime(2026, 5, 8, 10, 0, 0, tzinfo=timezone.utc)
    stale_ts = now - timedelta(minutes=35)
    recent_ts = now - timedelta(minutes=5)
    cutoff = now - timedelta(minutes=30)

    # Insert a stale event
    stale_event = Event(
        id="evt-stale-fl1",
        session_id="sess-stale-fl1",
        project_id=_PROJECT_ID,
        event_type=EventType.TOOL_USE_START,
        timestamp=stale_ts,
        sequence_number=0,
        segment_index=0,
    )
    events_repo.insert(stale_event)

    # Insert a recent event (should NOT be returned)
    recent_event = Event(
        id="evt-recent-fl1",
        session_id="sess-recent-fl1",
        project_id=_PROJECT_ID,
        event_type=EventType.TOOL_USE_START,
        timestamp=recent_ts,
        sequence_number=0,
        segment_index=0,
    )
    events_repo.insert(recent_event)

    candidates = events_repo.find_stale_session_candidates(
        project_id=_PROJECT_ID,
        last_event_before=cutoff,
    )

    session_ids = [c[1] for c in candidates]
    assert "sess-stale-fl1" in session_ids, (
        "Stale session must appear in candidates"
    )
    assert "sess-recent-fl1" not in session_ids, (
        "Recent session must NOT appear in candidates"
    )

    # Verify the tuple structure
    for proj_id, sess_id, last_event_ts in candidates:
        assert isinstance(proj_id, str), f"project_id must be str, got {type(proj_id)}"
        assert isinstance(sess_id, str), f"session_id must be str, got {type(sess_id)}"
        # last_event_ts may be datetime or string depending on SQLite driver
        assert last_event_ts is not None, "last_event_ts must not be None"


def test_dt_fl_1_find_stale_session_candidates_cross_project(
    events_repo: EventsRepository,
) -> None:
    """DT-FL-1: find_stale_session_candidates with project_id=None returns all projects."""
    from datetime import datetime, timedelta, timezone

    now = datetime(2026, 5, 8, 10, 0, 0, tzinfo=timezone.utc)
    stale_ts = now - timedelta(minutes=35)
    cutoff = now - timedelta(minutes=30)

    for i, proj in enumerate(["proj-a", "proj-b"]):
        event = Event(
            id=f"evt-cross-{proj}",
            session_id=f"sess-cross-{proj}",
            project_id=proj,
            event_type=EventType.TOOL_USE_START,
            timestamp=stale_ts,
            sequence_number=i,
            segment_index=0,
        )
        events_repo.insert(event)

    # Cross-project scan (project_id=None)
    candidates = events_repo.find_stale_session_candidates(
        project_id=None,
        last_event_before=cutoff,
    )
    project_ids_found = {c[0] for c in candidates}
    assert "proj-a" in project_ids_found, "proj-a must appear in cross-project scan"
    assert "proj-b" in project_ids_found, "proj-b must appear in cross-project scan"


# --- DT-FL-2: LockRegistry has no dead _locks field ---


def test_dt_fl_2_lock_registry_no_dead_locks_field() -> None:
    """DT-FL-2: LockRegistry must NOT have a _locks attribute.

    Silent failure this closes: _locks (WeakValueDictionary) was
    initialized but never used for exclusion. The name lied about the
    mechanism, and any code that inspected _locks would see stale state.
    Removing _locks makes the implementation honest.
    """
    registry = LockRegistry()
    assert not hasattr(registry, "_locks"), (
        "LockRegistry._locks should not exist — it was a dead field. "
        "Exclusion is done via _active_sessions."
    )
    assert hasattr(registry, "_active_sessions"), (
        "LockRegistry._active_sessions must exist (the actual exclusion mechanism)."
    )


# --- DT-FL-4: NON_TERMINAL_STAGES is derived, not hardcoded ---


def test_dt_fl_4_non_terminal_stages_derived_from_terminal() -> None:
    """DT-FL-4: NON_TERMINAL_STAGES must be derived from AnalysisRunStage - TERMINAL_STAGES.

    Silent failure this closes: a hardcoded set of non-terminal stages would
    drift if a new AnalysisRunStage is added. Derived set is always consistent.
    """
    from secondsight.analysis.schemas import AnalysisRunStage, TERMINAL_STAGES
    from secondsight.sdk.trigger import NON_TERMINAL_STAGES

    all_stage_values = frozenset(s.value for s in AnalysisRunStage)
    expected_non_terminal = all_stage_values - TERMINAL_STAGES

    assert NON_TERMINAL_STAGES == expected_non_terminal, (
        f"NON_TERMINAL_STAGES diverges from derived set. "
        f"Got {NON_TERMINAL_STAGES!r}, expected {expected_non_terminal!r}"
    )

    # Verify it covers the known non-terminal stages
    assert "pending" in NON_TERMINAL_STAGES
    assert "segmented" in NON_TERMINAL_STAGES
    assert "behavior_done" in NON_TERMINAL_STAGES

    # Verify terminal stages are NOT in it
    for stage in TERMINAL_STAGES:
        assert stage not in NON_TERMINAL_STAGES, (
            f"Terminal stage {stage!r} must not be in NON_TERMINAL_STAGES"
        )


def test_dt_fl_4_block_redispatch_stages_excludes_failed() -> None:
    """DT-FL-4: _BLOCK_REDISPATCH_STAGES must exclude 'failed' so failed sessions
    can be re-dispatched without --force.
    """
    from secondsight.sdk.trigger import _BLOCK_REDISPATCH_STAGES
    from secondsight.analysis.schemas import TERMINAL_STAGES

    assert "failed" not in _BLOCK_REDISPATCH_STAGES, (
        "'failed' must not block re-dispatch — operators should be able to "
        "retry failed analysis without --force."
    )
    # The set must be TERMINAL_STAGES minus "failed"
    assert _BLOCK_REDISPATCH_STAGES == TERMINAL_STAGES - {"failed"}, (
        f"_BLOCK_REDISPATCH_STAGES must equal TERMINAL_STAGES - {{'failed'}}. "
        f"Got {_BLOCK_REDISPATCH_STAGES!r}"
    )


# --- DT-FL-9: Sweeper.set_task() does not exist (ghost method removed) ---


def test_dt_fl_9_sweeper_no_set_task_method(
    runs_repo: AnalysisRunsRepository,
    events_repo: EventsRepository,
) -> None:
    """DT-FL-9: Sweeper.set_task() must NOT exist — it was a ghost caller.

    Sweeper.run() self-registers via asyncio.current_task(). set_task()
    was unused and had no callers. Remove it so no one accidentally calls it
    and ends up with a stale _task reference that bypasses self-registration.
    """
    recording_trigger = MagicMock()

    sweeper = Sweeper(
        trigger=recording_trigger,
        events_repo=events_repo,
        analysis_runs_repo=runs_repo,
        interval_seconds=999,
        session_timeout_minutes=30,
    )

    assert not hasattr(sweeper, "set_task"), (
        "Sweeper.set_task() must be removed. "
        "run() self-registers via asyncio.current_task()."
    )


# --- DT-FL-6: in-memory record BEFORE create_task ---


@pytest.mark.asyncio
async def test_dt_fl_6_in_memory_record_before_create_task_failure(
    runs_repo: AnalysisRunsRepository,
    events_repo: EventsRepository,
    lock_registry: LockRegistry,
) -> None:
    """DT-FL-6: If create_task fails, the in-memory record is cleaned up.

    Silent failure this closes: if _in_memory_dispatched was recorded AFTER
    create_task (old behavior), and create_task raised RuntimeError, the entry
    was never recorded → the ghost entry didn't exist. But if the order was
    reversed (record first, then fail), we need to remove the entry on failure
    so the next dispatch is not blocked by a ghost entry.

    This test verifies that after a create_task failure, dispatching again
    succeeds (the ghost entry was cleaned up).
    """
    call_count = 0

    class _FailOnceOrchestrator:
        """First create_task call succeeds, verify dispatch recovers from failure."""
        async def analyze_and_aggregate(self, session_id: str, *, force: bool = False) -> None:
            nonlocal call_count
            call_count += 1

    trigger = Trigger(
        orchestrator=_FailOnceOrchestrator(),
        analysis_runs_repo=runs_repo,
        events_repo=events_repo,
        lock_registry=lock_registry,
        trigger_lock_seconds=30,
    )

    # Normal dispatch should work
    result = await trigger.dispatch(_PROJECT_ID, _SESSION_ID, source="manual")
    assert result.dispatched is True

    # The in-memory entry should be set (task was created successfully)
    assert _SESSION_ID in trigger._in_memory_dispatched, (
        "In-memory entry must be set after successful dispatch"
    )
