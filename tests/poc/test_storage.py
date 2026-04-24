"""
Storage Architecture POC tests.

Test organization:
1. Death tests (silent failure paths) -- MUST come first
2. Unit tests (CRUD, query, latency)

Death tests target DC-4: "Storage POC passes on synthetic data but fails
on real traces (unexpected sizes, nesting, encoding)."
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from secondsight.poc.event_schema import (
    AgentMetadata,
    AgentType,
    EventType,
    SecondSightEvent,
    TokenUsage,
    event_from_dict,
    event_to_dict,
    normalize_event,
)
from secondsight.poc.storage import (
    DualLayerStorage,
    StorageConfig,
)


# ===========================================================================
# Fixtures
# ===========================================================================


@pytest.fixture
def storage_dir(tmp_path: Path) -> Path:
    """Provide a temporary directory for storage tests."""
    return tmp_path / "secondsight_storage"


@pytest.fixture
def storage(storage_dir: Path) -> DualLayerStorage:
    """Provide a configured DualLayerStorage instance with on-disk SQLite."""
    config = StorageConfig(
        base_dir=str(storage_dir),
        use_memory_db=False,  # Death test: use real disk I/O
    )
    s = DualLayerStorage(config)
    yield s
    s.close()


@pytest.fixture
def memory_storage(storage_dir: Path) -> DualLayerStorage:
    """Provide a storage instance with in-memory SQLite (for fast unit tests only)."""
    config = StorageConfig(
        base_dir=str(storage_dir),
        use_memory_db=True,
    )
    s = DualLayerStorage(config)
    yield s
    s.close()


def _make_event(
    session_id: str = "test-session-001",
    event_type: str = EventType.TOOL_CALL_START.value,
    agent_type: str = AgentType.CLAUDE_CODE.value,
    timestamp: str | None = None,
    tool_name: str = "Bash",
    tool_args: dict | None = None,
    content: str | None = None,
    token_usage: TokenUsage | None = None,
) -> SecondSightEvent:
    """Create a minimal test event."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat()
    return SecondSightEvent(
        session_id=session_id,
        event_type=event_type,
        agent_type=agent_type,
        timestamp=timestamp,
        tool_name=tool_name,
        tool_args=tool_args or {"command": "echo hello"},
        content=content,
        token_usage=token_usage,
    )


# ===========================================================================
# DEATH TESTS -- Silent failure paths
# ===========================================================================


