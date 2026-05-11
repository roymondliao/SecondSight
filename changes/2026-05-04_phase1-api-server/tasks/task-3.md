# Task 3: Hook Endpoints — POST /hook/{type} (P1-6)

## Context

Read: overview.md (esp. "Normalizer is a Protocol, not a class hierarchy" and "Hook response returns *before* ingest completes")

This is the heart of the API server. The route handler must do four things and exactly four things, in order:

1. Validate the envelope (project_id, session_id, agent identifier required).
2. Route the payload to the right Normalizer and produce a `PartialEvent`.
3. Hand the partial to `SessionTracker.bind` to obtain a fully-formed `Event`.
4. Schedule `pipeline.ingest(event)` via `asyncio.create_task` and return `{"status": "ok"}`.

The latency contract is that the handler does **not await** the ingest task. A future contributor adding `await` to "make tests pass" would silently violate the contract; we make this structurally inspectable via a death test.

**Plan ref:** P1-6
**SD refs:** §3.9 (pipeline shape), §3.9.1 (latency analysis)

**Dependencies:** task-1 (server scaffold + registry), task-2 (SessionTracker)

## Files

- Create: `src/secondsight/api/hooks.py` — APIRouter
- Create: `src/secondsight/api/normalizer.py` — `Normalizer` Protocol, `NormalizerRegistry`, `IdentityNormalizer`
- Create: `src/secondsight/api/schemas.py` — Pydantic envelope models
- Create: `tests/api/test_hooks_endpoint.py`
- Create: `tests/api/test_latency_contract.py` — the dedicated death-test suite for the no-await contract
- Update: `src/secondsight/api/server.py` — wire hooks router into `create_app()`

## Public Contract

```python
# api/schemas.py
class HookEnvelope(BaseModel):
    """Minimum required envelope for any hook payload."""
    model_config = ConfigDict(extra="allow")  # adapter-specific fields pass through

    project_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    agent: str = Field(min_length=1, max_length=64)        # "claude-code", "codex", ...
    event_id: str = Field(min_length=1, max_length=128)    # adapter-supplied deterministic id
    timestamp: datetime                                     # adapter-supplied; UTC
    sequence_number: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)  # raw agent-side payload

# api/normalizer.py
class Normalizer(Protocol):
    def supports(self, agent: str, event_type: str) -> bool: ...
    def normalize(self, envelope: HookEnvelope, event_type: str) -> PartialEvent: ...

class NormalizerRegistry:
    def register(self, normalizer: Normalizer) -> None: ...
    def for_(self, agent: str, event_type: str) -> Normalizer: ...
        """Raises NoNormalizerError on unsupported (agent, event_type)."""

class IdentityNormalizer:
    """Stub for tests + as a baseline. Requires the envelope's `payload`
    to already contain the canonical Event fields; passes them through.
    Real adapters land in P1-9..P1-11.
    """

# api/hooks.py
router = APIRouter()

@router.post("/hook/{event_type}")
async def handle_hook(
    event_type: str,
    envelope: HookEnvelope,
    request: Request,
) -> dict[str, str]:
    """See module docstring for the four-step contract."""
```

## Death Test Requirements (write and verify red BEFORE production code)

