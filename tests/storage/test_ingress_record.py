"""Validation tests for IngressRecord."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from secondsight.storage.ingress_record import IngressRecord


def test_ingress_record_validates_minimal_shape() -> None:
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
    assert record.sequence_number == 0


def test_ingress_record_rejects_negative_sequence_number() -> None:
    with pytest.raises(Exception):
        IngressRecord(
            agent="claude_code",
            event_type="session_start",
            event_id="evt-1",
            timestamp=datetime(2026, 5, 12, 3, 0, tzinfo=timezone.utc),
            sequence_number=-1,
            session_id="sess-1",
            project_id="proj-1",
            payload={},
        )
