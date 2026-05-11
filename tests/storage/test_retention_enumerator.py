"""Death tests for enumerate_expired_sessions — task-A2 of GUR-147.

Samsara discipline: death tests first.

Death cases (from changes/2026-05-06_gur-107_phase3a-retention-observation-api/2-plan.md §5):
    DC-1: A project with zero events returns an empty list. No spurious
          deletions on fresh installs (and no DB query path that
          accidentally reads `MAX(NULL)` and reaps everything).
    DC-2: A session whose `last_event_at` is younger than the TTL
          boundary is NEVER reaped, even when its `created_at` (i.e.,
          first event) is older than TTL. This is D1 in the plan: the
          boundary is `last_event_at`, not `created_at`. Otherwise a
          long-running session with a 91-day-old first event and a
          5-minute-old most-recent event would be reaped while still
          observably alive.

Plus precision tests for the boundary computation itself: cutoff is
inclusive vs. exclusive matters when wall-clock and storage timestamps
collide at the exact TTL boundary.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from secondsight.event import Event, EventType
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.events_repository import EventsRepository
from secondsight.storage.retention import (
    ExpiredSession,
    enumerate_expired_sessions,
)
from tests.conftest import make_event


# ----------------------------------------------------------------------
# Test fixture: a per-test EventsRepository over a fresh sqlite file.
# ----------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path: Path) -> EventsRepository:
    db = DBEngine(db_path=tmp_path / "intel.db")
    r = EventsRepository(db_engine=db)
    r.create_schema()
    return r


def _evt(
    *,
    session_id: str,
    seq: int,
    ts: datetime,
    event_type: EventType = EventType.USER_PROMPT,
) -> Event:
    return make_event(
        event_id=f"{session_id}-evt-{seq:06d}",
        session_id=session_id,
        sequence_number=seq,
        timestamp=ts,
        event_type=event_type,
    )


# ----------------------------------------------------------------------
# DC-1 — empty repo returns empty list, never raises
# ----------------------------------------------------------------------


class TestDC1EmptyProjectReturnsEmpty:
    """Fresh install + first cleanup must not crash and must not reap
    anything."""

    def test_no_events_returns_empty(self, repo: EventsRepository) -> None:
        now = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
        result = enumerate_expired_sessions(repo, raw_traces_ttl_days=90, now=now)
        assert result == []

    def test_no_events_does_not_raise(self, repo: EventsRepository) -> None:
        """Explicit DC-1: must NOT raise on an empty events table."""
        now = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
        # Should not raise — even with absurdly large TTL.
        enumerate_expired_sessions(repo, raw_traces_ttl_days=99999, now=now)


# ----------------------------------------------------------------------
# DC-2 — last_event_at boundary, NOT created_at
# ----------------------------------------------------------------------


class TestDC2LastEventAtBoundary:
    """The boundary is the most recent event in the session, not the
    first. A session that's been around for years but had an event
    5 minutes ago is observably alive and must not be reaped."""

    def test_old_first_event_recent_last_event_is_NOT_expired(self, repo: EventsRepository) -> None:
        now = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
        ttl_days = 90

        # First event 100 days ago — older than TTL.
        repo.insert(
            _evt(
                session_id="long-running",
                seq=1,
                ts=now - timedelta(days=100),
            )
        )
        # Most recent event 5 minutes ago — well within TTL.
        repo.insert(
            _evt(
                session_id="long-running",
                seq=2,
                ts=now - timedelta(minutes=5),
            )
        )

        result = enumerate_expired_sessions(repo, raw_traces_ttl_days=ttl_days, now=now)
        assert result == [], (
            "Long-running session reaped despite recent activity — "
            "boundary must be last_event_at, not created_at."
        )

    def test_old_first_event_old_last_event_IS_expired(self, repo: EventsRepository) -> None:
        now = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
        ttl_days = 90

        # All events older than TTL — session is genuinely dead.
        repo.insert(
            _evt(
                session_id="dead-session",
                seq=1,
                ts=now - timedelta(days=120),
            )
        )
        repo.insert(
            _evt(
                session_id="dead-session",
                seq=2,
                ts=now - timedelta(days=100),
            )
        )

        result = enumerate_expired_sessions(repo, raw_traces_ttl_days=ttl_days, now=now)
        assert len(result) == 1
        assert result[0].session_id == "dead-session"
        # Each ExpiredSession carries last_event_at for the cleanup
        # log line (D4). SQLite `sa.DateTime` columns are naive on
        # round-trip — strip tz from the expected value.
        assert result[0].last_event_at == (now - timedelta(days=100)).replace(tzinfo=None)

    def test_mixed_sessions_only_expired_ones_returned(self, repo: EventsRepository) -> None:
        now = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
        ttl_days = 90

        # Session A: dead (all events > 90d ago).
        repo.insert(_evt(session_id="sess-A", seq=1, ts=now - timedelta(days=150)))
        # Session B: alive (recent activity).
        repo.insert(_evt(session_id="sess-B", seq=1, ts=now - timedelta(days=200)))
        repo.insert(_evt(session_id="sess-B", seq=2, ts=now - timedelta(days=10)))
        # Session C: dead (all old).
        repo.insert(_evt(session_id="sess-C", seq=1, ts=now - timedelta(days=95)))

        result = enumerate_expired_sessions(repo, raw_traces_ttl_days=ttl_days, now=now)
        ids = sorted(r.session_id for r in result)
        assert ids == ["sess-A", "sess-C"], f"Expected only sess-A and sess-C reaped; got {ids}"


# ----------------------------------------------------------------------
# Boundary precision: a session with last_event_at exactly at the cutoff
# should be expired (>= ttl_days). Adopting the strict-inequality
# interpretation would let a session linger one tick longer than
# advertised — small but worth pinning.
# ----------------------------------------------------------------------


class TestBoundaryPrecision:
    def test_exactly_at_ttl_boundary_is_expired(self, repo: EventsRepository) -> None:
        now = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
        ttl_days = 90

        # last_event_at == now - 90d exactly → at the boundary.
        # Per the contract: "older than TTL" means >= ttl_days old,
        # so exactly-at-boundary IS expired.
        repo.insert(
            _evt(
                session_id="at-boundary",
                seq=1,
                ts=now - timedelta(days=ttl_days),
            )
        )
        result = enumerate_expired_sessions(repo, raw_traces_ttl_days=ttl_days, now=now)
        assert [r.session_id for r in result] == ["at-boundary"]

    def test_one_second_inside_boundary_not_expired(self, repo: EventsRepository) -> None:
        now = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
        ttl_days = 90

        repo.insert(
            _evt(
                session_id="just-alive",
                seq=1,
                ts=now - timedelta(days=ttl_days) + timedelta(seconds=1),
            )
        )
        result = enumerate_expired_sessions(repo, raw_traces_ttl_days=ttl_days, now=now)
        assert result == []


# ----------------------------------------------------------------------
# Return shape — ExpiredSession dataclass surface
# ----------------------------------------------------------------------


class TestExpiredSessionShape:
    def test_returns_expired_session_dataclass(self, repo: EventsRepository) -> None:
        now = datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)
        last = now - timedelta(days=120)
        repo.insert(_evt(session_id="dead", seq=1, ts=last))

        result = enumerate_expired_sessions(repo, raw_traces_ttl_days=90, now=now)
        assert len(result) == 1
        item = result[0]
        assert isinstance(item, ExpiredSession)
        assert item.session_id == "dead"
        # SQLite naive-datetime round-trip — see comment in
        # TestDC2LastEventAtBoundary.test_old_first_event_old_last_event_IS_expired
        assert item.last_event_at == last.replace(tzinfo=None)