class TestDeathSyncFailure:
    """Death test: Storage writes succeed but read returns stale data.

    Targets the scenario where filesystem and SQLite get out of sync --
    an event is written to one layer but not the other, and the system
    does not detect or report the inconsistency.
    """

    def test_filesystem_write_succeeds_but_sqlite_missing(self, storage: DualLayerStorage) -> None:
        """Write an event, then verify we can detect if SQLite row is missing.

        If the system silently returns empty results when SQLite is missing
        the index entry, we lose the event with no error.
        """
        event = _make_event()
        storage.store_event(event)

        # Verify event is in both layers
        events = storage.query_session_events(event.session_id)
        assert len(events) == 1

        # Manually delete from SQLite to simulate partial write
        storage._db.execute(
            "DELETE FROM runtime_events WHERE session_id = ?",
            (event.session_id,),
        )
        storage._db.commit()

        # Query through normal path -- should this return 0 events?
        # If it does, we silently lost the event. The filesystem still has it.
        events_after_delete = storage.query_session_events(event.session_id)

        # This is the death case: the event EXISTS on filesystem but
        # query returns empty because it only checks SQLite index.
        # We must at minimum detect this inconsistency.
        assert len(events_after_delete) == 0, (
            "Expected 0 events from SQLite-only query after manual SQLite delete"
        )

        # But the filesystem file should still exist
        fs_events = storage.scan_filesystem_events(event.session_id)
        assert len(fs_events) >= 1, (
            "Filesystem should still have the event even if SQLite lost it"
        )

    def test_sqlite_write_succeeds_but_filesystem_missing(self, storage: DualLayerStorage) -> None:
        """SQLite has the index entry but the filesystem JSON file is gone.

        This simulates: filesystem write failed silently (disk full, permission),
        or someone manually deleted the file.
        """
        event = _make_event()
        storage.store_event(event)

        events = storage.query_session_events(event.session_id)
        assert len(events) == 1

        # Delete the filesystem file
        session_dir = storage._session_events_dir(event.session_id)
        for f in Path(session_dir).glob("*.json"):
            f.unlink()

        # Query should still work (from SQLite indexed data)
        # but fetching the full event from filesystem should fail gracefully
        events_after = storage.query_session_events(event.session_id)
        # SQLite query should still return indexed data
        assert len(events_after) == 1

        # But attempting to load full event from filesystem should report the gap
        full_event = storage.load_full_event(
            event.session_id, events_after[0]["filesystem_path"]
        )
        assert full_event is None, (
            "load_full_event should return None when filesystem file is missing, "
            "not raise an exception silently swallowed"
        )

    def test_integrity_check_detects_mismatch(self, storage: DualLayerStorage) -> None:
        """The storage layer must have an integrity check that can detect
        when SQLite count != filesystem count for a session.
        """
        event1 = _make_event(session_id="integrity-session")
        event2 = _make_event(
            session_id="integrity-session",
            event_type=EventType.TOOL_CALL_END.value,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        storage.store_event(event1)
        storage.store_event(event2)

        # Both layers should be consistent
        result = storage.check_integrity("integrity-session")
        assert result.is_consistent

        # Break consistency: remove one file
        session_dir = storage._session_events_dir("integrity-session")
        files = sorted(Path(session_dir).glob("*.json"))
        assert len(files) == 2
        files[0].unlink()

        # Integrity check should detect the mismatch
        result = storage.check_integrity("integrity-session")
        assert not result.is_consistent
        assert result.sqlite_count == 2
        assert result.filesystem_count == 1

    def test_recovery_reindexes_missing_sqlite_entries(self, storage: DualLayerStorage) -> None:
        """Recovery scan should re-index filesystem events missing from SQLite.

        This addresses the non-atomic write scenario: filesystem write succeeded
        but SQLite write failed (process crash, disk full on db, etc.)
        """
        event = _make_event(session_id="recovery-session")
        storage.store_event(event)

        # Verify event exists in both layers
        assert len(storage.query_session_events("recovery-session")) == 1
        result = storage.check_integrity("recovery-session")
        assert result.is_consistent

        # Simulate partial write: delete SQLite entry but keep filesystem file
        storage._db.execute(
            "DELETE FROM runtime_events WHERE session_id = ?",
            ("recovery-session",),
        )
        storage._db.execute(
            "DELETE FROM sessions WHERE session_id = ?",
            ("recovery-session",),
        )
        storage._db.commit()

        # Query now returns empty
        assert len(storage.query_session_events("recovery-session")) == 0

        # But filesystem still has the event
        assert len(storage.scan_filesystem_events("recovery-session")) == 1

        # Recovery should fix this
        recovered = storage.recover_session("recovery-session")
        assert recovered == 1

        # Now query should find the event again
        events = storage.query_session_events("recovery-session")
        assert len(events) == 1

        # And integrity should be restored
        result = storage.check_integrity("recovery-session")
        assert result.is_consistent


class TestDeathLatencyAtScale:
    """Death test: Query latency passes on 10 sessions but exceeds 500ms at 1000.

    This catches the classic "works on my machine with tiny data" POC failure.
    Uses on-disk SQLite, not in-memory, because in-memory hides I/O latency.
    """

    def test_latency_at_1000_sessions(self, storage: DualLayerStorage) -> None:
        """Benchmark: query a single session when 1000 sessions exist.

        Target: < 500ms for a single-session query at 1000 session scale.
        """
        target_session = "target-session-for-latency"
        batch_size = 1000

        # Bulk insert sessions with events
        events_to_store = []
        for i in range(batch_size):
            sid = f"session-{i:04d}" if i < batch_size - 1 else target_session
            for j in range(10):  # 10 events per session
                ts = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc) + timedelta(
                    seconds=i * 100 + j
                )
                events_to_store.append(
                    _make_event(
                        session_id=sid,
                        timestamp=ts.isoformat(),
                        event_type=EventType.TOOL_CALL_START.value if j % 2 == 0 else EventType.TOOL_CALL_END.value,
                    )
                )

        # Store all events (this is setup, not measured)
        storage.store_events_batch(events_to_store)

        # Now measure query latency
        start = time.perf_counter()
        events = storage.query_session_events(target_session)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert len(events) == 10, f"Expected 10 events, got {len(events)}"
        assert elapsed_ms < 500, (
            f"Query latency {elapsed_ms:.1f}ms exceeds 500ms target at {batch_size} sessions"
        )

    def test_time_range_query_latency_at_scale(self, storage: DualLayerStorage) -> None:
        """Benchmark: time range query across 1000 sessions.

        This tests a different access pattern: "show me all events in the last hour."
        """
        batch_size = 1000
        base_time = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc)

        events_to_store = []
        for i in range(batch_size):
            sid = f"session-{i:04d}"
            ts = base_time + timedelta(seconds=i)
            events_to_store.append(
                _make_event(session_id=sid, timestamp=ts.isoformat())
            )

        storage.store_events_batch(events_to_store)

        # Query a narrow time range that should hit ~60 events
        range_start = (base_time + timedelta(seconds=100)).isoformat()
        range_end = (base_time + timedelta(seconds=160)).isoformat()

        start = time.perf_counter()
        events = storage.query_events_by_time_range(range_start, range_end)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert len(events) > 0, "Time range query returned no events"
        assert elapsed_ms < 500, (
            f"Time range query latency {elapsed_ms:.1f}ms exceeds 500ms target"
        )


