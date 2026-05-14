"""Trigger layer — per-session dispatch with lock, dedup, and sweeper (GUR-103 P2-15).

Three trigger paths converge on ``Trigger.dispatch()``:
  1. Event-driven: ``ObservationPipeline`` post-ingest callback fires on SESSION_END.
  2. Timeout-based: ``Sweeper`` sweeps stale sessions on a configurable interval.
  3. Manual: ``cli/analyze.py`` calls dispatch directly (in-process or via API server).

All three paths use the existing ``analysis_runs`` table (GUR-102) for idempotency.
Per-session asyncio lock prevents race-condition duplicate dispatch (DC-4).

Architecture decisions:
  D2 (reuse analysis_runs): No new table. Trigger reads analysis_runs via
  AnalysisRunsRepository.get_latest_for_session to check terminal status.

  D3 (non-blocking ingest): The post-ingest callback uses asyncio.create_task so
  pipeline.ingest() is not blocked by the analysis pipeline.

  D10 (Sweeper lifecycle): Sweeper runs as an asyncio task in the API server's
  lifespan. Manual CLI works without the server; Sweeper-based timeout analysis
  requires ``secondsight serve``.

  D14 (no pre-insert): Trigger never inserts analysis_runs rows. Only the
  orchestrator's start_run() does (DC-1 audit contract).

Death cases closed here:
  DC-4 (LockRegistry): DispatchRegistry tracks active session_ids in a plain set.
  Per-session exclusion is atomic in asyncio (single-threaded cooperative scheduling:
  no other coroutine runs between the membership check and set.add() because there
  is no await between them). The set is bounded by concurrently-dispatching sessions.

  DC-5 (non-blocking): asyncio.create_task is called immediately after lock
  acquisition; the caller returns before the analysis pipeline runs.

  DC-6 (sweeper last_event_ts): Sweeper uses EventsRepository.find_stale_session_candidates()
  to find sessions whose max(timestamp) < now - timeout. A session with recent events
  is not swept even if its analysis_runs row is absent or non-terminal.

Silent failure conditions (documented in scar report):
  - asyncio.create_task failure: if the event loop is shutting down at task
    creation time, create_task raises RuntimeError. Wrapped in try/except
    with ERROR log so the trigger always returns DispatchResult (never raises
    to the pipeline callback).

  - Sweeper exception isolation: per-session exceptions are caught and logged;
    the sweep loop continues for the remaining sessions (verified by
    test_sweeper_per_session_exception_does_not_stop_loop).
"""

from __future__ import annotations

import asyncio
from loguru import logger
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, AsyncGenerator, Literal

from secondsight.analysis.schemas import AnalysisRunStage, TERMINAL_STAGES
from secondsight.event import Event, EventType

if TYPE_CHECKING:
    from secondsight.analysis.orchestrator import Orchestrator
    from secondsight.analysis.runtime import ModeAwareDispatch
    from secondsight.observation.pipeline import ObservationPipeline
    from secondsight.storage.analysis_runs_repository import AnalysisRunsRepository
    from secondsight.storage.events_repository import EventsRepository


