# Task 8: Storage Architecture POC

## Context

Read: overview.md

This task produces a working POC of the dual-layer storage architecture: filesystem for raw traces + SQLite for structured intelligence. The POC must demonstrate read/write/query operations and meet latency targets on real-ish data.

**Depends on:** Task 7 (event schema POC). The storage layer stores events in the schema defined by Task 7.

**Storage requirements (from PRD §9):**

Raw Trace Layer (filesystem):
```
/secondsight/
  /sessions/
    /{session_id}/
      /events/
        {timestamp}_{event_type}.json
      /metadata.json
      /framework_context.json
```

Structured Intelligence Layer (SQLite):
- runtime_events table
- behavior_spans table (Phase 2, but schema should anticipate it)
- sessions table (metadata)

**POC scope:**
- Write events to filesystem + SQLite
- Query single session events by session_id
- Query events by time range
- Latency target: < 500ms for single session query at 1000 session scale
- Must test with real trace data from reference_opensoure

## Files

- Create: `src/secondsight/poc/storage.py` — Dual-layer storage implementation
- Create: `src/secondsight/poc/storage_schema.sql` — SQLite DDL
- Create: `tests/poc/test_storage.py` — Storage tests including real data
- Modify: `tests/poc/conftest.py` — Add storage fixtures and real trace data samples
- Create: `changes/2026-04-24_phase0-feasibility/storage-design.md` — Design notes

## Death Test Requirements

- Test: Storage writes succeed but read returns stale data (filesystem and SQLite out of sync)
- Test: Query latency passes on 10 sessions but exceeds 500ms at 1000 sessions
- Test: Storage handles synthetic test events but fails on real trace data (unexpected sizes, nesting, encoding)
- Test: Concurrent writes to same session produce corrupted data or lost events

## Implementation Steps

- [ ] Step 1: Write death tests — sync failure between layers, latency at scale, real data handling
- [ ] Step 2: Run death tests — verify they fail
- [ ] Step 3: Write unit tests — CRUD operations, query by session_id, query by time range, latency benchmarks
- [ ] Step 4: Run unit tests — verify they fail
- [ ] Step 5: Implement SQLite schema DDL
- [ ] Step 6: Implement dual-layer storage (filesystem write + SQLite index update)
- [ ] Step 7: Implement query operations
- [ ] Step 8: Load real trace data from reference_opensoure for testing
- [ ] Step 9: Run all tests including latency benchmark at 1000 session scale — verify they pass
- [ ] Step 10: Write storage-design.md documenting architecture decisions, scaling limits, and Phase 1 optimization needs
- [ ] Step 11: Write scar report
- [ ] Step 12: Commit

## Expected Scar Report Items

- Potential shortcut: Testing only with small synthetic events instead of real-sized trace data
- Potential shortcut: Skipping concurrent write testing because "Phase 0 is single-user"
- Potential shortcut: Using in-memory SQLite for tests, which hides I/O latency issues
- Assumption to verify: Filesystem event storage scales linearly with session count (directory listing performance)
- Assumption to verify: SQLite can handle the write throughput of real-time event ingestion

## Acceptance Criteria

- Covers: "Silent failure - storage POC passes on synthetic data but fails on real traces"
- Covers: "Success - storage POC handles 1000 sessions within latency target"