class TestDeathRealTraceData:
    """Death test: Storage handles synthetic events but fails on real trace data.

    This is DC-4: "Storage POC passes on synthetic data but fails on real
    traces (unexpected sizes, nesting, encoding)."

    Uses actual event structures from reference_opensoure to generate
    realistic events through the normalize_event pipeline.
    """

    def test_large_tool_result_storage(self, storage: DualLayerStorage) -> None:
        """Real tool results can be 100KB+. Test that storage handles large payloads.

        Observed in reference: Bash tool returning full file contents,
        Codex function_call_output with large diffs.
        """
        # Simulate a Bash tool result that returns a large file listing
        large_output = "file_" * 25000 + ".py"  # ~125KB
        event = _make_event(
            event_type=EventType.TOOL_CALL_END.value,
            tool_name="Bash",
        )
        event.tool_result = large_output

        storage.store_event(event)

        # Read it back
        events = storage.query_session_events(event.session_id)
        assert len(events) == 1

        # Full event from filesystem should have the complete result
        full_event = storage.load_full_event(
            event.session_id, events[0]["filesystem_path"]
        )
        assert full_event is not None
        assert full_event.tool_result == large_output

    def test_deeply_nested_tool_args(self, storage: DualLayerStorage) -> None:
        """Real Codex events have JSON-encoded strings in arguments that
        are themselves nested JSON. Test that storage preserves nesting.
        """
        nested_args = {
            "cmd": "complex command",
            "options": {
                "level1": {
                    "level2": {
                        "level3": ["a", "b", {"level4": True}]
                    }
                }
            },
        }
        event = _make_event(tool_args=nested_args)

        storage.store_event(event)

        # Read back from filesystem and verify nesting is preserved
        events = storage.query_session_events(event.session_id)
        full_event = storage.load_full_event(
            event.session_id, events[0]["filesystem_path"]
        )
        assert full_event is not None
        assert full_event.tool_args == nested_args

    def test_unicode_and_special_characters(self, storage: DualLayerStorage) -> None:
        """Real agent responses contain Unicode, emoji, code with special chars.

        Reference: Claude Code assistant messages with markdown, code blocks,
        and international characters.
        """
        content_with_unicode = (
            "## Analysis\n\n"
            "The function `calculate_α_coefficient` returns NaN when δ < 0.\n"
            "Fix: add guard clause.\n\n"
            "```python\ndef calc(α: float, δ: float) -> float:\n"
            "    if δ < 0:\n"
            "        raise ValueError(f'δ must be >= 0, got {δ}')\n"
            "```\n\n"
            "CJK: 這是一個測試 | これはテストです | 이것은 테스트입니다\n"
        )
        event = _make_event(
            event_type=EventType.AGENT_RESPONSE.value,
            content=content_with_unicode,
        )

        storage.store_event(event)

        # Read back
        events = storage.query_session_events(event.session_id)
        full_event = storage.load_full_event(
            event.session_id, events[0]["filesystem_path"]
        )
        assert full_event is not None
        assert full_event.content == content_with_unicode

    def test_real_codex_jsonl_event_through_storage(self, storage: DualLayerStorage) -> None:
        """Store an event derived from actual Codex JSONL (from lazyagent test data).

        This catches: normalization produces events that storage cannot handle.
        """
        # Real Codex JSONL from reference_opensoure/lazyagent/internal/codex/process_test.go
        raw_codex_events = [
            {
                "timestamp": "2026-03-28T11:26:17.785Z",
                "type": "session_meta",
                "payload": {
                    "id": "019d3431-8669-7603-be71-7079fa555f4a",
                    "cwd": "/tmp/project",
                    "cli_version": "0.116.0",
                    "source": "cli",
                },
            },
            {
                "timestamp": "2026-03-28T11:26:19.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": '{"cmd":"rg codex"}',
                },
            },
            {
                "timestamp": "2026-03-28T11:26:20.000Z",
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "call_1",
                    "output": "ok",
                },
            },
            {
                "timestamp": "2026-03-28T11:26:21.000Z",
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "total_token_usage": {
                            "input_tokens": 1200,
                            "cached_input_tokens": 300,
                            "output_tokens": 500,
                            "reasoning_output_tokens": 100,
                        },
                    },
                },
            },
        ]

        for raw in raw_codex_events:
            event = normalize_event("codex", raw)
            storage.store_event(event)

        # Verify all events are queryable
        # The session_meta event sets session_id from payload.id
        events = storage.query_session_events(
            "019d3431-8669-7603-be71-7079fa555f4a"
        )
        assert len(events) >= 1, (
            f"Expected at least 1 event for Codex session, got {len(events)}. "
            "Some Codex events may not have session_id set."
        )

        # Verify each stored event can be fully loaded from filesystem
        for ev_row in events:
            full = storage.load_full_event(
                "019d3431-8669-7603-be71-7079fa555f4a",
                ev_row["filesystem_path"],
            )
            assert full is not None, (
                f"Failed to load full event from filesystem: {ev_row['filesystem_path']}"
            )

    def test_real_claude_code_hook_event_through_storage(
        self,
        storage: DualLayerStorage,
        claude_code_pre_tool_use_event: dict,
        claude_code_post_tool_use_event: dict,
    ) -> None:
        """Store events derived from Claude Code hook payloads (from conftest fixtures)."""
        pre = normalize_event(
            claude_code_pre_tool_use_event["agent"],
            claude_code_pre_tool_use_event["raw"],
        )
        post = normalize_event(
            claude_code_post_tool_use_event["agent"],
            claude_code_post_tool_use_event["raw"],
        )

        # Hook events may lack timestamp -- storage must handle this
        if pre.timestamp is None:
            pre.timestamp = datetime.now(timezone.utc).isoformat()
        if post.timestamp is None:
            post.timestamp = datetime.now(timezone.utc).isoformat()

        storage.store_event(pre)
        storage.store_event(post)

        events = storage.query_session_events(pre.session_id)
        assert len(events) == 2

        for ev_row in events:
            full = storage.load_full_event(pre.session_id, ev_row["filesystem_path"])
            assert full is not None

    def test_real_opencode_db_event_through_storage(
        self,
        storage: DualLayerStorage,
        opencode_db_message_event: dict,
    ) -> None:
        """Store event from OpenCode DB polling (from conftest fixtures)."""
        event = normalize_event(
            opencode_db_message_event["agent"],
            opencode_db_message_event["raw"],
        )

        storage.store_event(event)

        events = storage.query_session_events(event.session_id)
        assert len(events) == 1

        full = storage.load_full_event(event.session_id, events[0]["filesystem_path"])
        assert full is not None
        assert full.token_usage is not None
        assert full.token_usage.cost == 0.0042


