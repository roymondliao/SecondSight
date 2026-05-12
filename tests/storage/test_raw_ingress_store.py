"""Tests for RawIngressStore."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from secondsight.storage.ingress_record import IngressRecord
from secondsight.storage.raw_ingress_store import RawIngressStore
from secondsight.storage.raw_trace_store import UnsafePathError


@pytest.mark.asyncio
async def test_raw_ingress_store_round_trip(tmp_path: Path) -> None:
    store = RawIngressStore(tmp_path)
    record = IngressRecord(
        agent="claude_code",
        event_type="session_start",
        event_id="evt-1",
        timestamp=datetime(2026, 5, 12, 3, 0, tzinfo=timezone.utc),
        sequence_number=0,
        session_id="sess-1",
        project_id="proj-1",
        payload={"hook_event_name": "SessionStart"},
    )
    path = await store.write(record)
    loaded = await store.read(path)
    assert loaded == record


def test_raw_ingress_store_rejects_unsafe_session_id(tmp_path: Path) -> None:
    store = RawIngressStore(tmp_path)
    record = IngressRecord(
        agent="claude_code",
        event_type="session_start",
        event_id="evt-1",
        timestamp=datetime(2026, 5, 12, 3, 0, tzinfo=timezone.utc),
        sequence_number=0,
        session_id="../oops",
        project_id="proj-1",
        payload={},
    )
    with pytest.raises(UnsafePathError):
        store.ingress_path(record)