def _ensure_utc(dt: datetime) -> datetime:
    """Normalize a datetime to UTC-aware.

    SQLite stores datetimes without timezone info; rows returned by the
    repository may be naive. This function attaches UTC if the datetime
    is naive, or converts to UTC if it carries another timezone.

    Inlined here (rather than imported from orchestrator._ensure_utc) to
    avoid importing a private symbol from another module. Both copies are
    identical; if the logic changes, update both (or promote to a shared
    utility module).
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# Maximum age of a non-terminal analysis_runs row before we treat it as stale
# (not "in-flight" anymore). Used by the in-flight guard only.
_DEFAULT_TRIGGER_LOCK_SECONDS: int = 30

# Sweeper defaults (documented as requiring `secondsight serve` — D10)
_DEFAULT_SWEEPER_INTERVAL_SECONDS: int = 60
_DEFAULT_SESSION_TIMEOUT_MINUTES: int = 30

# Derived from TERMINAL_STAGES: stages where a run is not yet at a definitive end state.
# When a new AnalysisRunStage is added, TERMINAL_STAGES is the single source of truth;
# this set is derived automatically. A hardcoded copy would drift silently on new stages.
NON_TERMINAL_STAGES: frozenset[str] = frozenset(s.value for s in AnalysisRunStage) - TERMINAL_STAGES

# Stages that block re-dispatch (do not allow re-analysis) even without force=True.
# Intentionally excludes "failed": a failed run SHOULD be re-dispatchable so the
# operator can retry without passing --force. Only successful terminal stages block.
_BLOCK_REDISPATCH_STAGES: frozenset[str] = TERMINAL_STAGES - {"failed"}


# ---------------------------------------------------------------------------
# LockRegistry — per-session dispatch registry with active-session-set guard
# ---------------------------------------------------------------------------


class LockRegistry:
    """Per-session dispatch guard backed by a set of active session_ids.

    Design:
    - Tracks which session_ids are currently inside an ``acquire()`` block.
    - Uses a Python set (strong references, no GC surprises).
    - Per-session exclusion is implemented via a set membership check, which
      is atomic in asyncio (single-threaded cooperative scheduling: no other
      coroutine can run between the membership check and the set.add() because
      there is no ``await`` between them).

    Exclusion mechanism (actual):
    - _active_sessions: set[str] tracks currently-dispatching sessions.
    - The in-memory dispatch tracker (_in_memory_dispatched on Trigger) handles
      rapid re-dispatch before the DB has a row.

    WeakValueDictionary (NOT used here):
    - An asyncio.Lock WeakValueDictionary was the original design but was
      replaced with this set-based approach. The WeakValueDictionary approach
      had subtle correctness issues under asyncio's cooperative scheduling.

    Public API:
    - ``acquire(session_id)``: async context manager.
      Yields ``True`` if the slot was acquired (exclusive dispatch path).
      Yields ``False`` if the session is already being dispatched (contention).
      Callers on the False path return ``DispatchResult(dispatched=False, reason="lock-held")``.
    """

    def __init__(self) -> None:
        # Set of session_ids currently inside an acquire() context.
        # Atomically checked and mutated (no await between check and add).
        self._active_sessions: set[str] = set()

    @asynccontextmanager
    async def acquire(self, session_id: str) -> AsyncGenerator[bool, None]:
        """Async context manager. Yields True if acquired, False on contention.

        Non-blocking: if session_id is already in _active_sessions (another
        coroutine is dispatching it), yields False immediately.

        Atomicity guarantee: the `in` check and `add()` happen with no
        intervening ``await``, so no other coroutine can run between them
        in asyncio's single-threaded cooperative model.

        The session_id is removed from _active_sessions in the ``finally``
        block so it is released even if the caller raises inside the
        ``async with`` block.
        """
        if session_id in self._active_sessions:
            # Contention: another coroutine is dispatching this session.
            yield False
            return

        # No contention: mark this session as actively dispatching.
        # This add() happens synchronously (no await) after the check above.
        self._active_sessions.add(session_id)
        try:
            yield True
        finally:
            self._active_sessions.discard(session_id)


# ---------------------------------------------------------------------------
# DispatchResult
# ---------------------------------------------------------------------------


@dataclass
class DispatchResult:
    """Outcome of one Trigger.dispatch() call.

    Fields:
        dispatched: True if orchestrator.analyze_and_aggregate was scheduled.
        reason: Short string describing the outcome. One of:
            "dispatched"          — task scheduled.
            "lock-held"           — another coroutine is dispatching this session.
            "another-run-in-flight" — non-terminal run updated recently.
            "already-analyzed"    — terminal run exists and force=False.
        run_id: The existing run_id from analysis_runs (set on non-dispatch paths).
        existing_stage: Stage of the latest run (set on non-dispatch paths).
        existing_completed_at: completed_at of the latest run, if any.
    """

    dispatched: bool
    reason: str
    run_id: str | None = None
    existing_stage: str | None = None
    existing_completed_at: datetime | None = None


# ---------------------------------------------------------------------------
# Trigger
# ---------------------------------------------------------------------------


class Trigger:
    """Schedules analysis pipeline for a session via asyncio.create_task.

    Three source values:
      "event"   — SESSION_END event received in ObservationPipeline.
      "timeout" — Sweeper detected a stale session.
      "manual"  — CLI or API call from the operator.

    Idempotency contract:
      1. Per-session lock (LockRegistry) prevents concurrent dispatch of the
         same session_id.
      2. analysis_runs.get_latest_for_session() is checked inside the lock.
         If terminal and force=False, returns dispatched=False.
      3. If a non-terminal run exists and updated_at is recent (< trigger_lock_seconds),
         returns dispatched=False reason="another-run-in-flight".
      4. Only if all checks pass: asyncio.create_task(orchestrator.analyze_and_aggregate).

    D14 contract: Trigger NEVER inserts analysis_runs rows. Only orchestrator
    does via start_run() (preserves DC-1 audit trail).
    """

    def __init__(
        self,
        orchestrator: "Orchestrator",
        analysis_runs_repo: "AnalysisRunsRepository",
        events_repo: "EventsRepository",
        lock_registry: "LockRegistry",
        trigger_lock_seconds: int = _DEFAULT_TRIGGER_LOCK_SECONDS,
        mode_aware_dispatch: "ModeAwareDispatch | None" = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._analysis_runs_repo = analysis_runs_repo
        self._events_repo = events_repo
        self._lock_registry = lock_registry
        self._trigger_lock_seconds = trigger_lock_seconds
        # mode_aware_dispatch: when provided, Trigger.dispatch() routes through
        # ModeAwareDispatch.dispatch() instead of the legacy orchestrator path.
        # This is the CRITICAL FIX 1 wiring: Trigger now delegates to
        # ModeAwareDispatch (the single place that reads the dispatch mode from config).
        # When None (legacy backward-compat path), orchestrator.analyze_and_aggregate
        # is called directly via asyncio.create_task (SDK-only path).
        self._mode_aware_dispatch = mode_aware_dispatch
        # In-memory dispatch tracker: maps session_id → monotonic dispatch time.
        # Used to prevent rapid re-dispatch before the orchestrator's start_run()
        # has written an analysis_runs row (which is the DB-based in-flight check).
        # The tracker is a Trigger-instance attribute (not shared across triggers).
        # Entries older than trigger_lock_seconds are treated as stale.
        self._in_memory_dispatched: dict[str, float] = {}
        self._registered_pipeline_ids: set[int] = set()

    async def dispatch(
        self,
        project_id: str,
        session_id: str,
        *,
        source: Literal["event", "timeout", "manual"],
        force: bool = False,
    ) -> DispatchResult:
        """Attempt to schedule analysis for session_id.

        Returns DispatchResult immediately. The actual analysis runs in
        a background asyncio.Task (does not block the caller).

        Args:
            project_id: The project this session belongs to.
            session_id: The session to analyze.
            source: Who triggered this dispatch ("event", "timeout", "manual").
            force: If True, bypasses the already-analyzed idempotency gate.

        Returns:
            DispatchResult with dispatched=True if the task was scheduled,
            or dispatched=False with an appropriate reason.
        """
        async with self._lock_registry.acquire(session_id) as acquired:
            if not acquired:
                logger.debug(
                    f"dispatch: lock-held for session_id={session_id!r} source={source!r} — skipping"
                )
                return DispatchResult(dispatched=False, reason="lock-held", run_id=None)

            # In-memory in-flight check (pre-DB): prevents rapid re-dispatch
            # before the orchestrator's start_run() has written a row.
            # The DB-based check (below) handles the case where start_run()
            # has already written a row; this handles the gap before that.
            now_mono = asyncio.get_running_loop().time()

            # Prune stale entries from the in-memory tracker.
            # This prevents unbounded growth for long-running servers with
            # many unique sessions. Pruning runs on every dispatch call —
            # O(len(_in_memory_dispatched)) — acceptable for v1 where the
            # dict is bounded by concurrent active sessions.
            stale_cutoff = now_mono - self._trigger_lock_seconds
            stale_keys = [k for k, ts in self._in_memory_dispatched.items() if ts < stale_cutoff]
            for k in stale_keys:
                del self._in_memory_dispatched[k]

            last_dispatch_ts = self._in_memory_dispatched.get(session_id)
            if last_dispatch_ts is not None and not force:
                age_s = now_mono - last_dispatch_ts
                if age_s < self._trigger_lock_seconds:
                    logger.info(
                        f"dispatch: in-memory-in-flight session_id={session_id!r} "
                        f"age_s={age_s:.1f} < trigger_lock_seconds={self._trigger_lock_seconds} "
                        f"— skipping"
                    )
                    return DispatchResult(
                        dispatched=False,
                        reason="another-run-in-flight",
                        run_id=None,
                    )

            # Inside the lock: check idempotency via DB.
            latest = self._analysis_runs_repo.get_latest_for_session(session_id)

            if latest is not None:
                stage_val = latest.stage.value

                # Successful terminal stage? Block re-dispatch (already done).
                # Uses _BLOCK_REDISPATCH_STAGES (derived from TERMINAL_STAGES minus "failed")
                # so "failed" runs remain re-dispatchable without --force.
                if stage_val in _BLOCK_REDISPATCH_STAGES and not force:
                    logger.info(
                        f"dispatch: already-analyzed session_id={session_id!r} "
                        f"stage={stage_val!r} source={source!r} "
                        f"— returning dispatched=False"
                    )
                    return DispatchResult(
                        dispatched=False,
                        reason="already-analyzed",
                        run_id=latest.id,
                        existing_stage=stage_val,
                        existing_completed_at=latest.completed_at,
                    )

                # Non-terminal run updated recently? Another run may be in flight.
                # NON_TERMINAL_STAGES is derived from AnalysisRunStage - TERMINAL_STAGES,
                # so new stages added to the enum are automatically covered.
                if stage_val in NON_TERMINAL_STAGES:
                    now = datetime.now(tz=timezone.utc)
                    updated_at_utc = _ensure_utc(latest.updated_at)
                    age_seconds = (now - updated_at_utc).total_seconds()
                    if age_seconds < self._trigger_lock_seconds:
                        logger.info(
                            f"dispatch: another-run-in-flight session_id={session_id!r} "
                            f"stage={stage_val!r} age_seconds={age_seconds:.1f} "
                            f"< trigger_lock_seconds={self._trigger_lock_seconds} — skipping"
                        )
                        return DispatchResult(
                            dispatched=False,
                            reason="another-run-in-flight",
                            run_id=latest.id,
                            existing_stage=stage_val,
                        )

            # All checks passed: record in-memory dispatch BEFORE scheduling.
            # If scheduling fails, we remove the entry in the except block
            # so the next dispatch attempt is not blocked by a ghost entry.
            self._in_memory_dispatched[session_id] = asyncio.get_running_loop().time()

            # Route through ModeAwareDispatch if wired (CRITICAL FIX 1).
            # When mode_aware_dispatch is provided, it is the SINGLE place that
            # reads the dispatch mode from config and routes to the appropriate
            # dispatcher (CLI or SDK). The orchestrator path (legacy SDK-only) is
            # used only for backward compat when mode_aware_dispatch=None.
            if self._mode_aware_dispatch is not None:
                # Mode-aware path: dispatch synchronously (ModeAwareDispatch handles
                # its own DC10 lock internally). The Trigger's idempotency checks
                # above (analysis_runs DB check + in-memory tracker) still guard
                # against re-dispatch; ModeAwareDispatch adds per-session lock as
                # defense-in-depth for concurrent callers.
                try:
                    await self._mode_aware_dispatch.dispatch(
                        session_id,
                        project_id=project_id,
                    )
                except Exception as exc:
                    self._in_memory_dispatched.pop(session_id, None)
                    logger.error(
                        f"dispatch: mode_aware_dispatch.dispatch() failed for "
                        f"session_id={session_id!r} source={source!r}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    return DispatchResult(
                        dispatched=False,
                        reason=f"dispatch-error: {exc}",
                        run_id=None,
                    )
            else:
                # Legacy path: schedule the orchestrator (SDK-only) as a background task.
                # Used when mode_aware_dispatch=None (backward compat or tests that
                # construct Trigger without the new parameter).
                try:
                    task = asyncio.create_task(
                        self._orchestrator.analyze_and_aggregate(session_id, force=force),
                        name=f"analyze-{session_id}",
                    )
                    # Add error logging as a done_callback so exceptions in the
                    # background task are not silently swallowed.
                    task.add_done_callback(lambda t: self._on_task_done(t, session_id, source))
                except RuntimeError as exc:
                    # create_task can fail if the event loop is closing.
                    # Remove the in-memory entry so the next dispatch attempt is not
                    # blocked by this ghost entry (it was recorded before create_task).
                    self._in_memory_dispatched.pop(session_id, None)
                    logger.error(
                        f"dispatch: create_task failed for session_id={session_id!r} "
                        f"source={source!r}: {exc}"
                    )
                    return DispatchResult(
                        dispatched=False,
                        reason=f"create-task-error: {exc}",
                        run_id=None,
                    )

            logger.info(
                f"dispatch: scheduled analysis source={source!r} "
                f"session_id={session_id!r} project_id={project_id!r}"
            )
            return DispatchResult(dispatched=True, reason="dispatched", run_id=None)

    @staticmethod
    def _on_task_done(
        task: asyncio.Task,  # type: ignore[type-arg]
        session_id: str,
        source: str,
    ) -> None:
        """Done callback: log if the background analysis task raised."""
        if task.cancelled():
            logger.warning(
                f"dispatch: analysis task cancelled for session_id={session_id!r} source={source!r}"
            )
        elif task.exception() is not None:
            exc = task.exception()
            logger.error(
                f"dispatch: analysis task raised for session_id={session_id!r} "
                f"source={source!r}: {type(exc).__name__}: {exc}"
            )

    def register_pipeline_callback(self, pipeline: "ObservationPipeline") -> None:
        """Register a SESSION_END callback on the given ObservationPipeline.

        The callback fires after the DB write succeeds in pipeline.ingest().
        Non-SESSION_END events are filtered out at the callback level.

        The pipeline's add_post_ingest_callback() method accepts an async
        callable ``(event: Event) -> None``.

        This is the wiring point for the event-driven trigger path (D3):
        ingest() stays unblocked because the callback uses asyncio.create_task.
        """
        pipeline_id = id(pipeline)
        if pipeline_id in self._registered_pipeline_ids:
            logger.debug(
                f"register_pipeline_callback: pipeline_id={pipeline_id!r} "
                f"already registered; skipping"
            )
            return
        pipeline.add_post_ingest_callback(self._pipeline_callback)
        self._registered_pipeline_ids.add(pipeline_id)
        logger.info(
            f"register_pipeline_callback: registered SESSION_END callback "
            f"for pipeline_id={pipeline_id!r}"
        )

    async def _pipeline_callback(self, event: Event) -> None:
        """Called by ObservationPipeline after each DB-write-succeeded ingest.

        Filters to SESSION_END only, then dispatches (non-blocking via create_task
        inside dispatch()).
        """
        if event.event_type != EventType.SESSION_END:
            return

        logger.debug(
            f"_pipeline_callback: SESSION_END received for "
            f"session_id={event.session_id!r} project_id={event.project_id!r}"
        )
        result = await self.dispatch(
            event.project_id,
            event.session_id,
            source="event",
        )
        logger.info(
            f"_pipeline_callback: source='event' session_id={event.session_id!r} "
            f"project_id={event.project_id!r} outcome={result.reason!r}"
        )


# ---------------------------------------------------------------------------
# Sweeper — timeout-based session sweep
# ---------------------------------------------------------------------------


class Sweeper:
    """Periodic sweep for sessions whose last_event_ts exceeds the timeout.

    Design (D10): Sweeper runs as an asyncio task in the API server's lifespan
    (``api/server.py``). Users who run without ``secondsight serve`` get no
    automatic sweep — only manual CLI triggers work.

    Query contract (DC-6):
    - Finds sessions where max(events.timestamp) < now - session_timeout_minutes
      AND no terminal analysis_run exists (summary_written or aggregated).
    - Uses max(events.timestamp) as last_event_ts, NOT events.created_at or
      analysis_runs.started_at. A session with recent events is not swept even if
      it was started long ago.

    Exception isolation:
    - Per-session dispatch errors are caught and logged.
    - One session's failure NEVER stops the sweep loop.

    cancel() contract:
    - Idempotent: calling cancel() multiple times is safe.
    - Awaits the background task with a bounded timeout to ensure cleanup.
    """

    def __init__(
        self,
        trigger: "Trigger",
        events_repo: "EventsRepository",
        analysis_runs_repo: "AnalysisRunsRepository",
        interval_seconds: int = _DEFAULT_SWEEPER_INTERVAL_SECONDS,
        session_timeout_minutes: int = _DEFAULT_SESSION_TIMEOUT_MINUTES,
        project_id_filter: str | None = None,
    ) -> None:
        self._trigger = trigger
        self._events_repo = events_repo
        self._analysis_runs_repo = analysis_runs_repo
        self._interval_seconds = interval_seconds
        self._session_timeout_minutes = session_timeout_minutes
        # Optional project filter. None = scan all projects.
        # Set to a specific project_id for per-project Sweeper instances.
        self._project_id_filter: str | None = project_id_filter
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]
        self._cancelled = False

    async def run(self) -> None:
        """Main sweep loop. Run as an asyncio.Task from the server lifespan.

        Each tick:
          1. Compute now.
          2. Find stale sessions without terminal analysis.
          3. Dispatch each (catching per-session errors).
          4. Sleep interval_seconds.

        Loop exits when cancelled (asyncio.CancelledError propagates).

        Self-registers the current task so cancel() can await it without
        requiring the caller to call set_task() explicitly.
        """
        # Self-register: store the current task so cancel() can await it.
        self._task = asyncio.current_task()
        logger.info(
            f"Sweeper started: interval={self._interval_seconds}s, "
            f"timeout={self._session_timeout_minutes}min"
        )
        while True:
            try:
                now = datetime.now(tz=timezone.utc)
                await self.sweep_stale_sessions(now)
            except asyncio.CancelledError:
                logger.info("Sweeper cancelled during sweep.")
                raise
            except Exception as exc:
                logger.error(f"Sweeper sweep loop error (non-session): {type(exc).__name__}: {exc}")

            try:
                await asyncio.sleep(self._interval_seconds)
            except asyncio.CancelledError:
                logger.info("Sweeper cancelled during sleep.")
                raise

    async def sweep_stale_sessions(self, now: datetime) -> None:
        """One sweep tick: find + dispatch stale sessions.

        Separated from run() so tests can call it directly without
        the while-True loop or sleep.

        Args:
            now: The current UTC datetime (passed in so tests can freeze time).
        """
        stale = self._find_stale_sessions(now)
        logger.debug(f"Sweeper: found {len(stale)} stale session(s) at {now.isoformat()}")

        for project_id, session_id in stale:
            try:
                result = await self._trigger.dispatch(
                    project_id,
                    session_id,
                    source="timeout",
                )
                if result.dispatched:
                    logger.info(
                        f"Sweeper dispatched session_id={session_id!r} project_id={project_id!r}"
                    )
                else:
                    logger.debug(
                        f"Sweeper skipped session_id={session_id!r} reason={result.reason!r}"
                    )
            except Exception as exc:
                # Per-session exception isolation: log and continue.
                logger.error(
                    f"Sweeper error for session_id={session_id!r} "
                    f"project_id={project_id!r}: {type(exc).__name__}: {exc}"
                )

    def _find_stale_sessions(self, now: datetime) -> list[tuple[str, str]]:
        """Query sessions where last_event_ts < now - timeout AND no terminal run.

        Returns list of (project_id, session_id) tuples.

        Uses EventsRepository.find_stale_session_candidates() (public API) for the
        last-event timestamp query, then filters out sessions with a terminal
        analysis_runs row. The two-step approach avoids a compound cross-repository
        query and keeps orchestration concern (what constitutes "done") in the
        Sweeper where it belongs.

        Note: the analysis_runs stage filter is done in a second pass (Python-side)
        rather than a SQL JOIN. For v1 session counts this is acceptable.
        """
        cutoff = now - timedelta(minutes=self._session_timeout_minutes)

        # Step 1: Find candidate sessions with stale last_event_ts via public API.
        # project_id=None → cross-project scan (for when Sweeper covers all projects).
        candidates = self._events_repo.find_stale_session_candidates(
            project_id=self._project_id_filter,
            last_event_before=cutoff,
        )

        # Step 2: Filter out sessions that already have a terminal analysis_runs row.
        result = []
        for project_id, session_id, _last_event_ts in candidates:
            latest = self._analysis_runs_repo.get_latest_for_session(session_id)
            if latest is not None and latest.stage.value in TERMINAL_STAGES:
                logger.debug(
                    f"_find_stale_sessions: session_id={session_id!r} "
                    f"already has terminal run stage={latest.stage.value!r} — skipping"
                )
                continue
            result.append((project_id, session_id))

        return result

    async def cancel(self) -> None:
        """Cancel the sweep loop task. Idempotent.

        Cancels the asyncio.Task if it was started (via run()), then awaits
        it with a bounded timeout. Safe to call multiple times.

        The _task is self-registered by run() via asyncio.current_task().
        If run() has not been awaited yet, _task is None and cancel() is a no-op.
        """
        if self._cancelled:
            return
        self._cancelled = True

        task = self._task
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except asyncio.TimeoutError, asyncio.CancelledError:
                pass


__all__ = [
    "DispatchResult",
    "LockRegistry",
    "NON_TERMINAL_STAGES",
    "Sweeper",
    "Trigger",
]
