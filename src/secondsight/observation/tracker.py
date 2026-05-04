"""SessionTracker — in-memory, DB-warm-started session state (P1-7).

Maintains fast-path state for `segment_index`, `sub_agent_id`, and `depth`
per `session_id`. This is derived state — the durable truth lives in the
events table — but recomputing those fields per request from SQL would
dominate the hot-path latency budget.

Design assumptions (co-located with the fast-path code they justify):
- Single asyncio event loop / single uvicorn worker. asyncio.Lock is NOT
  cross-process safe. Multi-worker deployments are out of scope for Phase 1.
- The GIL makes `session_id in self._sessions` dict-lookup atomic for
  hashable keys (CPython implementation detail). ADDITIONALLY, the GIL acts
  as a full memory barrier between coroutines: the fast-path read at the
  `if session_id in self._sessions` check is guaranteed to observe the
  fully-initialized _SessionState written by the slow path. A free-threaded
  Python build (PEP 703 / no-GIL) invalidates BOTH the atomicity and the
  memory-ordering assumption — this code must be redesigned before running
  under a no-GIL interpreter.
- warm_start is invoked once per cold session per process lifetime.
  External DB rewrites (backfill) are not reflected in the in-memory cache.
  The cache is intentionally process-local and restart-cleared.
- Memory grows linearly with unique session_ids. No eviction policy in Phase 1.
  Both `_sessions` AND `_session_locks` grow unboundedly — both dicts accumulate
  one entry per unique session_id seen since server start. Neither is evicted
  until Phase 2 TTL/LRU (see scar report KS-1). `_session_locks` entries are
  intentionally retained even after reset_session() to prevent split-brain
  during concurrent reset+bind (see reset_session() docstring and KS-2).

If these assumptions stop holding, the first thing to rot is:
  concurrent segment_index assignment for the same session (duplicated indices).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from secondsight.event import Event, EventType


class WarmStart(Protocol):
    """Async callable: returns max segment_index for a session, or None.

    Returns None if the session has no prior events in the DB.
    Injected into SessionTracker for testability; production wires it to
    events_repo.get_max_segment_index.

    Raising is acceptable — SessionTracker propagates the exception and does
    NOT silently default to 0 (which would corrupt history).

    SCOPE NOTE: This Protocol recovers ONLY `segment_index`. The sub-agent
    stack is intentionally NOT reconstructed on warm-start (see SF-2 in scar
    report): nesting state is intra-session-context and too expensive to
    replay from DB on every cold sight. Post-restart events are treated as
    top-level (depth=0) regardless of pre-restart nesting.
    """

    async def __call__(self, session_id: str) -> int | None: ...


class SubAgentStackMismatch(Exception):
    """Raised when sub_agent_end arrives with a mismatched id or on an empty stack.

    Raising (rather than silently ignoring) is the correct behaviour because:
    - A silent pop would corrupt sub_agent_id and depth for all subsequent events
      in the session.
    - The caller (hook router) is responsible for logging / returning HTTP 422.
    """


@dataclass(frozen=True)
class PartialEvent:
    """Pre-tracker shape: everything we know before tracker-derived fields.

    The tracker fills in: segment_index, sub_agent_id, depth.
    For sub_agent_start events, data["sub_agent_id"] is the id being pushed.
    For sub_agent_end events, data["sub_agent_id"] is the id being popped
    (must match the top of the stack, or SubAgentStackMismatch is raised).
    """

    id: str
    session_id: str
    project_id: str
    event_type: EventType
    timestamp: datetime
    sequence_number: int
    data: dict[str, Any]
    duration_ms: int | None = None
    token_count: int | None = None


@dataclass
class _SessionState:
    """Per-session mutable state managed by SessionTracker.

    Lock hierarchy (two distinct lock objects per session):
    - `_SessionState.lock` (this field): the **mutation lock**, held during
      bind() while reading/writing segment_index and the sub_agent_stack.
    - `SessionTracker._session_locks[session_id]`: the **materialisation lock**,
      held only during the slow-path _get_or_create_state() window to prevent
      two concurrent coroutines from both calling warm_start for the same session.
    """

    segment_index: int
    sub_agent_stack: list[str] = field(default_factory=list)
    # Mutation lock — see class docstring for distinction from materialisation lock.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def current_sub_agent_id(self) -> str | None:
        return self.sub_agent_stack[-1] if self.sub_agent_stack else None

    @property
    def depth(self) -> int:
        return len(self.sub_agent_stack)


class SessionTracker:
    """Fast-path tracker for segment_index, sub_agent_id, depth.

    Thread-safety contract: per-session asyncio.Lock for bind() serialisation.
    The sessions dict itself is guarded by a single asyncio.Lock during the brief
    "does this session exist?" + "insert new session state" window. After a session
    is materialised, subsequent bind() calls hold only the per-session lock.

    Warm-start contract: on first bind() for a cold session, warm_start is called
    to recover segment_index from the events table. If warm_start raises, the
    exception propagates — we do NOT silently default to 0. (Silent default would
    corrupt segment history: see scar report SF-1.)
    """

    def __init__(self, *, warm_start: WarmStart) -> None:
        """
        Args:
            warm_start: async callable returning the last segment_index for a
                session from the DB, or None if no prior events exist. Injected
                for testability; production wires it to
                events_repo.get_max_segment_index.
        """
        self._warm_start = warm_start

        # session_id → _SessionState
        # Fast-path read: dict.__contains__ is GIL-atomic (CPython).
        # Both assumptions must hold: (1) single event loop, (2) CPython GIL.
        self._sessions: dict[str, _SessionState] = {}

        # Per-session asyncio.Locks, keyed by session_id.
        self._session_locks: dict[str, asyncio.Lock] = {}

        # Single guard protecting _session_locks dict during the brief
        # "create lock for new session" window.
        self._locks_guard: asyncio.Lock = asyncio.Lock()

    # NOTE: this two-level locking pattern is duplicated at:
    #   - api/registry.py:84 (ProjectRegistry.get)
    #   - api/server.py (AppState.get_or_create_tracker)
    # Phase 2 / iteration will consolidate into a shared _LazyCacheWithLocking utility.
    # Until then: changes to the locking logic MUST be applied to ALL THREE sites.
    async def _get_or_create_state(self, session_id: str) -> _SessionState:
        """Return existing state, or materialise a new one via warm_start.

        IMPORTANT: warm_start errors propagate — never silently default to 0.
        This is the most critical failure path (see scar report SF-1).
        """
        # Fast path: session already known.
        # Safe without a lock: single event loop + CPython GIL atomicity.
        if session_id in self._sessions:
            return self._sessions[session_id]

        # Slow path: first bind for this session.
        # Ensure a per-session lock exists before acquiring it.
        async with self._locks_guard:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = asyncio.Lock()
        session_lock = self._session_locks[session_id]

        async with session_lock:
            # Double-check inside the lock — another coroutine may have
            # materialised state while we were waiting.
            if session_id in self._sessions:
                return self._sessions[session_id]

            # Call warm_start. If it raises, propagate — do NOT catch and
            # default to 0. Silently defaulting would corrupt history.
            resume_index = await self._warm_start(session_id)

            # resume_index is MAX(segment_index) from the DB.
            # We resume from that value — non-prompt events keep it;
            # user_prompt increments it.
            initial_segment = resume_index if resume_index is not None else 0

            state = _SessionState(segment_index=initial_segment)
            self._sessions[session_id] = state
            return state

    async def bind(self, partial: PartialEvent) -> Event:
        """Fill in segment_index, sub_agent_id, depth on partial.

        Returns a fully-formed immutable Event.

        Raises:
            SubAgentStackMismatch: sub_agent_end with no matching start,
                or with a mismatched id. Tracker state is NOT mutated on mismatch.
            ValueError: partial is missing required fields (e.g. sub_agent_id
                absent from data on a sub_agent_start/end event).
            Any exception from the WarmStart callable on cold session sight.
        """
        state = await self._get_or_create_state(partial.session_id)

        async with state.lock:
            # --- Compute tracker-derived fields inside the per-session lock ---

            if partial.event_type == EventType.USER_PROMPT:
                # Invariant: segment_index increments by exactly 1 on user_prompt.
                state.segment_index += 1

            segment_index = state.segment_index

            # --- Sub-agent stack management ---
            if partial.event_type == EventType.SUB_AGENT_START:
                agent_id = partial.data.get("sub_agent_id")
                if agent_id is None:
                    raise ValueError(
                        "sub_agent_start data must include non-empty sub_agent_id"
                    )
                if not isinstance(agent_id, str) or agent_id == "":
                    raise ValueError(
                        "sub_agent_start data must include non-empty sub_agent_id"
                    )
                state.sub_agent_stack.append(agent_id)

            elif partial.event_type == EventType.SUB_AGENT_END:
                end_id = partial.data.get("sub_agent_id")
                if end_id is None:
                    raise ValueError(
                        "sub_agent_end data must include non-empty sub_agent_id"
                    )
                if not isinstance(end_id, str) or end_id == "":
                    raise ValueError(
                        "sub_agent_end data must include non-empty sub_agent_id"
                    )
                # end_id is guaranteed str and non-empty after the checks above.
                if not state.sub_agent_stack:
                    raise SubAgentStackMismatch(
                        f"sub_agent_end(id={end_id!r}) on empty stack "
                        f"for session {partial.session_id!r}"
                    )
                top = state.sub_agent_stack[-1]
                if top != end_id:
                    # Hard error: do NOT mutate state. Stack stays as-is.
                    raise SubAgentStackMismatch(
                        f"sub_agent_end(id={end_id!r}) does not match "
                        f"top of stack (id={top!r}) "
                        f"for session {partial.session_id!r}"
                    )
                # Matched — safe to pop.
                state.sub_agent_stack.pop()

            # Snapshot depth and sub_agent_id AFTER push/pop.
            depth = state.depth
            sub_agent_id = state.current_sub_agent_id

        # Construct the immutable Event outside the lock (pure data construction).
        return Event(
            id=partial.id,
            session_id=partial.session_id,
            project_id=partial.project_id,
            event_type=partial.event_type,
            timestamp=partial.timestamp,
            sequence_number=partial.sequence_number,
            segment_index=segment_index,
            sub_agent_id=sub_agent_id,
            depth=depth,
            duration_ms=partial.duration_ms,
            token_count=partial.token_count,
            data=partial.data,
        )

    def reset_session(self, session_id: str) -> None:
        """Drop tracker state for a session.

        Used when a session_end event arrives — we still write the event;
        we just stop caching the session counters to reclaim memory.

        Contract: synchronous and non-locking. Any in-flight bind() that already
        holds the per-session lock will complete normally. The next bind() after
        reset will cold-start (calling warm_start again).

        The per-session lock object is intentionally NOT removed from
        _session_locks. A concurrent bind() may hold it; removing and
        re-creating it would produce two independent locks for the same session,
        breaking mutual exclusion during the overlap window.
        """
        self._sessions.pop(session_id, None)


__all__ = [
    "PartialEvent",
    "SessionTracker",
    "SubAgentStackMismatch",
    "WarmStart",
]
