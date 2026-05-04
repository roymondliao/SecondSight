# Task 3: Events Table — SQLAlchemy Core + Repository (P1-3)

## Context

Read: overview.md (esp. events table schema in SD §3.7.5)
Depends on: task-2 (DBEngine)

Implements the **events table** as a single-table-per-event design (SD §3.7.5) and a thin repository over SQLAlchemy Core. The repository exposes only the operations Phase 1 and Phase 2 actually need; we resist building a full DAO surface.

**Plan ref:** P1-3
**SD refs:** §3.7.5 (table schema), §3.7.6 (segment_index), §3.7.7 (sub-agent nesting)

Schema (from SD §3.7.5):

```sql
CREATE TABLE events (
    id               TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    project_id       TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    timestamp        DATETIME NOT NULL,
    sequence_number  INTEGER NOT NULL,
    segment_index    INTEGER NOT NULL,
    sub_agent_id     TEXT,
    depth            INTEGER NOT NULL DEFAULT 0,
    duration_ms      INTEGER,
    token_count      INTEGER,
    data             TEXT NOT NULL,
    UNIQUE(session_id, sequence_number)
);
CREATE INDEX idx_events_session_seq ON events(session_id, sequence_number);
CREATE INDEX idx_events_segment ON events(session_id, segment_index);
CREATE INDEX idx_events_type ON events(session_id, event_type);
CREATE INDEX idx_events_sub_agent ON events(session_id, sub_agent_id);
```

## Files

- Create: `src/secondsight/storage/events_table.py` — SQLAlchemy `Table` definition + DDL helpers
- Create: `src/secondsight/storage/events_repository.py` — `EventsRepository` class
- Create: `tests/storage/test_events_repository.py`

## Public Contract

```python
class EventsRepository:
    def __init__(self, db_engine: DBEngine) -> None: ...

    def create_schema(self) -> None:
        """Idempotent: creates table + indexes if absent."""

    def insert(self, event: Event) -> None:
        """Insert single event. Idempotent on (id) — repeats are no-op."""

    def insert_many(self, events: Sequence[Event]) -> int:
        """Bulk insert; returns rows inserted. Idempotent batch."""

    def get_session_events(self, session_id: str) -> list[Event]:
        """Ordered by sequence_number ascending."""

    def get_segment_events(self, session_id: str, segment_index: int) -> list[Event]: ...

    def get_max_segment_index(self, session_id: str) -> int | None:
        """For SessionTracker.next_segment_index (SD §3.7.6). None if session empty."""

    def exists(self, event_id: str) -> bool: ...
```

## Death Test Requirements

1. **Idempotent INSERT silently overwrites.** Two `insert(event)` calls with the same `id` must NOT raise and must NOT change row data. Test asserts row count = 1 and original data preserved (SQLite `INSERT OR IGNORE` semantics, not `INSERT OR REPLACE`).
2. **`UNIQUE(session_id, sequence_number)` violated by retried event with new id.** Two different `id`s but same `(session_id, sequence_number)` must raise `IntegrityError` — this is the analysis-correctness boundary.
3. **JSON `data` column stores corrupt JSON.** Insert an event whose `data` (after `model_dump_json`) somehow contains a NUL byte or non-UTF-8. SQLite stores TEXT — must round-trip exactly or raise. Test injects edge cases.
4. **`get_max_segment_index` returns 0 when session is empty** (vs `None`). Phase 2 SessionTracker treats `None` as "fresh" and `0` as "one segment exists" — confusing them shifts every subsequent segment_index by 1.
5. **`segment_index` re-derivation under concurrent inserts.** Two writers race to insert events with the same `(session_id, sequence_number+1)`. UNIQUE constraint must let exactly one win; the loser's caller must see `IntegrityError` (not silently get the wrong segment_index).
6. **Schema drift across versions.** Run `create_schema()` against a DB that already has the table with an extra column. Must not raise; must not drop the extra column. (Forward-compat for Phase 2 ALTER TABLE migrations.)

## Unit Test Requirements

- Round-trip insert/get for every event_type from SD §3.7.2
- Indexes are actually created (query `sqlite_master`)
- `get_session_events` ordered by sequence_number
- `get_segment_events` filters correctly across multiple segments
- `insert_many` of 1,000 events completes in < 1 sec on dev hardware (perf gate, not benchmark theatre)
- Sub-agent nesting fields (`sub_agent_id`, `depth`) round-trip correctly including NULL
- `data` JSON round-trip preserves nested structures and unicode

## Implementation Steps

- [ ] Step 1: STEP 0
- [ ] Step 2: Death tests
- [ ] Step 3: Death tests red
- [ ] Step 4: Unit tests
- [ ] Step 5: Unit tests red
- [ ] Step 6: Define `events` Table via SQLAlchemy Core
- [ ] Step 7: Implement Repository methods using parameterized `insert`/`select`
- [ ] Step 8: Use `INSERT OR IGNORE` (SQLite-specific) for idempotency on `id`
- [ ] Step 9: Run all tests — green
- [ ] Step 10: Scar report + Level 1 self-iteration

## Expected Scar Report Items

- Silent failure: `INSERT OR IGNORE` swallows constraint violations including the UNIQUE one — must distinguish `id` collision (silent) from `(session_id, sequence_number)` collision (raise)
- Silent failure: `data` column is unindexed; queries that filter on JSON fields will table-scan. Acceptable for Phase 1 (low row count) but a future bottleneck
- Assumption: SQLite TEXT is UTF-8 by default; non-UTF-8 inserts may corrupt
- Performance: `get_session_events` returns full rows even when callers only need IDs — no projection support yet
- Idempotency boundary: re-INSERT with same id but different data is silently dropped; this protects retries but hides real bugs in upstream code

## Acceptance Criteria

- All death + unit tests pass
- DDL matches SD §3.7.5 byte-for-byte (column types, NOT NULL, UNIQUE, indexes)
- mypy clean
- No raw SQL strings in repository methods (all SQLAlchemy expressions) — DDL is acceptable
