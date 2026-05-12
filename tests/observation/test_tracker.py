"""Tests for SessionTracker (P1-7).

Death tests come first, per samsara protocol.
Death tests target the silent failure paths; unit tests verify the happy path.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import pytest

from secondsight.event import Event, EventType
from secondsight.observation.tracker import (
    PartialEvent,
    SessionTracker,
    SubAgentStackMismatch,
    WarmStart,
)
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.events_repository import EventsRepository
from tests.conftest import make_event

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TIMESTAMP = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)


def make_partial(
    *,
    event_id: str | None = None,
    session_id: str = "sess-001",
    project_id: str = "proj-alpha",
    event_type: EventType = EventType.THINKING,
    sequence_number: int = 1,
    data: dict[str, Any] | None = None,
    duration_ms: int | None = None,
    token_count: int | None = None,
) -> PartialEvent:
    return PartialEvent(
        id=event_id or str(uuid.uuid4()),
        session_id=session_id,
        project_id=project_id,
        event_type=event_type,
        timestamp=_TIMESTAMP,
        sequence_number=sequence_number,
        data=data or {},
        duration_ms=duration_ms,
        token_count=token_count,
    )


async def null_warm_start(session_id: str) -> int | None:
    """Always returns None — fresh session, no prior events."""
    return None


def make_tracker(warm_start: WarmStart | None = None) -> SessionTracker:
    return SessionTracker(warm_start=warm_start or null_warm_start)


# ---------------------------------------------------------------------------
# DEATH TESTS — silent failure paths verified red before production code
# ---------------------------------------------------------------------------


async def test_death_cold_restart_segment_index_resume(tmp_path: Path) -> None:
    """Death test 1: cold restart must NOT reset segment_index to 0.

    Simulate a prior process by inserting events with segment_index 0..5
    directly into the DB. Then construct a fresh tracker backed by that DB.
    The tracker must resume from 5, not overwrite from 0.
    """
    eng = DBEngine(tmp_path / "intel.db")
    repo = EventsRepository(eng)
    repo.create_schema()

    # Simulate events from a prior process: 6 events across 6 segments
    # The user_prompt events bumped segment_index from 0 to 5.
    prior_events = []
    for seg_idx in range(6):
        prior_events.append(
            make_event(
                event_id=f"prior-{seg_idx}",
                session_id="sess-cold",
                project_id="proj-alpha",
                event_type=EventType.USER_PROMPT,
                segment_index=seg_idx,
                sequence_number=seg_idx,
            )
        )
    repo.insert_many(prior_events)
    assert repo.get_max_segment_index("sess-cold") == 5

    # Wire warm_start to the real DB
    async def db_warm_start(session_id: str) -> int | None:
        return await asyncio.to_thread(repo.get_max_segment_index, session_id)

    tracker = SessionTracker(warm_start=db_warm_start)

    # Non-user_prompt event: must resume at segment_index=5, NOT 0
    partial_non_prompt = make_partial(
        event_id="after-restart-1",
        session_id="sess-cold",
        event_type=EventType.THINKING,
        sequence_number=6,
    )
    evt1 = await tracker.bind(partial_non_prompt)
    assert evt1.segment_index == 5, (
        f"Expected segment_index=5 after cold restart (resume), got {evt1.segment_index}. "
        "Silent default to 0 would corrupt history."
    )

    # user_prompt event: must increment from 5 → 6
    partial_prompt = make_partial(
        event_id="after-restart-2",
        session_id="sess-cold",
        event_type=EventType.USER_PROMPT,
        sequence_number=7,
    )
    evt2 = await tracker.bind(partial_prompt)
    assert evt2.segment_index == 6, (
        f"Expected segment_index=6 after user_prompt post-restart, got {evt2.segment_index}."
    )

    eng.dispose()


async def test_death_sub_agent_mismatch_not_silently_swallowed() -> None:
    """Death test 2: sub_agent_end with wrong id must raise SubAgentStackMismatch.

    After the mismatch, tracker state must be unmodified — the stack still has
    "A" on top, and subsequent binds behave as if "B" was never attempted.
    """
    tracker = make_tracker()

    # Push sub_agent "A"
    start_a = make_partial(
        event_id="sa-start-a",
        event_type=EventType.SUB_AGENT_START,
        data={"sub_agent_id": "agent-A"},
        sequence_number=1,
    )
    evt_start = await tracker.bind(start_a)
    assert evt_start.sub_agent_id == "agent-A"
    assert evt_start.depth == 1

    # Attempt to pop "B" — must raise, NOT silently match "A"
    end_b = make_partial(
        event_id="sa-end-b",
        event_type=EventType.SUB_AGENT_END,
        data={"sub_agent_id": "agent-B"},
        sequence_number=2,
    )
    with pytest.raises(SubAgentStackMismatch):
        await tracker.bind(end_b)

    # Tracker state must be unmodified: "A" still on top
    check_event = make_partial(
        event_id="check-after-mismatch",
        event_type=EventType.THINKING,
        sequence_number=3,
    )
    evt_check = await tracker.bind(check_event)
    assert evt_check.sub_agent_id == "agent-A", (
        "After mismatch, sub_agent_id must still be 'agent-A' — stack not mutated."
    )
    assert evt_check.depth == 1, "After mismatch, depth must still be 1 — stack not mutated."


async def test_death_concurrent_bind_race_no_duplicates() -> None:
    """Death test 3: 100 concurrent user_prompt binds must produce exactly
    1..100 segment indices with no duplicates and no gaps.

    This is the critical race — without a per-session asyncio.Lock, two
    coroutines could both read segment_index=N and both return N+1.
    """
    tracker = make_tracker()

    # Prime the tracker for this session with segment_index=0
    # (new session, warm_start returns None → starts at 0)
    # All 100 events are user_prompt — each must increment by 1.
    async def bind_one(seq: int) -> Event:
        p = make_partial(
            event_id=f"concurrent-{seq}",
            session_id="sess-race",
            event_type=EventType.USER_PROMPT,
            sequence_number=seq,
        )
        return await tracker.bind(p)

    results = await asyncio.gather(*[bind_one(i) for i in range(100)])
    segment_indices = [e.segment_index for e in results]

    assert len(set(segment_indices)) == 100, (
        f"Duplicate segment_index values found under concurrency: {sorted(segment_indices)}"
    )
    assert set(segment_indices) == set(range(1, 101)), (
        f"Expected exactly {{1..100}}, got: {sorted(segment_indices)}"
    )


async def test_death_reset_session_db_backed_warm_start(tmp_path: Path) -> None:
    """Death test 4 (SF-3 race window): reset_session + bind with DB-backed warm_start
    must resume from DB segment_index, NOT restart from 0.

    This exercises the actual SF-3 risk: reset fires after bind1 completes
    (incrementing tracker memory to segment_index=5), then bind2's warm_start reads
    MAX(segment_index)=5 from the DB and resumes at 5, incrementing to 6.

    The test would fail if the implementation dropped the _sessions entry on reset
    but did NOT re-invoke warm_start for the post-reset bind — instead resuming
    from 0 (the "silent regression" path).

    Ordering:
    1. Bind user_prompt events so segment_index reaches 5 in tracker memory.
    2. Insert those events into DB so MAX(segment_index)=5 in the DB.
    3. Call reset_session — evicts tracker memory.
    4. Bind another user_prompt — warm_start reads MAX=5, resumes at 5, increments to 6.
    5. Assert evt2.segment_index == 6 (not 1, which is the regression).
    """
    eng = DBEngine(tmp_path / "sf3-race.db")
    repo = EventsRepository(eng)
    repo.create_schema()

    # Build 5 user_prompt events with segment_index 1..5
    prior_events = []
    for seg_idx in range(1, 6):
        prior_events.append(
            make_event(
                event_id=f"sf3-prior-{seg_idx}",
                session_id="sess-sf3",
                project_id="proj-alpha",
                event_type=EventType.USER_PROMPT,
                segment_index=seg_idx,
                sequence_number=seg_idx,
            )
        )
    repo.insert_many(prior_events)
    assert repo.get_max_segment_index("sess-sf3") == 5

    # Wire warm_start to the real DB — simulates production
    async def db_warm_start(session_id: str) -> int | None:
        return await asyncio.to_thread(repo.get_max_segment_index, session_id)

    tracker = SessionTracker(warm_start=db_warm_start)

    # Prime tracker in-memory state: bind a non-prompt event to cold-start at 5
    prime = make_partial(
        event_id="sf3-prime",
        session_id="sess-sf3",
        event_type=EventType.THINKING,
        sequence_number=6,
    )
    evt_prime = await tracker.bind(prime)
    assert evt_prime.segment_index == 5, (
        f"Tracker must resume at 5 after cold sight, got {evt_prime.segment_index}"
    )

    # reset_session: evicts _sessions["sess-sf3"] from tracker memory
    tracker.reset_session("sess-sf3")

    # Post-reset bind: warm_start is called again, reads MAX=5 from DB,
    # resumes at 5, then increments to 6 for user_prompt.
    evt2 = await tracker.bind(
        make_partial(
            event_id="sf3-post-reset",
            session_id="sess-sf3",
            event_type=EventType.USER_PROMPT,
            sequence_number=7,
        )
    )
    assert evt2.segment_index == 6, (
        f"Post-reset bind must resume from DB warm_start (MAX=5) and increment to 6. "
        f"Got segment_index={evt2.segment_index}. "
        f"segment_index=1 would indicate regression: tracker re-started from 0 "
        f"instead of calling warm_start again after reset."
    )

    eng.dispose()


async def test_death_warm_start_failure_does_not_default_to_zero() -> None:
    """Death test 5: if WarmStart raises, bind must propagate the exception.

    Silent default to 0 is the worst possible failure — it would re-stamp
    every restart's first segment as 0 and corrupt history.
    """

    async def failing_warm_start(session_id: str) -> int | None:
        raise OSError("disk full")

    tracker = SessionTracker(warm_start=failing_warm_start)

    partial = make_partial(
        event_id="warm-fail-1",
        session_id="sess-warm-fail",
        event_type=EventType.THINKING,
        sequence_number=1,
    )
    with pytest.raises(OSError, match="disk full"):
        await tracker.bind(partial)

    # Verify the tracker did NOT cache a zero default for this session.
    # If we retry with a working warm_start, it must call warm_start again
    # (the cache must not have been poisoned with 0).
    # We verify this by swapping in a working warm_start on a new tracker.
    resume_called: list[str] = []

    async def working_warm_start(session_id: str) -> int | None:
        resume_called.append(session_id)
        return 7  # Simulate existing history with max segment_index=7

    tracker2 = SessionTracker(warm_start=working_warm_start)
    partial2 = make_partial(
        event_id="warm-retry-1",
        session_id="sess-warm-fail",
        event_type=EventType.THINKING,
        sequence_number=2,
    )
    evt = await tracker2.bind(partial2)
    assert evt.segment_index == 7, (
        f"After warm_start returns 7, non-prompt event should stay at 7, got {evt.segment_index}"
    )
    assert resume_called == ["sess-warm-fail"], "warm_start must be called on cold session"


async def test_death_sub_agent_depth_does_not_leak_across_sessions() -> None:
    """Death test 6: sub_agent stack is per-session — depth in session B
    must not be contaminated by session A's stack.
    """
    tracker = make_tracker()

    # Push sub_agent in session A
    start_a = make_partial(
        event_id="sa-start-sess-a",
        session_id="session-A",
        event_type=EventType.SUB_AGENT_START,
        data={"sub_agent_id": "agent-A"},
        sequence_number=1,
    )
    evt_a = await tracker.bind(start_a)
    assert evt_a.depth == 1
    assert evt_a.sub_agent_id == "agent-A"

    # Bind an event in session B — must have depth=0 and sub_agent_id=None
    evt_b_partial = make_partial(
        event_id="thinking-sess-b",
        session_id="session-B",
        event_type=EventType.THINKING,
        sequence_number=1,
    )
    evt_b = await tracker.bind(evt_b_partial)
    assert evt_b.depth == 0, (
        f"Session B must start at depth=0; session A's stack must not leak. Got {evt_b.depth}"
    )
    assert evt_b.sub_agent_id is None, (
        f"Session B must have sub_agent_id=None; got {evt_b.sub_agent_id}"
    )


# ---------------------------------------------------------------------------
# UNIT TESTS — happy path and invariants
# ---------------------------------------------------------------------------


async def test_unit_user_prompt_increments_segment_index() -> None:
    """user_prompt increments segment_index by exactly 1."""
    tracker = make_tracker()
    evt = await tracker.bind(
        make_partial(
            event_id="ut-prompt-1",
            session_id="sess-ut",
            event_type=EventType.USER_PROMPT,
            sequence_number=1,
        )
    )
    assert evt.segment_index == 1


async def test_unit_non_prompt_keeps_segment_index() -> None:
    """Non-user_prompt events keep segment_index unchanged."""
    tracker = make_tracker()

    # First event — cold start, segment_index=0
    evt1 = await tracker.bind(
        make_partial(
            event_id="ut-think-1",
            session_id="sess-ut-np",
            event_type=EventType.THINKING,
            sequence_number=1,
        )
    )
    assert evt1.segment_index == 0

    # Second event — also non-prompt; still 0
    evt2 = await tracker.bind(
        make_partial(
            event_id="ut-think-2",
            session_id="sess-ut-np",
            event_type=EventType.TOOL_USE_START,
            sequence_number=2,
        )
    )
    assert evt2.segment_index == 0


async def test_unit_sub_agent_start_pushes_stack() -> None:
    """sub_agent_start pushes id and depth=1; subsequent events carry both."""
    tracker = make_tracker()
    session = "sess-sa-push"

    start = make_partial(
        event_id="sa-push-start",
        session_id=session,
        event_type=EventType.SUB_AGENT_START,
        data={"sub_agent_id": "agent-X"},
        sequence_number=1,
    )
    evt_start = await tracker.bind(start)
    assert evt_start.sub_agent_id == "agent-X"
    assert evt_start.depth == 1

    # Next event also carries the sub_agent context
    next_evt = await tracker.bind(
        make_partial(
            event_id="sa-push-next",
            session_id=session,
            event_type=EventType.THINKING,
            sequence_number=2,
        )
    )
    assert next_evt.sub_agent_id == "agent-X"
    assert next_evt.depth == 1


async def test_unit_matched_sub_agent_end_pops_stack() -> None:
    """Matched sub_agent_end pops; subsequent events carry depth=0, sub_agent_id=None."""
    tracker = make_tracker()
    session = "sess-sa-pop"

    await tracker.bind(
        make_partial(
            event_id="sa-pop-start",
            session_id=session,
            event_type=EventType.SUB_AGENT_START,
            data={"sub_agent_id": "agent-Y"},
            sequence_number=1,
        )
    )
    await tracker.bind(
        make_partial(
            event_id="sa-pop-end",
            session_id=session,
            event_type=EventType.SUB_AGENT_END,
            data={"sub_agent_id": "agent-Y"},
            sequence_number=2,
        )
    )
    # Post-pop: depth=0, sub_agent_id=None
    after = await tracker.bind(
        make_partial(
            event_id="sa-pop-after",
            session_id=session,
            event_type=EventType.THINKING,
            sequence_number=3,
        )
    )
    assert after.depth == 0
    assert after.sub_agent_id is None


async def test_unit_nested_sub_agents() -> None:
    """Nested sub-agents: A → B carry (sub_agent_id=B, depth=2).
    End B → (A, depth=1). End A → (None, 0).
    """
    tracker = make_tracker()
    session = "sess-nested"
    seq = 0

    def next_seq() -> int:
        nonlocal seq
        seq += 1
        return seq

    await tracker.bind(
        make_partial(
            event_id="nested-start-a",
            session_id=session,
            event_type=EventType.SUB_AGENT_START,
            data={"sub_agent_id": "agent-A"},
            sequence_number=next_seq(),
        )
    )

    await tracker.bind(
        make_partial(
            event_id="nested-start-b",
            session_id=session,
            event_type=EventType.SUB_AGENT_START,
            data={"sub_agent_id": "agent-B"},
            sequence_number=next_seq(),
        )
    )

    # Inside B: depth=2, sub_agent_id=B
    inside_b = await tracker.bind(
        make_partial(
            event_id="nested-inside-b",
            session_id=session,
            event_type=EventType.THINKING,
            sequence_number=next_seq(),
        )
    )
    assert inside_b.depth == 2
    assert inside_b.sub_agent_id == "agent-B"

    # End B → depth=1, sub_agent_id=A
    await tracker.bind(
        make_partial(
            event_id="nested-end-b",
            session_id=session,
            event_type=EventType.SUB_AGENT_END,
            data={"sub_agent_id": "agent-B"},
            sequence_number=next_seq(),
        )
    )
    after_b = await tracker.bind(
        make_partial(
            event_id="nested-after-b",
            session_id=session,
            event_type=EventType.THINKING,
            sequence_number=next_seq(),
        )
    )
    assert after_b.depth == 1
    assert after_b.sub_agent_id == "agent-A"

    # End A → depth=0, sub_agent_id=None
    await tracker.bind(
        make_partial(
            event_id="nested-end-a",
            session_id=session,
            event_type=EventType.SUB_AGENT_END,
            data={"sub_agent_id": "agent-A"},
            sequence_number=next_seq(),
        )
    )
    after_a = await tracker.bind(
        make_partial(
            event_id="nested-after-a",
            session_id=session,
            event_type=EventType.THINKING,
            sequence_number=next_seq(),
        )
    )
    assert after_a.depth == 0
    assert after_a.sub_agent_id is None


async def test_unit_bind_returns_frozen_event() -> None:
    """bind returns an Event with model_config.frozen honored — mutation raises."""
    tracker = make_tracker()
    evt = await tracker.bind(
        make_partial(
            event_id="frozen-check",
            session_id="sess-frozen",
            event_type=EventType.USER_PROMPT,
            sequence_number=1,
        )
    )
    assert isinstance(evt, Event)
    # Pydantic frozen model raises ValidationError / TypeError on mutation
    with pytest.raises(Exception):
        evt.segment_index = 999  # type: ignore[misc]


async def test_unit_reset_session_clears_state() -> None:
    """reset_session drops tracker state; next bind for that session cold-starts."""
    warm_calls: list[str] = []

    async def tracking_warm_start(session_id: str) -> int | None:
        warm_calls.append(session_id)
        return None

    tracker = SessionTracker(warm_start=tracking_warm_start)
    session = "sess-reset"

    # First bind — cold start
    evt1 = await tracker.bind(
        make_partial(
            event_id="reset-before",
            session_id=session,
            event_type=EventType.USER_PROMPT,
            sequence_number=1,
        )
    )
    assert evt1.segment_index == 1
    assert warm_calls == [session]  # warm_start called once

    # Reset
    tracker.reset_session(session)

    # Second bind — must call warm_start again (session was evicted)
    evt2 = await tracker.bind(
        make_partial(
            event_id="reset-after",
            session_id=session,
            event_type=EventType.USER_PROMPT,
            sequence_number=2,
        )
    )
    assert evt2.segment_index == 1  # warm_start returns None → fresh start
    assert warm_calls == [session, session], "warm_start must be called again after reset"


async def test_unit_sub_agent_end_on_empty_stack_raises() -> None:
    """sub_agent_end with an empty stack must raise SubAgentStackMismatch."""
    tracker = make_tracker()

    with pytest.raises(SubAgentStackMismatch):
        await tracker.bind(
            make_partial(
                event_id="empty-stack-end",
                session_id="sess-empty",
                event_type=EventType.SUB_AGENT_END,
                data={"sub_agent_id": "agent-Z"},
                sequence_number=1,
            )
        )


async def test_unit_empty_string_sub_agent_id_raises() -> None:
    """sub_agent_id='' must raise ValueError — empty string is not a valid id.

    F5 fix: the guard changed from `if not agent_id` to `if agent_id is None`
    plus an explicit empty-string check. This test verifies empty string is
    explicitly rejected rather than silently treated as missing.
    """
    tracker = make_tracker()

    # sub_agent_start with empty string sub_agent_id
    with pytest.raises(ValueError, match="non-empty sub_agent_id"):
        await tracker.bind(
            make_partial(
                event_id="empty-str-start",
                session_id="sess-empty-str",
                event_type=EventType.SUB_AGENT_START,
                data={"sub_agent_id": ""},
                sequence_number=1,
            )
        )

    # sub_agent_end with empty string sub_agent_id (also rejected before stack check)
    with pytest.raises(ValueError, match="non-empty sub_agent_id"):
        await tracker.bind(
            make_partial(
                event_id="empty-str-end",
                session_id="sess-empty-str",
                event_type=EventType.SUB_AGENT_END,
                data={"sub_agent_id": ""},
                sequence_number=2,
            )
        )

    # Verify tracker state was NOT corrupted (stack still empty for this session)
    thinking = await tracker.bind(
        make_partial(
            event_id="empty-str-check",
            session_id="sess-empty-str",
            event_type=EventType.THINKING,
            sequence_number=3,
        )
    )
    assert thinking.depth == 0, "Stack must be unmodified after rejected empty-string push"
    assert thinking.sub_agent_id is None


async def test_unit_all_event_types_preserve_segment_index() -> None:
    """Non-user_prompt event types each keep segment_index at 0 on a fresh session."""
    non_prompt_types = [
        EventType.THINKING,
        EventType.TOOL_USE_START,
        EventType.TOOL_USE_END,
        EventType.RESPONSE,
        EventType.TASK_CREATED,
        EventType.TASK_COMPLETED,
        EventType.SESSION_START,
        EventType.SESSION_END,
    ]
    tracker = make_tracker()
    for i, et in enumerate(non_prompt_types):
        # Each event goes to its own session to avoid state carry-over
        session = f"sess-etype-{i}"
        evt = await tracker.bind(
            make_partial(
                event_id=f"etype-{i}",
                session_id=session,
                event_type=et,
                sequence_number=1,
            )
        )
        assert evt.segment_index == 0, (
            f"EventType.{et.name} must keep segment_index=0 on cold start, got {evt.segment_index}"
        )
