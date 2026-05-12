"""
Dual-Layer Storage Architecture POC -- SecondSight Phase 0

Architecture:
  Layer 1 (Filesystem): Raw event JSON files organized by session.
  Layer 2 (SQLite): Indexed event metadata for fast querying.

Filesystem layout:
  {base_dir}/
    sessions/
      {session_id}/
        metadata.json
        framework_context.json  (Phase 1)
        events/
          {timestamp}_{event_type}_{counter}.json

SQLite schema:
  - sessions: session-level metadata (agent_type, event_count, timestamps)
  - runtime_events: indexed event fields (session_id, agent_type, event_type,
    timestamp, tool_name, success, duration_ms, filesystem_path)

Assumptions:
  1. Session IDs are unique across agents. If not: events from different
     agents with colliding session_ids will be mixed in queries.
  2. ISO 8601 timestamps sort lexicographically. If not: time range queries
     and filesystem filename ordering will be wrong.
  3. Filesystem and SQLite are on the same disk. If not: partial write
     failures become more likely (one layer succeeds, the other fails).
  4. Events are immutable after storage. If not: the dual-layer design
     has no update mechanism and will produce stale reads.
  5. Python's sqlite3 module handles our concurrency needs (WAL mode +
     busy_timeout). If not: we need a connection pool or external DB.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from secondsight.poc.event_schema import (
    SecondSightEvent,
    event_from_dict,
    event_to_dict,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_UNKNOWN_SESSION = "_unknown_session"


@dataclass
class StorageConfig:
    """Configuration for DualLayerStorage.

    base_dir: Root directory for filesystem storage.
    use_memory_db: If True, use in-memory SQLite (for fast unit tests ONLY).
                   Death tests MUST use use_memory_db=False.
    db_path: Override SQLite database file path. Default: {base_dir}/secondsight.db
    """

    base_dir: str
    use_memory_db: bool = False
    db_path: str | None = None

    @property
    def effective_db_path(self) -> str:
        if self.use_memory_db:
            return ":memory:"
        if self.db_path:
            return self.db_path
        return os.path.join(self.base_dir, "secondsight.db")


# ---------------------------------------------------------------------------
# Integrity check result
# ---------------------------------------------------------------------------


@dataclass
class IntegrityResult:
    """Result of an integrity check between filesystem and SQLite."""

    session_id: str
    is_consistent: bool
    sqlite_count: int
    filesystem_count: int
    missing_in_sqlite: list[str] | None = None
    missing_in_filesystem: list[str] | None = None


# ---------------------------------------------------------------------------
# DualLayerStorage
# ---------------------------------------------------------------------------


class DualLayerStorage:
    """Dual-layer storage: filesystem for raw events, SQLite for indexed queries.

    Thread safety:
    - SQLite writes are serialized via a threading lock.
    - Filesystem writes are safe because each event gets a unique file path
      (timestamp + counter + uuid suffix to avoid collisions).
    - The lock does NOT span both layers. If SQLite write succeeds but
      filesystem write fails (or vice versa), the layers will be out of sync.
      The check_integrity() method can detect this.

    This is a known limitation for Phase 0 (single-user). Phase 1 should
    consider a write-ahead approach where both writes are logged before
    committing, so partial failures can be recovered.
    """

    def __init__(self, config: StorageConfig) -> None:
        self._config = config
        self._write_lock = threading.Lock()
        # Per-session file counter to avoid filename collisions
        # when multiple events arrive in the same millisecond.
        self._file_counters: dict[str, int] = {}
        self._counter_lock = threading.Lock()

        # Create directory structure
        os.makedirs(os.path.join(config.base_dir, "sessions"), exist_ok=True)

        # Initialize SQLite
        self._db = self._init_db()

    def _init_db(self) -> sqlite3.Connection:
        """Initialize SQLite database with schema."""
        db_path = self._config.effective_db_path
        # check_same_thread=False is needed for multi-threaded access.
        # We protect writes with our own lock.
        conn = sqlite3.connect(db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row

        # Apply pragmas
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 5000")

        # Create tables
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id      TEXT PRIMARY KEY,
                agent_type      TEXT NOT NULL,
                cwd             TEXT,
                first_event_ts  TEXT,
                last_event_ts   TEXT,
                event_count     INTEGER NOT NULL DEFAULT 0,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sessions_agent_type ON sessions(agent_type)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_last_event_ts ON sessions(last_event_ts)"
        )

        conn.execute("""
            CREATE TABLE IF NOT EXISTS runtime_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id      TEXT NOT NULL,
                agent_type      TEXT NOT NULL,
                event_type      TEXT NOT NULL,
                timestamp       TEXT NOT NULL,
                tool_name       TEXT,
                success         INTEGER,
                duration_ms     INTEGER,
                cwd             TEXT,
                filesystem_path TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(session_id)
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_session_id ON runtime_events(session_id)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_timestamp ON runtime_events(timestamp)")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_session_timestamp "
            "ON runtime_events(session_id, timestamp)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_event_type ON runtime_events(event_type)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_events_agent_type ON runtime_events(agent_type)"
        )

        conn.commit()
        return conn

    def close(self) -> None:
        """Close the SQLite connection."""
        if self._db:
            self._db.close()

    # ------------------------------------------------------------------
    # Shared SQLite helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _success_to_int(success: bool | None) -> int | None:
        if success is True:
            return 1
        if success is False:
            return 0
        return None

    def _upsert_event_to_sqlite(
        self,
        event: SecondSightEvent,
        session_id: str,
        timestamp: str,
        rel_path: str,
        now: str,
    ) -> None:
        """Insert event row + upsert session row. Caller must hold _write_lock."""
        self._db.execute(
            """INSERT INTO runtime_events
               (session_id, agent_type, event_type, timestamp,
                tool_name, success, duration_ms, cwd,
                filesystem_path, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                session_id,
                event.agent_type or "",
                event.event_type or "",
                timestamp,
                event.tool_name,
                self._success_to_int(event.success),
                event.duration_ms,
                event.cwd,
                rel_path,
                now,
            ),
        )

        self._db.execute(
            """INSERT INTO sessions
               (session_id, agent_type, cwd, first_event_ts,
                last_event_ts, event_count, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT(session_id) DO UPDATE SET
                 first_event_ts = CASE
                   WHEN excluded.first_event_ts < sessions.first_event_ts
                        OR sessions.first_event_ts IS NULL
                   THEN excluded.first_event_ts
                   ELSE sessions.first_event_ts
                 END,
                 last_event_ts = CASE
                   WHEN excluded.last_event_ts > sessions.last_event_ts
                        OR sessions.last_event_ts IS NULL
                   THEN excluded.last_event_ts
                   ELSE sessions.last_event_ts
                 END,
                 event_count = sessions.event_count + 1,
                 cwd = COALESCE(excluded.cwd, sessions.cwd),
                 updated_at = excluded.updated_at""",
            (
                session_id,
                event.agent_type or "",
                event.cwd,
                timestamp,
                timestamp,
                now,
                now,
            ),
        )

    # ------------------------------------------------------------------
    # Directory helpers
    # ------------------------------------------------------------------

    def _session_dir(self, session_id: str) -> str:
        """Return the session root directory path."""
        return os.path.join(self._config.base_dir, "sessions", session_id)

    def _session_events_dir(self, session_id: str) -> str:
        """Return the session events directory path."""
        return os.path.join(self._session_dir(session_id), "events")

    # ------------------------------------------------------------------
    # File naming
    # ------------------------------------------------------------------

    def _next_counter(self, session_id: str) -> int:
        """Get and increment the file counter for a session (thread-safe)."""
        with self._counter_lock:
            count = self._file_counters.get(session_id, 0)
            self._file_counters[session_id] = count + 1
            return count

    def _event_filename(self, event: SecondSightEvent, session_id: str) -> str:
        """Generate a unique filename for an event.

        Format: {timestamp_compact}_{event_type}_{counter}.json

        The counter prevents collisions when multiple events have the
        same timestamp (common when events arrive in bursts).
        A short uuid suffix adds further collision resistance for
        concurrent writers.
        """
        ts = event.timestamp or datetime.now(timezone.utc).isoformat()
        # Compact timestamp: replace colons and dots for filesystem safety
        ts_compact = re.sub(r"[:.+]", "-", ts)
        event_type = event.event_type or "unknown"
        counter = self._next_counter(session_id)
        short_id = uuid.uuid4().hex[:8]
        return f"{ts_compact}_{event_type}_{counter:04d}_{short_id}.json"

    # ------------------------------------------------------------------
    # Core write operations
    # ------------------------------------------------------------------

    def store_event(self, event: SecondSightEvent) -> str:
        """Store a single event to both layers.

        Returns the filesystem path (relative to base_dir) of the stored event.

        Write order: filesystem first, then SQLite.
        Rationale: if SQLite write fails, the event is still on disk
        and can be recovered. If filesystem write fails first, we avoid
        creating an orphan SQLite entry pointing to a missing file.

        This is NOT atomic across layers. check_integrity() can detect
        inconsistencies.
        """
        session_id = event.session_id or _UNKNOWN_SESSION
        timestamp = event.timestamp or datetime.now(timezone.utc).isoformat()

        if event.session_id is None:
            logger.warning(
                "Event stored without session_id (event_type=%s, agent_type=%s). "
                "Falling back to '%s'. This event will be hard to query by session.",
                event.event_type,
                event.agent_type,
                _UNKNOWN_SESSION,
            )

        # Ensure event has the values we'll use for storage
        if event.timestamp is None:
            event.timestamp = timestamp
        if event.session_id is None:
            event.session_id = session_id

        # --- Layer 1: Filesystem ---
        events_dir = self._session_events_dir(session_id)
        os.makedirs(events_dir, exist_ok=True)

        filename = self._event_filename(event, session_id)
        filepath = os.path.join(events_dir, filename)

        event_dict = event_to_dict(event)
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(event_dict, f, ensure_ascii=False, indent=2)

        # Write/update session metadata.json
        self._write_session_metadata_file(session_id, event)

        # Relative path for SQLite storage (relative to base_dir)
        rel_path = os.path.relpath(filepath, self._config.base_dir)

        # --- Layer 2: SQLite ---
        now = datetime.now(timezone.utc).isoformat()

        with self._write_lock:
            self._upsert_event_to_sqlite(event, session_id, timestamp, rel_path, now)
            self._db.commit()

        return rel_path

    def store_events_batch(self, events: list[SecondSightEvent]) -> list[str]:
        """Store multiple events efficiently.

        Uses a single SQLite transaction for all events.
        Filesystem writes are done individually (no transactional guarantee).

        Returns list of filesystem paths for stored events.
        """
        if not events:
            return []

        paths: list[str] = []
        now = datetime.now(timezone.utc).isoformat()

        # Pre-process: filesystem writes (outside the lock)
        fs_records: list[tuple[SecondSightEvent, str, str]] = []
        for event in events:
            session_id = event.session_id or _UNKNOWN_SESSION
            timestamp = event.timestamp or datetime.now(timezone.utc).isoformat()

            if event.timestamp is None:
                event.timestamp = timestamp
            if event.session_id is None:
                event.session_id = session_id

            events_dir = self._session_events_dir(session_id)
            os.makedirs(events_dir, exist_ok=True)

            filename = self._event_filename(event, session_id)
            filepath = os.path.join(events_dir, filename)

            event_dict = event_to_dict(event)
            with open(filepath, "w", encoding="utf-8") as f:
                json.dump(event_dict, f, ensure_ascii=False, indent=2)

            rel_path = os.path.relpath(filepath, self._config.base_dir)
            fs_records.append((event, session_id, rel_path))
            paths.append(rel_path)

        # SQLite writes (inside the lock, single transaction)
        with self._write_lock:
            try:
                for event, session_id, rel_path in fs_records:
                    timestamp = event.timestamp or now
                    self._upsert_event_to_sqlite(event, session_id, timestamp, rel_path, now)
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise

        # Write session metadata files — use last event per session
        # (later events have more context than earlier ones)
        last_event_per_session: dict[str, SecondSightEvent] = {}
        for event, session_id, _ in fs_records:
            last_event_per_session[session_id] = event
        for session_id, event in last_event_per_session.items():
            self._write_session_metadata_file(session_id, event)

        return paths

    def _write_session_metadata_file(self, session_id: str, event: SecondSightEvent) -> None:
        """Write or update the session metadata.json file on filesystem.

        NOT thread-safe for concurrent writes to the same session_id.
        This is acceptable for Phase 0 (single-writer). Phase 1 must add
        file locking if concurrent session writes become possible.
        """
        session_dir = self._session_dir(session_id)
        os.makedirs(session_dir, exist_ok=True)
        metadata_path = os.path.join(session_dir, "metadata.json")

        # Read existing metadata or create new
        metadata: dict[str, Any] = {}
        if os.path.exists(metadata_path):
            try:
                with open(metadata_path, "r", encoding="utf-8") as f:
                    metadata = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(
                    "Corrupt or unreadable metadata.json at %s: %s. Resetting to fresh metadata.",
                    metadata_path,
                    e,
                )
                metadata = {}

        metadata["session_id"] = session_id
        metadata["agent_type"] = event.agent_type or metadata.get("agent_type", "")
        if event.cwd:
            metadata["cwd"] = event.cwd
        metadata["updated_at"] = datetime.now(timezone.utc).isoformat()
        if "created_at" not in metadata:
            metadata["created_at"] = metadata["updated_at"]

        with open(metadata_path, "w", encoding="utf-8") as f:
            json.dump(metadata, f, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # Query operations
    # ------------------------------------------------------------------

    def query_session_events(self, session_id: str) -> list[dict[str, Any]]:
        """Query all events for a session, ordered by timestamp.

        Returns list of dicts with indexed fields from SQLite.
        Use load_full_event() to get the complete event from filesystem.
        """
        cursor = self._db.execute(
            """SELECT id, session_id, agent_type, event_type, timestamp,
                      tool_name, success, duration_ms, cwd, filesystem_path
               FROM runtime_events
               WHERE session_id = ?
               ORDER BY timestamp ASC, id ASC""",
            (session_id,),
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def query_events_by_time_range(self, start_ts: str, end_ts: str) -> list[dict[str, Any]]:
        """Query events within a time range (inclusive start, exclusive end).

        Args:
            start_ts: ISO 8601 start timestamp (inclusive)
            end_ts: ISO 8601 end timestamp (exclusive)

        Returns list of dicts with indexed fields from SQLite.
        """
        cursor = self._db.execute(
            """SELECT id, session_id, agent_type, event_type, timestamp,
                      tool_name, success, duration_ms, cwd, filesystem_path
               FROM runtime_events
               WHERE timestamp >= ? AND timestamp < ?
               ORDER BY timestamp ASC, id ASC""",
            (start_ts, end_ts),
        )
        rows = cursor.fetchall()
        return [dict(row) for row in rows]

    def load_full_event(self, session_id: str, filesystem_path: str) -> SecondSightEvent | None:
        """Load the complete event from filesystem.

        Args:
            session_id: Session ID — used to validate the path belongs to this session
            filesystem_path: Relative path from base_dir to the event JSON file

        Returns:
            SecondSightEvent if the file exists and is valid JSON, None otherwise.
            Does NOT raise on missing file -- returns None to allow callers
            to detect and handle filesystem gaps.
        """
        expected_prefix = os.path.join("sessions", session_id, "")
        if not filesystem_path.startswith(expected_prefix):
            logger.warning(
                "Path %s does not belong to session %s (expected prefix: %s). "
                "Possible cross-session data leak.",
                filesystem_path,
                session_id,
                expected_prefix,
            )
            return None

        abs_path = os.path.join(self._config.base_dir, filesystem_path)
        if not os.path.exists(abs_path):
            logger.warning(
                "Filesystem event file missing: %s (session: %s). "
                "SQLite index has an entry but filesystem does not. "
                "This indicates a sync gap between storage layers.",
                abs_path,
                session_id,
            )
            return None

        try:
            with open(abs_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return event_from_dict(data)
        except (json.JSONDecodeError, OSError) as e:
            logger.error(
                "Failed to load event from filesystem: %s (session: %s): %s",
                abs_path,
                session_id,
                e,
            )
            return None

    # ------------------------------------------------------------------
    # Filesystem scan (for integrity checks and recovery)
    # ------------------------------------------------------------------

    def scan_filesystem_events(self, session_id: str) -> list[str]:
        """Scan filesystem for all event files in a session.

        Returns list of filenames (not full paths).
        This bypasses SQLite entirely -- used for integrity checks.
        """
        events_dir = self._session_events_dir(session_id)
        if not os.path.exists(events_dir):
            return []
        return sorted(f for f in os.listdir(events_dir) if f.endswith(".json"))

    # ------------------------------------------------------------------
    # Integrity check
    # ------------------------------------------------------------------

    def check_integrity(self, session_id: str) -> IntegrityResult:
        """Check that filesystem and SQLite are consistent for a session.

        Compares event count in SQLite vs filesystem.
        Does NOT check content equality (that would require loading every file).
        """
        # SQLite count
        cursor = self._db.execute(
            "SELECT COUNT(*) FROM runtime_events WHERE session_id = ?",
            (session_id,),
        )
        sqlite_count = cursor.fetchone()[0]

        # Filesystem count
        fs_files = self.scan_filesystem_events(session_id)
        fs_count = len(fs_files)

        is_consistent = sqlite_count == fs_count

        # Detail: which paths are missing?
        missing_in_fs: list[str] = []
        missing_in_sqlite: list[str] = []

        if not is_consistent:
            # Get all filesystem_path values from SQLite for this session
            cursor = self._db.execute(
                "SELECT filesystem_path FROM runtime_events WHERE session_id = ?",
                (session_id,),
            )
            sqlite_paths = {os.path.basename(row[0]) for row in cursor.fetchall()}

            fs_set = set(fs_files)

            # Note: sqlite_paths are based on the basename of the relative path
            # stored in SQLite. We compare basenames since that's what's on disk.
            missing_in_fs = sorted(sqlite_paths - fs_set)
            missing_in_sqlite = sorted(fs_set - sqlite_paths)

        return IntegrityResult(
            session_id=session_id,
            is_consistent=is_consistent,
            sqlite_count=sqlite_count,
            filesystem_count=fs_count,
            missing_in_sqlite=missing_in_sqlite if not is_consistent else None,
            missing_in_filesystem=missing_in_fs if not is_consistent else None,
        )

    # ------------------------------------------------------------------
    # Recovery: re-index filesystem events missing from SQLite
    # ------------------------------------------------------------------

    def recover_session(self, session_id: str) -> int:
        """Re-index filesystem events that are missing from SQLite.

        Scans the filesystem for event files, loads each one, and inserts
        a SQLite row for any file not already indexed.

        Returns the number of recovered events.

        This is the primary recovery mechanism for the non-atomic write
        scenario: process crashed after filesystem write but before SQLite
        commit.
        """
        # Get filesystem files
        events_dir = self._session_events_dir(session_id)
        if not os.path.exists(events_dir):
            return 0

        fs_files = sorted(f for f in os.listdir(events_dir) if f.endswith(".json"))

        # Get already-indexed paths from SQLite
        cursor = self._db.execute(
            "SELECT filesystem_path FROM runtime_events WHERE session_id = ?",
            (session_id,),
        )
        indexed_basenames = {os.path.basename(row[0]) for row in cursor.fetchall()}

        recovered = 0
        failed = 0
        now = datetime.now(timezone.utc).isoformat()

        for filename in fs_files:
            if filename in indexed_basenames:
                continue

            filepath = os.path.join(events_dir, filename)
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                event = event_from_dict(data)
            except (json.JSONDecodeError, OSError) as e:
                logger.error(
                    "Recovery: failed to load event file %s: %s",
                    filepath,
                    e,
                )
                failed += 1
                continue

            rel_path = os.path.relpath(filepath, self._config.base_dir)
            timestamp = event.timestamp or now

            with self._write_lock:
                self._upsert_event_to_sqlite(event, session_id, timestamp, rel_path, now)
                self._db.commit()

            recovered += 1

        if recovered > 0 or failed > 0:
            logger.info(
                "Recovery for session %s: re-indexed %d events, %d files unrecoverable",
                session_id,
                recovered,
                failed,
            )

        return recovered
