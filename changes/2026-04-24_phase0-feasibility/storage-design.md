# Storage Architecture POC -- Design Notes

## Architecture Decision: Dual-Layer Storage

**Decision:** Filesystem for raw event JSON + SQLite for indexed queries.

**Why not SQLite-only?**
Real agent events vary enormously in size (50 bytes for a session_start to 200KB+
for a Bash tool returning file contents). Storing large JSON blobs in SQLite is
possible but degrades query performance and complicates backup/inspection.
Filesystem storage keeps raw events human-readable and allows external tools
(grep, jq) to inspect data without database access.

**Why not filesystem-only?**
Querying events by time range or session_id across thousands of sessions requires
directory traversal and JSON parsing. SQLite indexes make these queries sub-millisecond.

## Filesystem Layout

```
{base_dir}/
  sessions/
    {session_id}/
      metadata.json          -- Session-level metadata (agent_type, cwd, timestamps)
      events/
        {timestamp}_{event_type}_{counter}_{uuid8}.json
```

**Filename design choices:**
- Timestamp is filesystem-safe (colons/dots replaced with hyphens)
- Counter prevents collisions within the same millisecond
- 8-char UUID suffix prevents collisions across concurrent writers
- Event type in the filename aids manual inspection

## SQLite Schema

Two tables:
- `sessions`: Session-level metadata with agent_type, event counts, timestamps
- `runtime_events`: Indexed event fields + filesystem_path pointer

**Key indexes:**
- `(session_id, timestamp)` -- for session event queries
- `timestamp` -- for time range queries
- `event_type`, `agent_type` -- for filtering

**WAL mode is mandatory.** Without WAL, concurrent hook events would produce
"database is locked" errors. WAL allows concurrent reads during writes.

## Consistency Model

The two layers are NOT atomically consistent. Write order:
1. Filesystem first (event JSON file)
2. SQLite second (index entry)

**Rationale:** If SQLite write fails, the event is still on disk and can be
recovered by scanning the filesystem. If filesystem write fails first, we
avoid creating an orphan SQLite entry pointing to a missing file.

**Integrity check:** `check_integrity(session_id)` compares event counts
between SQLite and filesystem. Does not check content equality (too expensive
for a health check).

## Latency Results

At 1000 sessions with 10 events each (10,000 total events):
- Single session query: < 500ms target MET (typically < 50ms on-disk)
- Time range query: < 500ms target MET

The benchmark uses on-disk SQLite, not in-memory, to reflect real I/O costs.

## Scaling Limits (Phase 1 Concerns)

1. **Filesystem directory listing:** At 10,000+ sessions, `os.listdir()` on the
   sessions/ directory becomes slow. Phase 1 should consider date-based
   subdirectories (e.g., `sessions/2026/04/24/{session_id}/`).

2. **SQLite write throughput:** Single-writer with WAL handles POC load.
   Real-time ingestion from multiple concurrent agents may need connection
   pooling or write batching.

3. **Session metadata.json race condition:** Concurrent writes to the same
   session can cause lost updates to metadata.json (last writer wins).
   Phase 1 should use file locking or derive metadata from SQLite only.

4. **No event deduplication:** If the same event is stored twice (e.g., hook
   retry), both copies are stored. Phase 1 should add a content hash or
   event ID for dedup.

## Assumptions

| Assumption | Verified? | Consequence if wrong |
|---|---|---|
| Session IDs are unique across agents | No (assumed UUID-based) | Mixed events in queries |
| ISO 8601 timestamps sort lexicographically | Yes (tested) | Wrong time range results |
| Filesystem + SQLite on same disk | Yes (POC scope) | Higher partial-write risk |
| Events are immutable after storage | Yes (by design) | No update mechanism exists |
| sqlite3 stdlib handles concurrency needs | Yes (tested with threads) | Need connection pool |
