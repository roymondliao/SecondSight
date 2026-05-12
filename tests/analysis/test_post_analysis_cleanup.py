"""Death tests for PostAnalysisCleanupTrigger — task-B4 of GUR-149.

Samsara discipline: death tests first.

Death cases (from changes/2026-05-07_gur-149_analysis-ttl-and-post-analysis-trigger/2-plan.md §3):

    DC-B5: Per-session eager purge races with scheduled CLI cleanup.
           Trigger must be idempotent — calling it twice on the same
           session_id (or on a session whose raw_traces are already
           reaped) returns cleanly without raising. Relies on GUR-147
           `_delete_fs_session` returning False on absent dir + DB delete
           returning rowcount 0 cleanly.

    DC-B4: covered in task-B6 (factory wiring) — pinned by a separate
           assertion that the factory raises/warns if the operator
           configures cleanup_after_analysis=true but the orchestrator
           wiring drops the trigger. Within task-B4 scope we test the
           trigger itself: when invoked, it must produce observable side
           effects (purger called) when enabled, and it must NOT raise
           even when the underlying purger reports failures.

LOAD-BEARING NOTE (gap-fs-collision, 2-plan.md D5):
    When the trigger fires, RawTracesPurger.purge() shutil.rmtree's the
    entire `{home}/projects/{pid}/sessions/{sid}/` directory — which
    INCLUDES the orchestrator's session_report.json FS backup. The DB
    row in session_reports remains authoritative; tools that consume
    the FS backup must fall back to the DB after eager cleanup. The
    structured INFO log line below names this side effect explicitly so
    operators reading cleanup logs can correlate the two effects.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from secondsight.analysis.post_analysis_cleanup import PostAnalysisCleanupTrigger
from secondsight.event import Event, EventType
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.events_repository import EventsRepository
from secondsight.storage.raw_trace_store import RawTraceStore
from secondsight.storage.retention import (
    PurgeFailure,
    PurgeResult,
    RawTracesPurger,
)

UTC = timezone.utc
NOW = datetime(2026, 5, 7, 14, 0, 0, tzinfo=UTC)

_PROJECT_ID = "proj-trigger-test"


# ----------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------


@pytest.fixture
def project_dir(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    return p


@pytest.fixture
def events_repo(project_dir: Path) -> EventsRepository:
    db = DBEngine(db_path=project_dir / "intel.db")
    r = EventsRepository(db_engine=db)
    r.create_schema()
    return r


@pytest.fixture
def raw_trace_store(project_dir: Path) -> RawTraceStore:
    return RawTraceStore(project_root=project_dir)


@pytest.fixture
def real_purger(events_repo: EventsRepository, raw_trace_store: RawTraceStore) -> RawTracesPurger:
    return RawTracesPurger(repo=events_repo, raw_trace_store=raw_trace_store)


def _seed_session(
    events_repo: EventsRepository,
    raw_trace_store: RawTraceStore,
    *,
    session_id: str,
    last_ts: datetime = NOW,
) -> None:
    """Seed one session with one start + one end event in DB and FS."""
    events = [
        Event(
            id=f"evt-{session_id}-start",
            session_id=session_id,
            project_id=_PROJECT_ID,
            event_type=EventType.SESSION_START,
            timestamp=last_ts - timedelta(seconds=10),
            sequence_number=0,
            segment_index=0,
        ),
        Event(
            id=f"evt-{session_id}-end",
            session_id=session_id,
            project_id=_PROJECT_ID,
            event_type=EventType.SESSION_END,
            timestamp=last_ts,
            sequence_number=1,
            segment_index=0,
        ),
    ]
    for e in events:
        events_repo.insert(e)
        # Also write to FS via the store so the purger has files to remove.
        # Use asyncio.run (Python 3.14 — get_event_loop is gone outside coroutines).
        asyncio.run(raw_trace_store.write(e))


# ======================================================================
# Disabled path (cleanup_after_analysis=False)
# ======================================================================


class TestDisabledPath:
    """When `cleanup_after_analysis=False` the trigger MUST be a no-op.
    No DB read, no purger invocation, no exception. Structured INFO log
    so operators reading the trail know the trigger fired but skipped.

    Silent-failure case this guards: if the trigger silently invoked the
    purger anyway, the operator's `cleanup_after_analysis = false` config
    would be ignored — exactly the inverse of DC-B1's typo case at the
    config layer."""

    def test_disabled_trigger_does_not_invoke_purger(
        self,
        events_repo: EventsRepository,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        purge_calls: list[list] = []

        class SpyPurger:
            def purge(self, expired):  # type: ignore[no-untyped-def]
                purge_calls.append(list(expired))
                return PurgeResult(purged_session_ids=(), failures=())

        trigger = PostAnalysisCleanupTrigger(
            cleanup_after_analysis=False,
            raw_traces_purger=SpyPurger(),
            events_repo=events_repo,
        )

        with caplog.at_level(logging.INFO):
            trigger("sess-anything")

        assert purge_calls == [], "Purger MUST NOT be invoked when cleanup_after_analysis=False"
        # Structured INFO line names the disabled state.
        assert any(
            "cleanup_after_analysis=False" in r.getMessage()
            or "cleanup_after_analysis=false" in r.getMessage()
            for r in caplog.records
        )


# ======================================================================
# DC-B5: idempotent re-invocation
# ======================================================================


class TestDCB5IdempotentReInvocation:
    """Calling the trigger twice on the same session_id, OR on a session
    whose raw_traces are already gone, must return cleanly. The trigger
    relies on GUR-147's `_delete_fs_session` returning False on absent
    dirs and `_delete_db_events_for_session` returning rowcount 0 — both
    pinned by the GUR-147 test suite."""

    def test_trigger_on_already_purged_session_returns_cleanly(
        self,
        events_repo: EventsRepository,
        raw_trace_store: RawTraceStore,
        real_purger: RawTracesPurger,
    ) -> None:
        # No session seeded — trigger fires for a session that doesn't exist.
        trigger = PostAnalysisCleanupTrigger(
            cleanup_after_analysis=True,
            raw_traces_purger=real_purger,
            events_repo=events_repo,
        )
        # Must not raise.
        trigger("sess-never-existed")

    def test_trigger_called_twice_in_a_row_is_idempotent(
        self,
        events_repo: EventsRepository,
        raw_trace_store: RawTraceStore,
        real_purger: RawTracesPurger,
    ) -> None:
        """Yin review fix: this test must distinguish 'clean second
        call' from 'second call silently absorbed an FS failure via
        the purger's per-row try/except'. Wrap the real purger with a
        spy that records each PurgeResult; assert had_failures is False
        on the FIRST call (session present, reap succeeds) AND verify
        the SECOND call short-circuits at the no-events guard before
        the purger is invoked at all (DC-B5 idempotency).
        """
        _seed_session(
            events_repo,
            raw_trace_store,
            session_id="sess-twice",
        )

        purge_results: list[PurgeResult] = []

        class RecordingPurger:
            """Wraps real_purger; records each PurgeResult for assertion."""

            def __init__(self, inner: RawTracesPurger) -> None:
                self._inner = inner

            def purge(self, expired):  # type: ignore[no-untyped-def]
                result = self._inner.purge(expired)
                purge_results.append(result)
                return result

        trigger = PostAnalysisCleanupTrigger(
            cleanup_after_analysis=True,
            raw_traces_purger=RecordingPurger(real_purger),
            events_repo=events_repo,
        )

        # First call: session present → trigger calls purge → reap succeeds.
        trigger("sess-twice")
        assert len(purge_results) == 1
        assert purge_results[0].had_failures is False, (
            "First reap must complete cleanly (no absorbed FS/DB failures)"
        )
        assert "sess-twice" in purge_results[0].purged_session_ids

        # Second call: events table is now empty for sess-twice → trigger's
        # no-events guard fires → purge is NEVER called. This is the key
        # idempotency contract: the second invocation does not even reach
        # the purger, so it cannot silently absorb a failure.
        trigger("sess-twice")
        assert len(purge_results) == 1, (
            "Second invocation must short-circuit at the no-events guard "
            "BEFORE invoking the purger; the purger absorbing a failure "
            "would be a different (worse) idempotency contract"
        )


# ======================================================================
# Trigger must not raise even when underlying purger reports failures
# ======================================================================


class TestTriggerDoesNotRaiseOnPurgerFailure:
    """If RawTracesPurger.purge() returns had_failures=True, the trigger
    logs WARNING and returns normally. The purger's structured ERROR
    logs are sufficient — the trigger's job is to bridge the orchestrator
    callback to the purger, not to re-implement error escalation.

    This is load-bearing because Orchestrator's _invoke_on_analysis_complete
    swallows trigger exceptions (DC-B3). If the trigger raised on
    purge-failure, the exception would be caught at the orchestrator
    boundary, the analysis would still succeed, but the operator would
    see two ERROR messages (one from the purger, one from the orchestrator
    boundary) for one root cause. The trigger logs WARNING instead of
    raising to keep the failure trail readable."""

    def test_trigger_does_not_raise_when_purge_reports_failures(
        self,
        events_repo: EventsRepository,
        raw_trace_store: RawTraceStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        _seed_session(
            events_repo,
            raw_trace_store,
            session_id="sess-fails",
        )

        class FailingPurger:
            def purge(self, expired):  # type: ignore[no-untyped-def]
                return PurgeResult(
                    purged_session_ids=(),
                    failures=(
                        PurgeFailure(
                            session_id="sess-fails",
                            stage="filesystem",
                            error="simulated FS failure",
                        ),
                    ),
                )

        trigger = PostAnalysisCleanupTrigger(
            cleanup_after_analysis=True,
            raw_traces_purger=FailingPurger(),
            events_repo=events_repo,
        )
        with caplog.at_level(logging.WARNING):
            # Must not raise.
            trigger("sess-fails")

        # WARNING line names the session_id and points to RawTracesPurger logs.
        warnings = [
            r
            for r in caplog.records
            if r.levelno >= logging.WARNING and "sess-fails" in r.getMessage()
        ]
        assert len(warnings) >= 1, (
            f"Expected at least one WARNING for sess-fails, got: "
            f"{[(r.levelno, r.getMessage()) for r in caplog.records]}"
        )


# ======================================================================
# Happy path: trigger eagerly purges raw_traces for one session
# ======================================================================


class TestHappyPath:
    def test_trigger_eagerly_purges_session_when_enabled(
        self,
        events_repo: EventsRepository,
        raw_trace_store: RawTraceStore,
        real_purger: RawTracesPurger,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """B-H3 (partial): post-analysis trigger eagerly removes the
        session's events from DB AND its FS dir. The session_report.json
        FS backup (if present) is intentionally also removed per
        gap-fs-collision."""
        sid = "sess-eager"
        _seed_session(events_repo, raw_trace_store, session_id=sid)

        # Sanity: pre-trigger state has events in DB.
        assert len(events_repo.get_session_events(sid)) >= 1

        trigger = PostAnalysisCleanupTrigger(
            cleanup_after_analysis=True,
            raw_traces_purger=real_purger,
            events_repo=events_repo,
        )
        with caplog.at_level(logging.INFO):
            trigger(sid)

        # Events gone from DB.
        assert events_repo.get_session_events(sid) == []
        # FS dir gone.
        session_dir = raw_trace_store.project_root / "sessions" / sid
        assert not session_dir.exists()

        # Structured INFO line names the session AND warns that the
        # session_report.json backup is also gone (gap-fs-collision).
        info_messages = [r.getMessage() for r in caplog.records]
        assert any(sid in m for m in info_messages)
        assert any("session_report.json" in m or "DB row remains" in m for m in info_messages), (
            f"Expected gap-fs-collision disclosure in INFO logs, got: {info_messages}"
        )

    def test_trigger_uses_actual_last_event_timestamp(
        self,
        events_repo: EventsRepository,
        raw_trace_store: RawTraceStore,
    ) -> None:
        """The synthesized ExpiredSession.last_event_at must reflect the
        actual session's last-event timestamp, not wall-clock now. This
        is verified by spying on the purger and asserting the value."""
        sid = "sess-with-timestamp"
        _seed_session(
            events_repo,
            raw_trace_store,
            session_id=sid,
            last_ts=NOW,
        )

        captured = {}

        class CapturingPurger:
            def purge(self, expired):  # type: ignore[no-untyped-def]
                captured["expired"] = list(expired)
                return PurgeResult(
                    purged_session_ids=tuple(e.session_id for e in expired),
                    failures=(),
                )

        trigger = PostAnalysisCleanupTrigger(
            cleanup_after_analysis=True,
            raw_traces_purger=CapturingPurger(),
            events_repo=events_repo,
        )
        trigger(sid)

        assert "expired" in captured
        assert len(captured["expired"]) == 1
        es = captured["expired"][0]
        assert es.session_id == sid
        # Last event was inserted with timestamp = NOW.
        # SQLite may return naive datetimes; normalize before comparison.
        last_ts = es.last_event_at
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=UTC)
        assert last_ts == NOW