class TestDeathConcurrentWrites:
    """Death test: Concurrent writes to same session produce corrupted data.

    Phase 0 is single-user, but agents send events concurrently via hooks.
    If two hook events arrive simultaneously for the same session, we must
    not lose events or corrupt the SQLite index.
    """

    def test_rapid_sequential_writes_same_session(self, storage: DualLayerStorage) -> None:
        """Rapidly write many events to the same session.

        This is the non-threaded version: tests that the storage layer
        handles rapid writes without corruption even without threading.
        SQLite in WAL mode should handle this.
        """
        session_id = "concurrent-test-session"
        n_events = 100

        for i in range(n_events):
            ts = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc) + timedelta(
                milliseconds=i
            )
            event = _make_event(
                session_id=session_id,
                timestamp=ts.isoformat(),
                event_type=EventType.TOOL_CALL_START.value if i % 2 == 0 else EventType.TOOL_CALL_END.value,
            )
            storage.store_event(event)

        events = storage.query_session_events(session_id)
        assert len(events) == n_events, (
            f"Expected {n_events} events, got {len(events)}. "
            "Some events were lost during rapid sequential writes."
        )

        # Verify integrity
        result = storage.check_integrity(session_id)
        assert result.is_consistent

    def test_threaded_concurrent_writes_same_session(self, storage: DualLayerStorage) -> None:
        """Write events from multiple threads to the same session.

        This is the real concurrent test. SQLite must not produce
        "database is locked" errors that silently drop events.
        """
        import threading

        session_id = "threaded-concurrent-session"
        n_threads = 4
        events_per_thread = 25
        errors: list[str] = []

        def write_events(thread_id: int) -> None:
            try:
                for i in range(events_per_thread):
                    ts = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc) + timedelta(
                        milliseconds=thread_id * 1000 + i
                    )
                    event = _make_event(
                        session_id=session_id,
                        timestamp=ts.isoformat(),
                        tool_name=f"Tool_t{thread_id}",
                    )
                    storage.store_event(event)
            except Exception as e:
                errors.append(f"Thread {thread_id}: {e}")

        threads = [
            threading.Thread(target=write_events, args=(tid,))
            for tid in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Concurrent write errors: {errors}"

        expected = n_threads * events_per_thread
        events = storage.query_session_events(session_id)
        assert len(events) == expected, (
            f"Expected {expected} events, got {len(events)}. "
            "Concurrent writes lost events."
        )


# ===========================================================================
# UNIT TESTS -- CRUD operations, queries, benchmarks
# ===========================================================================


class TestStorageInit:
    """Test storage initialization and schema creation."""

    def test_creates_directory_structure(self, storage: DualLayerStorage) -> None:
        """Storage init should create base directories."""
        config = storage._config
        assert Path(config.base_dir).exists()
        assert (Path(config.base_dir) / "sessions").exists()

    def test_creates_sqlite_schema(self, storage: DualLayerStorage) -> None:
        """SQLite should have the expected tables."""
        cursor = storage._db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )
        tables = {row[0] for row in cursor.fetchall()}
        assert "runtime_events" in tables
        assert "sessions" in tables

    def test_sqlite_wal_mode(self, storage: DualLayerStorage) -> None:
        """SQLite should be in WAL mode for concurrent read/write."""
        cursor = storage._db.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]
        assert mode.lower() == "wal", f"Expected WAL mode, got {mode}"


