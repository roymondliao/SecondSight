# Task 5: End-to-end integration test (P1-9-int)

## Context

Read: `2-plan.md` §6 (AC-5, AC-9), `acceptance.md` (north-star verification block).

This task validates that the full pipeline — `POST /hook/{type}` → `AdapterRegistry` → `ClaudeCodeAdapter.normalize()` → `SessionTracker.bind()` → `ObservationPipeline.ingest()` → SQLite + RawTraceStore — works end-to-end with verified Claude Code payloads.

**Plan refs:** P1-9 (validation side)
**Depends on:** task-3 (migrated registry wired into server), task-4 (`ClaudeCodeAdapter` registered)

## Files

- Create: `tests/adapters/test_integration_claude_code.py`

## Test scope

Single test function (`test_fidelity_against_fixtures`) that, for every fixture in `tests/fixtures/claude_code/*.json`:

1. Stand up an in-memory FastAPI test client (existing pattern in `tests/api/`).
2. Wrap the fixture's `payload` in a `HookEnvelope` (synthesizing `event_id`, `sequence_number`, `timestamp` since those are envelope-level not Claude-Code-payload-level).
3. POST to `/hook/{event_type}` with `agent="claude_code"`.
4. Assert the response is `200 OK`.
5. Query the per-project SQLite for the just-ingested event.
6. Compare the stored `Event.data` dict against the fixture's `expected_partial_event_data`. Fields in `expected_partial_event_data` MUST be present and equal in `Event.data`.
7. Compute fidelity ratio: `len(matched_fields) / len(expected_fields)`. Assert == 1.0.
8. Privacy assertion: `"PRIVACY_CANARY_DO_NOT_STORE"` does NOT appear in the JSON serialization of `Event.data`.

## Death cases this test instruments

The integration test is itself a death case — it catches the failure mode of:

- "ABC + adapter + fixtures all pass in isolation but the end-to-end wiring is broken" (e.g., adapter not actually registered in server lifespan)
- "Drop_list passes per-adapter unit test but tracker.bind() or pipeline.ingest() somehow re-introduces the dropped field" (cannot happen by design but the test proves it)

## Implementation steps

- [ ] STEP 0
- [ ] Stand up TestClient with `lifespan` enabled (so registry actually boots)
- [ ] Loop over fixtures
- [ ] Synthesize envelopes
- [ ] POST → query → assert
- [ ] Run → green

## Acceptance for this task

- AC-5 (per-fixture round-trip) green via integration path, not just unit path
- North-star fidelity = 1.0 across all P1-floor fixtures
- Test takes < 5s wall time (uses in-memory engine, not double-fork daemon)
- Task-5 scar report committed
