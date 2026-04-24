-- SecondSight Storage Schema v0.1
-- Structured Intelligence Layer (SQLite)
--
-- This schema stores indexed event metadata for fast querying.
-- Full event payloads are stored on the filesystem (dual-layer architecture).
--
-- Assumptions:
-- 1. Session IDs are unique across agents (UUID-based).
--    If not: events from different agents with colliding session IDs
--    will be mixed in queries. Detection: query returns mixed agent_type.
-- 2. Timestamps are ISO 8601 strings and sort lexicographically.
--    If not: time range queries return wrong results.
--    SQLite string comparison works for ISO 8601 with timezone offset.
-- 3. Single writer at a time (Phase 0). WAL mode provides concurrent reads.
--    If not: BUSY errors possible. Mitigated with busy_timeout pragma.

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 5000;

-- Sessions metadata table
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    agent_type      TEXT NOT NULL,
    cwd             TEXT,
    first_event_ts  TEXT,       -- ISO 8601
    last_event_ts   TEXT,       -- ISO 8601
    event_count     INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,  -- ISO 8601
    updated_at      TEXT NOT NULL   -- ISO 8601
);

CREATE INDEX IF NOT EXISTS idx_sessions_agent_type
    ON sessions(agent_type);
CREATE INDEX IF NOT EXISTS idx_sessions_last_event_ts
    ON sessions(last_event_ts);

-- Runtime events table (indexed event metadata)
CREATE TABLE IF NOT EXISTS runtime_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    agent_type      TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    timestamp       TEXT NOT NULL,  -- ISO 8601
    tool_name       TEXT,
    success         INTEGER,       -- 0/1/NULL (SQLite has no boolean)
    duration_ms     INTEGER,
    cwd             TEXT,
    filesystem_path TEXT NOT NULL,  -- Relative path to event JSON on filesystem
    created_at      TEXT NOT NULL,  -- When this row was inserted

    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_events_session_id
    ON runtime_events(session_id);
CREATE INDEX IF NOT EXISTS idx_events_timestamp
    ON runtime_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_events_session_timestamp
    ON runtime_events(session_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_event_type
    ON runtime_events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_agent_type
    ON runtime_events(agent_type);

-- Phase 2 placeholder: behavior_spans table
-- This table is not used in Phase 0 but schema anticipates it.
-- CREATE TABLE IF NOT EXISTS behavior_spans (
--     id              INTEGER PRIMARY KEY AUTOINCREMENT,
--     session_id      TEXT NOT NULL,
--     span_type       TEXT NOT NULL,
--     start_event_id  INTEGER NOT NULL,
--     end_event_id    INTEGER,
--     classification  TEXT,
--     metadata_json   TEXT,
--     FOREIGN KEY (session_id) REFERENCES sessions(session_id),
--     FOREIGN KEY (start_event_id) REFERENCES runtime_events(id),
--     FOREIGN KEY (end_event_id) REFERENCES runtime_events(id)
-- );