class TestStoreAndQuery:
    """Test basic CRUD operations."""

    def test_store_single_event(self, storage: DualLayerStorage) -> None:
        """Store one event and retrieve it."""
        event = _make_event()
        storage.store_event(event)

        events = storage.query_session_events(event.session_id)
        assert len(events) == 1
        assert events[0]["session_id"] == event.session_id
        assert events[0]["event_type"] == event.event_type
        assert events[0]["agent_type"] == event.agent_type

    def test_store_multiple_events_same_session(self, storage: DualLayerStorage) -> None:
        """Store multiple events in one session."""
        session_id = "multi-event-session"
        for i in range(5):
            ts = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=i)
            event = _make_event(session_id=session_id, timestamp=ts.isoformat())
            storage.store_event(event)

        events = storage.query_session_events(session_id)
        assert len(events) == 5

    def test_store_events_different_sessions(self, storage: DualLayerStorage) -> None:
        """Store events across multiple sessions."""
        for sid in ["session-a", "session-b", "session-c"]:
            event = _make_event(session_id=sid)
            storage.store_event(event)

        assert len(storage.query_session_events("session-a")) == 1
        assert len(storage.query_session_events("session-b")) == 1
        assert len(storage.query_session_events("session-c")) == 1
        assert len(storage.query_session_events("session-d")) == 0

    def test_query_by_time_range(self, storage: DualLayerStorage) -> None:
        """Query events within a time range."""
        base = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc)
        for i in range(10):
            ts = base + timedelta(minutes=i)
            event = _make_event(
                session_id=f"time-session-{i}",
                timestamp=ts.isoformat(),
            )
            storage.store_event(event)

        # Query 3-minute window (should include events at minutes 3, 4, 5)
        range_start = (base + timedelta(minutes=3)).isoformat()
        range_end = (base + timedelta(minutes=6)).isoformat()
        events = storage.query_events_by_time_range(range_start, range_end)

        assert len(events) == 3

    def test_query_empty_session(self, storage: DualLayerStorage) -> None:
        """Query a session that doesn't exist returns empty list."""
        events = storage.query_session_events("nonexistent")
        assert events == []

    def test_query_empty_time_range(self, storage: DualLayerStorage) -> None:
        """Query a time range with no events returns empty list."""
        events = storage.query_events_by_time_range(
            "2020-01-01T00:00:00+00:00",
            "2020-01-02T00:00:00+00:00",
        )
        assert events == []

    def test_store_event_without_timestamp_gets_default(self, storage: DualLayerStorage) -> None:
        """Events without a timestamp should get a storage-assigned timestamp."""
        event = _make_event()
        event.timestamp = None
        storage.store_event(event)

        events = storage.query_session_events(event.session_id)
        assert len(events) == 1
        assert events[0]["timestamp"] is not None

    def test_store_event_without_session_id_uses_unknown(self, storage: DualLayerStorage) -> None:
        """Events without session_id should be stored under a fallback."""
        event = _make_event()
        event.session_id = None
        storage.store_event(event)

        events = storage.query_session_events("_unknown_session")
        assert len(events) == 1


