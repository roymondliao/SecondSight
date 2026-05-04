"""Tests for RawTraceStore (P1-1).

Death tests come first (samsara order). They probe the silent-failure
paths called out in task-1.md / overview.md. Unit tests come after.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from secondsight.event import EventType
from secondsight.storage.raw_trace_store import (
    RawTraceCorruptionError,
    RawTraceStore,
    UnsafePathError,
)
from tests.conftest import make_event

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Death tests — silent failure paths
# ---------------------------------------------------------------------------


async def test_death_submillisecond_collision_does_not_swallow_events(
    project_root: Path,
) -> None:
    """100 events with identical timestamp+event_type must produce 100 files.

    A naive `{timestamp}_{event_type}.json` scheme would overwrite. The store
    must use sequence_number (or equivalent monotonic key) for uniqueness.
    """
    store = RawTraceStore(project_root)
    same_ts = datetime(2026, 5, 4, 12, 0, 0, 123000, tzinfo=timezone.utc)

    events = [
        make_event(
            event_id=f"evt-{i:04d}",
            sequence_number=i,
            timestamp=same_ts,
            event_type=EventType.TOOL_USE_START,
        )
        for i in range(100)
    ]

    paths = await asyncio.gather(*(store.write(e) for e in events))

    assert len(set(paths)) == 100, "duplicate paths — events overwrote each other"
    for p in paths:
        assert p.exists()
        assert p.stat().st_size > 0


async def test_death_partial_write_leaves_no_corrupt_file(project_root: Path) -> None:
    """If the OS write fails mid-flight, no half-written file may remain.

    Atomic write must use tmp + os.replace — a partial tmp is acceptable
    (and gets cleaned up next), but the destination path must NEVER hold
    a corrupt JSON.
    """
    store = RawTraceStore(project_root)
    event = make_event()
    target = store.event_path(event)

    def fail_after_partial(*args, **kwargs):
        # Simulate disk-full error after a temp file has been created
        # but before the atomic rename happens.
        raise OSError(28, "No space left on device")

    with patch("os.replace", side_effect=fail_after_partial):
        with pytest.raises(OSError):
            await store.write(event)

    assert not target.exists(), (
        "destination path holds a file even though write failed — "
        "atomic-write contract violated"
    )


async def test_death_truncated_file_raises_typed_error(project_root: Path) -> None:
    """A reader that silently returns garbage on a truncated file is
    a worse failure than raising. Truncate to 5 bytes, expect typed error.
    """
    store = RawTraceStore(project_root)
    event = make_event()
    path = await store.write(event)

    # Manually truncate (simulating disk corruption / kill mid-write
    # done by something OTHER than this store).
    with open(path, "r+b") as fh:
        fh.truncate(5)

    with pytest.raises(RawTraceCorruptionError):
        await store.read(path)


async def test_death_path_traversal_in_session_id_rejected(project_root: Path) -> None:
    """An event whose session_id tries to escape the project root must be
    rejected before any write — not after.
    """
    store = RawTraceStore(project_root)

    bad = make_event(session_id="../../etc/passwd")

    with pytest.raises(UnsafePathError):
        store.event_path(bad)

    with pytest.raises(UnsafePathError):
        await store.write(bad)


async def test_death_session_id_with_separator_rejected(project_root: Path) -> None:
    """Even within the project root, a session_id containing `/` could
    create unintended subdirectories. Reject."""
    store = RawTraceStore(project_root)
    bad = make_event(session_id="sess/../other")

    with pytest.raises(UnsafePathError):
        store.event_path(bad)


# ---------------------------------------------------------------------------
# Unit tests — happy path + invariants
# ---------------------------------------------------------------------------


async def test_write_roundtrip_for_every_event_type(project_root: Path) -> None:
    store = RawTraceStore(project_root)
    for i, et in enumerate(EventType):
        e = make_event(
            event_id=f"evt-{i}",
            sequence_number=i,
            event_type=et,
            data={"k": f"v-{et.value}"},
        )
        path = await store.write(e)
        loaded = await store.read(path)
        assert loaded == e, f"round-trip failed for {et}"


async def test_event_path_is_deterministic(project_root: Path) -> None:
    store = RawTraceStore(project_root)
    e = make_event()
    p1 = store.event_path(e)
    p2 = store.event_path(e)
    assert p1 == p2


async def test_event_path_includes_session_and_event_type(project_root: Path) -> None:
    store = RawTraceStore(project_root)
    e = make_event(
        session_id="sess-xyz",
        event_type=EventType.TOOL_USE_END,
        sequence_number=42,
    )
    p = store.event_path(e)
    assert "sess-xyz" in str(p)
    assert "tool_use_end" in p.name
    assert p.suffix == ".json"


async def test_concurrent_writes_distinct_files(project_root: Path) -> None:
    store = RawTraceStore(project_root)
    events = [make_event(event_id=f"e-{i}", sequence_number=i) for i in range(20)]
    paths = await asyncio.gather(*(store.write(e) for e in events))
    assert len(set(paths)) == 20


async def test_iter_session_empty_session_returns_empty(project_root: Path) -> None:
    store = RawTraceStore(project_root)
    found = [p async for p in store.iter_session("nonexistent-session")]
    assert found == []


async def test_iter_session_returns_lexicographic_order(project_root: Path) -> None:
    store = RawTraceStore(project_root)
    base_ts = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
    # Insert out of order
    for seq in [5, 2, 8, 1, 3]:
        await store.write(
            make_event(
                event_id=f"e-{seq}",
                sequence_number=seq,
                timestamp=base_ts.replace(second=seq),
                event_type=EventType.USER_PROMPT,
            )
        )
    paths = [p async for p in store.iter_session("sess-001")]
    names = [p.name for p in paths]
    assert names == sorted(names), "iter_session not lex-ordered"


async def test_written_file_is_valid_json_with_schema_version(
    project_root: Path,
) -> None:
    store = RawTraceStore(project_root)
    e = make_event(data={"unicode": "中文 🚀", "nested": {"a": [1, 2, 3]}})
    path = await store.write(e)
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["id"] == e.id
    assert raw["schema_version"] == "1.0.0"
    assert raw["data"]["unicode"] == "中文 🚀"


async def test_write_creates_session_directory_if_missing(project_root: Path) -> None:
    store = RawTraceStore(project_root)
    sess_dir = project_root / "sessions" / "fresh-sess" / "events"
    assert not sess_dir.exists()

    await store.write(make_event(session_id="fresh-sess"))

    assert sess_dir.exists()


async def test_write_does_not_emit_partial_file_on_keyboard_interrupt(
    project_root: Path,
) -> None:
    """Even a KeyboardInterrupt mid-write must not leave a corrupt
    destination. (Closely related to the partial-write death test, but
    asserts the contract holds for BaseException, not just OSError.)
    """
    store = RawTraceStore(project_root)
    event = make_event(event_id="ki-1")
    target = store.event_path(event)

    with patch("os.replace", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            await store.write(event)

    assert not target.exists()