1. **Latency contract violation.** Use a `BlockingPipeline` test fixture whose `ingest` awaits an `asyncio.Event` (forever, until the test releases it). POST a hook event. Assert: response returns within 50ms (pytest-timeout). Then release the event and assert the ingest actually ran. (If a contributor changes `create_task` to `await`, this test deadlocks and pytest-timeout fires.)
2. **Unhandled task exception silently dropped.** Configure `pipeline.ingest` to raise `RuntimeError("simulated FS failure")`. POST a hook. Assert: a structured ERROR log line is emitted (loguru capture) with `event_id`, `error`, AND the test's caplog sees it. Without an `add_done_callback`, asyncio swallows it on GC.
3. **Unknown event_type silently routed to default.** POST `/hook/totally_invented_type`. Assert: response is 422 (envelope rejects, since the canonical `EventType` enum is closed). Without enum validation, the path component would flow into `event_type` field as a string and downstream code would panic at random points.
4. **Path traversal in event_type.** POST `/hook/..%2F..%2Fetc%2Fpasswd`. Assert: response is 404 from FastAPI router (path is normalized) OR 422 if it arrives as a literal value — never 200.
5. **Missing project_id silently fills default.** POST a payload without `project_id`. Assert: 422 with field-level error pointing at `project_id`. Tracker/registry MUST never see a `None` or `""` project_id.
6. **Sequence_number reuse silently overwrites.** POST two events with the same `(session_id, sequence_number)` but different `event_id`. Assert: second one returns 200 (handler fires-and-forgets) BUT a later DB row check shows the integrity error was logged via the done-callback (not silently swallowed). The events table already enforces UNIQUE(session_id, sequence_number); we must surface the violation in logs.
7. **Concurrent shutdown drains in-flight tasks.** Start ingest for a slow event (event released after 200ms). Trigger app lifespan shutdown after 50ms. Assert: shutdown waits up to a bounded time (e.g. 1s) for in-flight tasks; at exit, the slow ingest either completed OR was logged as cancelled — never silently abandoned.
8. **Two events for the same id.** POST event with `id="dup"` twice. First call: 200, ingest schedules. Second call: 200, ingest schedules. Verify the events table ends with one row (idempotency upheld at the storage layer) AND no error log appears (ON CONFLICT DO NOTHING is silent by design).

## Unit Test Requirements

- Happy path: POST a valid envelope → response 200 `{"status": "ok"}` → after a short sleep, raw_trace_store has one file and events table has one row matching the envelope.
- `IdentityNormalizer.supports(agent, event_type)` is true exactly for the canonical EventType enum values + agent="test".
- `NormalizerRegistry.for_(agent, event_type)` raises `NoNormalizerError` with a structured message naming the missing pair.
- `GET /health` continues to work after the hooks router is mounted.
- A request without an `agent` header AND without `agent` in body fails 422.

## Implementation Steps

- [ ] Step 1: STEP 0 — answer the four prerequisite questions
- [ ] Step 2: Write death tests (8 cases)
- [ ] Step 3: Run death tests — red
- [ ] Step 4: Write unit tests
- [ ] Step 5: Run unit tests — red
- [ ] Step 6: Implement `HookEnvelope`, `Normalizer`, `NormalizerRegistry`, `IdentityNormalizer`
- [ ] Step 7: Implement `handle_hook` route. Use a module-level `WeakSet[asyncio.Task]` (or `app.state.in_flight: set`) so lifespan can drain.
- [ ] Step 8: Wire `add_done_callback` that logs exceptions structurally (loguru) — this is the silent-failure plug.
- [ ] Step 9: Update `create_app()` to mount the router; lifespan now awaits in-flight task drain with bounded timeout.
- [ ] Step 10: Run all tests — green
- [ ] Step 11: Write scar report
- [ ] Step 12: Self-iteration (Level 1)
- [ ] Step 13: Re-run tests — no regression

## Expected Scar Report Items

- Potential silent failure: bounded-drain timeout (1s) at shutdown is arbitrary. A burst of slow ingests right before shutdown will be cancelled. Acceptable for Phase 1 (raw trace already on disk via FS-first), but document.
- Potential silent failure: `ON CONFLICT DO NOTHING` swallows duplicate-id ingests. This is correct for retry idempotency, but it also masks a buggy adapter that generates the same id for different events. We rely on (session_id, sequence_number) UNIQUE to catch that — verify it does.
- Assumption to verify: agent header vs body — which is canonical when both present? Plan: body wins, header is fallback for hook scripts that can't easily set body fields.
- Potential shortcut: `IdentityNormalizer` is the only registered normalizer in this change. Without P1-9..P1-11, the server can only ingest pre-normalized payloads. Document that this is intentional.
- Boundary issue: `request.client.host` is not validated against `127.0.0.1` even though we bind to localhost — uvicorn already enforces, but a defense-in-depth check would harden.

## Acceptance Criteria

- All death tests pass (especially the latency contract one — pytest-timeout enforces it)
- All unit tests pass
- `mypy` clean
- Scar report complete
- No `await pipeline.ingest(...)` anywhere in `api/hooks.py`
- Lifespan shutdown drains in-flight tasks with bounded timeout
- `add_done_callback` logging on every scheduled ingest task