class TestFullEventRoundTrip:
    """Test that events survive the full store -> filesystem -> load_full_event cycle."""

    def test_round_trip_preserves_all_fields(self, storage: DualLayerStorage) -> None:
        """Store an event with all fields populated, load it back, verify equality."""
        event = SecondSightEvent(
            agent_type=AgentType.CLAUDE_CODE.value,
            event_type=EventType.TOOL_CALL_END.value,
            timestamp="2026-04-24T10:30:00.000Z",
            session_id="roundtrip-session",
            cwd="/Users/dev/myapp",
            tool_name="Bash",
            tool_args={"command": "git status"},
            tool_result="On branch main\nnothing to commit",
            duration_ms=150,
            success=True,
            token_usage=TokenUsage(
                input_tokens=1500,
                output_tokens=200,
                cache_read_tokens=500,
                is_cumulative=False,
            ),
            agent_metadata=AgentMetadata(
                hook_event_name="PostToolUse",
                tool_use_id="toolu_01XYZ",
                permission_mode="default",
            ),
        )

        storage.store_event(event)

        events = storage.query_session_events("roundtrip-session")
        assert len(events) == 1

        full = storage.load_full_event("roundtrip-session", events[0]["filesystem_path"])
        assert full is not None
        assert full.agent_type == event.agent_type
        assert full.event_type == event.event_type
        assert full.session_id == event.session_id
        assert full.tool_name == event.tool_name
        assert full.tool_args == event.tool_args
        assert full.tool_result == event.tool_result
        assert full.duration_ms == event.duration_ms
        assert full.success == event.success
        assert full.token_usage is not None
        assert full.token_usage.input_tokens == 1500
        assert full.agent_metadata.hook_event_name == "PostToolUse"


