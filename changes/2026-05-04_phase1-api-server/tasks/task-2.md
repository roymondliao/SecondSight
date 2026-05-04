# Task 2: SessionTracker — In-memory, DB-warm-started (P1-7)

## Context

Read: overview.md (esp. "SessionTracker is process-local, but warm-starts from DB")

The tracker maintains the *fast-path* state needed to fill in `segment_index`, `sub_agent_id`, and `depth` on every event. It is a derived state — the durable truth lives in the events table — but recomputing those fields per request from SQL would dominate the hot-path latency budget. Tracker is therefore an in-memory cache that is **authoritative within a process lifetime** and **warm-starts from DB on first sight of a session after restart**.

**Plan ref:** P1-7
**SD refs:** §3.7.5 (column shape), §3.9 (pipeline ordering)

**Key invariants:**

- `segment_index` is monotonically non-decreasing per `(project_id, session_id)`.
- `segment_index` increments by **exactly 1** when the next event is a `user_prompt`. All other event types share the prior `segment_index`.
- `sub_agent_id` and `depth` form a stack: `sub_agent_start` pushes; `sub_agent_end` pops (must match top of stack — mismatched pop is a hard error, not silent ignore).
- After process restart, on first sight of a session, tracker reads `events_repo.get_max_segment_index(session_id)` to set the resume value. Sub-agent stack is NOT reconstructed (depth resets to 0); rationale: nesting is intra-session-LLM-context state and a server restart that interrupts a sub-agent run is rare *and* DB rows already carry the historical depth.

## Files

- Create: `src/secondsight/observation/tracker.py`
- Create: `tests/observation/test_tracker.py`

## Public Contract

```python
class SessionTracker:
    def __init__(self, *, warm_start: WarmStart) -> None:
        """warm_start: callable that returns the last segment_index for a session,
        or None if the session has no prior events. Injected for testability —
        production wires it to events_repo.get_max_segment_index.
        """

    async def bind(self, partial: PartialEvent) -> Event:
        """Fill in segment_index, sub_agent_id, depth on `partial`.
        Returns a fully-formed immutable Event.

        Raises:
            SubAgentStackMismatch: sub_agent_end with no matching start, or
                with a mismatched id.
            ValueError: partial is missing required fields.
        """

    def reset_session(self, session_id: str) -> None:
        """Drop tracker state for a session. Used when a session_end event
        arrives (we still write the event; we just stop caching the counters)."""

class WarmStart(Protocol):
    async def __call__(self, session_id: str) -> int | None: ...

class SubAgentStackMismatch(Exception): ...

@dataclass(frozen=True)
class PartialEvent:
    """Pre-tracker shape: everything we know before tracker-derived fields."""
    id: str
    session_id: str
    project_id: str
    event_type: EventType
    timestamp: datetime
    sequence_number: int
    data: dict[str, Any]
    duration_ms: int | None = None
    token_count: int | None = None
    # tracker fills these:
    # segment_index, sub_agent_id, depth
    # AND for sub_agent_start events, data["sub_agent_id"] is the id being pushed
```

## Death Test Requirements (write and verify red BEFORE production code)

1. **Cold-restart segment_index overwrite.** Insert events with `segment_index` 0..5 directly via `EventsRepository.insert_many` (simulating a prior process). Construct a fresh `SessionTracker`. Bind a *non*-`user_prompt` event for that session. Assert: `segment_index == 5` (resume), NOT `0`. Then bind a `user_prompt`: assert `segment_index == 6`.
2. **Sub-agent stack mismatch silently swallowed.** Send `sub_agent_start(id="A")`, then `sub_agent_end(id="B")`. Assert: `SubAgentStackMismatch` raises; tracker state is NOT mutated; subsequent `bind` calls behave as if "B" was never popped.
3. **Concurrent bind race for the same session.** 100 `asyncio.gather` calls to `bind` with `event_type=user_prompt` for one session. Assert: the 100 `segment_index` values returned are exactly `1..100` (or whatever the resume base is + 1..100), with no duplicates and no gaps.
4. **`reset_session` between concurrent binds.** Bind a `user_prompt` (segment_index=1), in parallel call `reset_session`, then bind another `user_prompt`. Assert: either ordering produces a valid monotonic sequence (no `segment_index=0` regression), and the `reset_session` either fully drains pending state or is queued behind the in-flight bind.
5. **Warm-start fails (DB error during cold sight).** Mock `WarmStart` to raise `OSError("disk full")`. Assert: `bind` raises and does NOT silently default to 0. (A silent default here is the worst possible failure — it would re-stamp every restart's first segment as 0 and overwrite history.)
6. **Sub-agent depth leak across sessions.** Push `sub_agent_start` in session A; bind events in session B. Assert: session B's depth is 0 — sub-agent stack is per-session.

## Unit Test Requirements

- `bind` of a `user_prompt` increments `segment_index` by exactly 1.
- `bind` of any non-`user_prompt` event keeps `segment_index` unchanged.
- `bind` of `sub_agent_start` pushes; subsequent events carry `depth=1` and `sub_agent_id` equal to the start's id.
- `bind` of matched `sub_agent_end` pops; subsequent events carry `depth=0`.
- Nested sub-agents: start "A" → start "B" → events carry `(sub_agent_id="B", depth=2)`. End "B" → `(sub_agent_id="A", depth=1)`. End "A" → `(None, 0)`.
- Round-trip: `bind` returns an `Event` with `model_config.frozen` honored.

## Implementation Steps

- [ ] Step 1: STEP 0 — answer the four prerequisite questions
- [ ] Step 2: Write death tests
- [ ] Step 3: Run death tests — red
- [ ] Step 4: Write unit tests
- [ ] Step 5: Run unit tests — red
- [ ] Step 6: Implement `PartialEvent` and `SubAgentStackMismatch`
- [ ] Step 7: Implement `SessionTracker` with per-session `asyncio.Lock` (similar pattern to `ProjectRegistry`)
- [ ] Step 8: Run all tests — green
- [ ] Step 9: Write scar report
- [ ] Step 10: Self-iteration (Level 1) — fix task-scope items
- [ ] Step 11: Re-run tests — no regression

## Expected Scar Report Items

- Potential silent failure: tracker memory grows linearly with unique session_ids seen. No eviction policy. Defer to Phase 2 (couple with session-end TTL).
- Potential silent failure: sub-agent stack is reset on restart — if a long-running sub_agent_start with no matching end was in flight, post-restart events look like top-level. Documented as intentional; depth lives in the events table for analysis.
- Assumption to verify: SD §3.7.5 says `segment_index` increments on `user_prompt`. Re-read SD to confirm we don't also need to bump on `session_start` (which is a session-level boundary, not a turn boundary).
- Potential shortcut: warm-start callable is invoked once per cold session. We do not invalidate the cache when the events table is rewritten externally (e.g. backfill).
- Boundary issue: `bind` accepts a `PartialEvent` with a caller-supplied `sequence_number`; tracker does not validate monotonicity of that field. Caller (hook router) owns it.

## Acceptance Criteria

- All death tests pass
- All unit tests pass
- `mypy` clean
- Scar report complete
- No imports from `secondsight.poc.*`
- No imports from `secondsight.api.*` (tracker is observation-layer; API depends on observation, not vice-versa)
