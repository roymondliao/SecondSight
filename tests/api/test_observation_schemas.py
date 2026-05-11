"""Structural tests for Observation API schemas (task-A3).

Task-A3 has no death cases (index.yaml). These tests pin down the contracts
the schemas exist to enforce so future edits cannot silently relax them:

    - frozen → response objects are immutable post-construction.
    - extra="forbid" → unknown fields are rejected, not absorbed.
    - required vs optional field set is explicit.
    - JSON round-trip is lossless (datetimes survive).
    - SegmentDetail.events is a list of full Event models, not dicts.

If a future schema change loosens any of these, the corresponding test fails;
that's the point. Schemas are the dashboard's contract — a silent shape drift
is exactly the failure these tests catch.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from secondsight.api.observation import (
    ListSegmentsResponse,
    ListSessionsResponse,
    SegmentDetail,
    SegmentSummary,
    SessionDetail,
    SessionSummary,
)
from secondsight.event import Event, EventType


def _ts(seconds: int = 0) -> datetime:
    return datetime(2026, 1, 1, 0, 0, seconds, tzinfo=UTC)


def _event(seq: int = 0, segment_index: int = 0) -> Event:
    return Event(
        id=f"e{seq}",
        session_id="s1",
        project_id="p1",
        event_type=EventType.USER_PROMPT,
        timestamp=_ts(seq),
        sequence_number=seq,
        segment_index=segment_index,
    )


# ---------------------------------------------------------------------------
# SessionSummary / SessionDetail
# ---------------------------------------------------------------------------


class TestSessionSummary:
    def test_minimal_construction(self) -> None:
        s = SessionSummary(
            session_id="s1",
            project_id="p1",
            first_event_at=_ts(0),
            last_event_at=_ts(10),
            event_count=5,
            segment_count=2,
        )
        assert s.event_count == 5

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            SessionSummary(
                session_id="s1",
                project_id="p1",
                first_event_at=_ts(0),
                last_event_at=_ts(10),
                event_count=5,
                segment_count=2,
                bogus_field="x",  # type: ignore[call-arg]
            )

    def test_is_frozen(self) -> None:
        s = SessionSummary(
            session_id="s1",
            project_id="p1",
            first_event_at=_ts(0),
            last_event_at=_ts(10),
            event_count=5,
            segment_count=2,
        )
        with pytest.raises(ValidationError):
            s.event_count = 99  # type: ignore[misc]

    def test_event_count_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            SessionSummary(
                session_id="s1",
                project_id="p1",
                first_event_at=_ts(0),
                last_event_at=_ts(10),
                event_count=-1,
                segment_count=0,
            )

    def test_session_id_required(self) -> None:
        with pytest.raises(ValidationError):
            SessionSummary(  # type: ignore[call-arg]
                project_id="p1",
                first_event_at=_ts(0),
                last_event_at=_ts(0),
                event_count=0,
                segment_count=0,
            )

    def test_project_id_required(self) -> None:
        with pytest.raises(ValidationError):
            SessionSummary(  # type: ignore[call-arg]
                session_id="s1",
                first_event_at=_ts(0),
                last_event_at=_ts(0),
                event_count=0,
                segment_count=0,
            )

    def test_json_round_trip_preserves_timestamp(self) -> None:
        s = SessionSummary(
            session_id="s1",
            project_id="p1",
            first_event_at=_ts(0),
            last_event_at=_ts(10),
            event_count=5,
            segment_count=2,
        )
        recovered = SessionSummary.model_validate_json(s.model_dump_json())
        assert recovered == s


class TestSessionDetail:
    def test_minimal_construction(self) -> None:
        d = SessionDetail(
            session_id="s1",
            project_id="p1",
            first_event_at=_ts(0),
            last_event_at=_ts(10),
            event_count=5,
            segment_count=2,
        )
        assert d.segment_count == 2

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            SessionDetail(
                session_id="s1",
                project_id="p1",
                first_event_at=_ts(0),
                last_event_at=_ts(0),
                event_count=0,
                segment_count=0,
                extra="x",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# SegmentSummary / SegmentDetail
# ---------------------------------------------------------------------------


class TestSegmentSummary:
    def test_minimal_construction_with_optional_metrics(self) -> None:
        seg = SegmentSummary(
            session_id="s1",
            segment_index=3,
            first_event_at=_ts(0),
            last_event_at=_ts(5),
            event_count=4,
        )
        assert seg.duration_ms is None
        assert seg.token_count is None

    def test_optional_metrics_accept_zero(self) -> None:
        seg = SegmentSummary(
            session_id="s1",
            segment_index=0,
            first_event_at=_ts(0),
            last_event_at=_ts(0),
            event_count=0,
            duration_ms=0,
            token_count=0,
        )
        assert seg.duration_ms == 0

    def test_segment_index_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            SegmentSummary(
                session_id="s1",
                segment_index=-1,
                first_event_at=_ts(0),
                last_event_at=_ts(0),
                event_count=0,
            )

    def test_duration_ms_non_negative(self) -> None:
        with pytest.raises(ValidationError):
            SegmentSummary(
                session_id="s1",
                segment_index=0,
                first_event_at=_ts(0),
                last_event_at=_ts(0),
                event_count=0,
                duration_ms=-1,
            )


class TestSegmentDetail:
    def test_carries_full_event_models(self) -> None:
        d = SegmentDetail(
            session_id="s1",
            segment_index=0,
            events=[_event(seq=0), _event(seq=1)],
        )
        assert len(d.events) == 2
        assert all(isinstance(e, Event) for e in d.events)

    def test_empty_events_list_allowed(self) -> None:
        d = SegmentDetail(session_id="s1", segment_index=0, events=[])
        assert d.events == []

    def test_rejects_dict_event(self) -> None:
        # Pydantic will coerce dict → Event; verify the coercion path still
        # validates Event's own constraints (extra="forbid" on Event).
        with pytest.raises(ValidationError):
            SegmentDetail(
                session_id="s1",
                segment_index=0,
                events=[{"not_an_event": True}],  # type: ignore[list-item]
            )


# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------


class TestListSessionsResponse:
    def test_empty_listing(self) -> None:
        resp = ListSessionsResponse(sessions=[])
        assert resp.sessions == []
        assert resp.next_cursor is None

    def test_with_cursor(self) -> None:
        s = SessionSummary(
            session_id="s1",
            project_id="p1",
            first_event_at=_ts(0),
            last_event_at=_ts(0),
            event_count=0,
            segment_count=0,
        )
        resp = ListSessionsResponse(sessions=[s], next_cursor="opaque-cursor")
        assert resp.next_cursor == "opaque-cursor"

    def test_rejects_unknown_field(self) -> None:
        with pytest.raises(ValidationError):
            ListSessionsResponse(sessions=[], total=0)  # type: ignore[call-arg]


class TestListSegmentsResponse:
    def test_empty_listing(self) -> None:
        assert ListSegmentsResponse(segments=[]).segments == []

    def test_no_cursor_field(self) -> None:
        with pytest.raises(ValidationError):
            ListSegmentsResponse(segments=[], next_cursor="x")  # type: ignore[call-arg]