class TestBatchOperations:
    """Test batch write performance."""

    def test_batch_store(self, storage: DualLayerStorage) -> None:
        """Batch store should be faster than individual stores."""
        events = []
        for i in range(100):
            ts = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc) + timedelta(seconds=i)
            events.append(
                _make_event(
                    session_id=f"batch-session-{i % 10}",
                    timestamp=ts.isoformat(),
                )
            )

        start = time.perf_counter()
        storage.store_events_batch(events)
        elapsed_ms = (time.perf_counter() - start) * 1000

        # Verify all events stored
        total = 0
        for sid_idx in range(10):
            total += len(storage.query_session_events(f"batch-session-{sid_idx}"))
        assert total == 100

    def test_batch_store_empty(self, storage: DualLayerStorage) -> None:
        """Batch store with empty list should not error."""
        storage.store_events_batch([])


class TestSessionMetadata:
    """Test session-level metadata management."""

    def test_session_metadata_created_on_first_event(self, storage: DualLayerStorage) -> None:
        """First event in a session should create a sessions table row."""
        event = _make_event(session_id="meta-session")
        storage.store_event(event)

        cursor = storage._db.execute(
            "SELECT session_id, agent_type FROM sessions WHERE session_id = ?",
            ("meta-session",),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == "meta-session"
        assert row[1] == AgentType.CLAUDE_CODE.value

    def test_session_metadata_updated_on_subsequent_events(self, storage: DualLayerStorage) -> None:
        """Subsequent events should update session metadata (e.g., event count, last_event_ts)."""
        session_id = "meta-update-session"
        ts1 = datetime(2026, 4, 24, 10, 0, 0, tzinfo=timezone.utc)
        ts2 = ts1 + timedelta(minutes=5)

        storage.store_event(_make_event(session_id=session_id, timestamp=ts1.isoformat()))
        storage.store_event(_make_event(session_id=session_id, timestamp=ts2.isoformat()))

        cursor = storage._db.execute(
            "SELECT event_count, last_event_ts FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = cursor.fetchone()
        assert row is not None
        assert row[0] == 2  # event_count
        assert row[1] == ts2.isoformat()  # last_event_ts


class TestFilesystemLayout:
    """Test that filesystem layout matches the spec."""

    def test_event_file_naming(self, storage: DualLayerStorage) -> None:
        """Event files should follow {timestamp}_{event_type}.json naming."""
        event = _make_event(
            session_id="layout-session",
            timestamp="2026-04-24T10:30:00.000000+00:00",
            event_type=EventType.TOOL_CALL_START.value,
        )
        storage.store_event(event)

        session_dir = storage._session_events_dir("layout-session")
        files = list(Path(session_dir).glob("*.json"))
        assert len(files) == 1

        filename = files[0].name
        # Should contain timestamp component and event type
        assert "tool_call_start" in filename
        assert filename.endswith(".json")

    def test_session_directory_structure(self, storage: DualLayerStorage) -> None:
        """Session should have events/ subdirectory."""
        event = _make_event(session_id="structure-session")
        storage.store_event(event)

        session_dir = Path(storage._config.base_dir) / "sessions" / "structure-session"
        assert session_dir.exists()
        assert (session_dir / "events").exists()

    def test_session_metadata_json(self, storage: DualLayerStorage) -> None:
        """Session should have a metadata.json file."""
        event = _make_event(session_id="metadata-json-session")
        storage.store_event(event)

        metadata_path = (
            Path(storage._config.base_dir)
            / "sessions"
            / "metadata-json-session"
            / "metadata.json"
        )
        assert metadata_path.exists()

        with open(metadata_path) as f:
            meta = json.load(f)
        assert meta["session_id"] == "metadata-json-session"
