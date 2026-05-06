"""Death tests + happy-path tests for the Observation API router (task-A5).

Death cases pinned in plan §5:
    DC-4: Observation API rejects requests without ``project_id`` with a 422;
          NEVER returns data scoped to "first project found" (no cross-project
          leak).
    DC-7: ETag header on listing endpoints derived from
          ``max(events.timestamp)`` so dashboard polling can short-circuit
          on 304 Not Modified.

DC-4 is the highest-value invariant: the registry materializes a per-project
DB engine; if the router silently fell back to "the project we happen to
have cached" when project_id was missing, two clients on the same instance
could be served each other's data. FastAPI's ``Query(...)`` no-default
mechanism enforces 422 — we pin it here so a future refactor (e.g. someone
adding a default value for convenience) cannot regress it silently.

DC-7 baseline: ETag presence on listing endpoints, 304 round-trip when the
client sends back the same If-None-Match. Full cursor/pagination semantics
land in task-A7; here we only require the listing response to carry an ETag
that the dashboard can echo for short-circuiting.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from secondsight.api.server import create_app
from secondsight.event import EventType
from tests.conftest import make_event

UTC = timezone.utc


# ---------------------------------------------------------------------------
# App factory + seed helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / ".secondsight"
    h.mkdir()
    return h


def _client(home: Path) -> TestClient:
    app = create_app(secondsight_home=home)
    return TestClient(app, raise_server_exceptions=False)


def _seed_session(
    home: Path,
    *,
    project_id: str,
    session_id: str,
    last_ts: datetime,
    event_count: int = 1,
    segment_index: int = 0,
    base_seq: int = 0,
) -> None:
    """Materialize a project via ProjectRegistry and seed events into it.

    ``base_seq`` lets callers append more events to an existing session
    without colliding with the UNIQUE(session_id, sequence_number)
    constraint.
    """
    from secondsight.api.registry import ProjectRegistry

    registry = ProjectRegistry(secondsight_home=home)
    resources = asyncio.run(registry.get(project_id))
    repo = resources.events_repository
    store = resources.raw_trace_store

    async def _write_all():
        for i in range(event_count):
            ts = last_ts - timedelta(seconds=event_count - 1 - i)
            ev = make_event(
                event_id=f"{session_id}-seg{segment_index}-{base_seq + i:04d}",
                session_id=session_id,
                project_id=project_id,
                sequence_number=base_seq + i,
                timestamp=ts,
                event_type=EventType.USER_PROMPT,
                segment_index=segment_index,
            )
            repo.insert(ev)
            await store.write(ev)

    asyncio.run(_write_all())
    asyncio.run(registry.aclose())


# ---------------------------------------------------------------------------
# DC-4 — project_id is mandatory on every endpoint.
# ---------------------------------------------------------------------------


class TestDC4ProjectIdRequired:
    def test_get_sessions_without_project_id_is_422(self, home: Path) -> None:
        with _client(home) as client:
            r = client.get("/api/sessions")
        assert r.status_code == 422, r.text

    def test_get_session_detail_without_project_id_is_422(self, home: Path) -> None:
        with _client(home) as client:
            r = client.get("/api/sessions/anything")
        assert r.status_code == 422, r.text

    def test_get_segments_without_project_id_is_422(self, home: Path) -> None:
        with _client(home) as client:
            r = client.get("/api/sessions/anything/segments")
        assert r.status_code == 422, r.text

    def test_get_segment_detail_without_project_id_is_422(self, home: Path) -> None:
        with _client(home) as client:
            r = client.get("/api/sessions/anything/segments/0")
        assert r.status_code == 422, r.text

    def test_no_cross_project_leak(self, home: Path) -> None:
        """Belt-and-braces: if project_id is provided but points at an
        unmaterialized project, the response is empty — NOT data from
        whichever project was last accessed.
        """
        old_ts = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC) - timedelta(days=1)
        _seed_session(home, project_id="alpha", session_id="s1", last_ts=old_ts)

        with _client(home) as client:
            r = client.get("/api/sessions", params={"project_id": "beta"})
        assert r.status_code == 200, r.text
        assert r.json()["sessions"] == []


# ---------------------------------------------------------------------------
# Happy paths — listings and detail.
# ---------------------------------------------------------------------------


class TestSessionListing:
    def test_empty_project(self, home: Path) -> None:
        with _client(home) as client:
            r = client.get("/api/sessions", params={"project_id": "p1"})
        assert r.status_code == 200
        body = r.json()
        assert body["sessions"] == []
        assert body["next_cursor"] is None

    def test_two_sessions_listed_with_aggregates(self, home: Path) -> None:
        old_ts = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC) - timedelta(days=1)
        _seed_session(
            home,
            project_id="p1",
            session_id="s1",
            last_ts=old_ts,
            event_count=2,
            segment_index=0,
        )
        _seed_session(
            home,
            project_id="p1",
            session_id="s2",
            last_ts=old_ts + timedelta(hours=1),
            event_count=3,
            segment_index=1,
        )

        with _client(home) as client:
            r = client.get("/api/sessions", params={"project_id": "p1"})

        assert r.status_code == 200, r.text
        body = r.json()
        sessions_by_id = {s["session_id"]: s for s in body["sessions"]}
        assert set(sessions_by_id) == {"s1", "s2"}
        assert sessions_by_id["s1"]["event_count"] == 2
        assert sessions_by_id["s2"]["event_count"] == 3
        # segment_count is derived from distinct segment_index values.
        assert sessions_by_id["s1"]["segment_count"] == 1
        assert sessions_by_id["s2"]["segment_count"] == 1


class TestSessionDetail:
    def test_unknown_session_returns_404(self, home: Path) -> None:
        with _client(home) as client:
            r = client.get(
                "/api/sessions/does-not-exist",
                params={"project_id": "p1"},
            )
        assert r.status_code == 404, r.text

    def test_known_session_returns_header(self, home: Path) -> None:
        last = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)
        _seed_session(home, project_id="p1", session_id="s1", last_ts=last, event_count=4)

        with _client(home) as client:
            r = client.get("/api/sessions/s1", params={"project_id": "p1"})

        assert r.status_code == 200
        body = r.json()
        assert body["session_id"] == "s1"
        assert body["project_id"] == "p1"
        assert body["event_count"] == 4
        assert body["segment_count"] == 1


class TestSegmentListing:
    def test_unknown_session_returns_404(self, home: Path) -> None:
        with _client(home) as client:
            r = client.get(
                "/api/sessions/missing/segments",
                params={"project_id": "p1"},
            )
        assert r.status_code == 404, r.text

    def test_two_segments_listed(self, home: Path) -> None:
        ts = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)
        _seed_session(
            home,
            project_id="p1",
            session_id="s1",
            last_ts=ts,
            event_count=2,
            segment_index=0,
            base_seq=0,
        )
        _seed_session(
            home,
            project_id="p1",
            session_id="s1",
            last_ts=ts + timedelta(seconds=10),
            event_count=3,
            segment_index=1,
            base_seq=2,
        )

        with _client(home) as client:
            r = client.get(
                "/api/sessions/s1/segments",
                params={"project_id": "p1"},
            )

        assert r.status_code == 200
        body = r.json()
        segs_by_index = {s["segment_index"]: s for s in body["segments"]}
        assert set(segs_by_index) == {0, 1}
        assert segs_by_index[0]["event_count"] == 2
        assert segs_by_index[1]["event_count"] == 3


class TestSegmentDetail:
    def test_unknown_segment_returns_404(self, home: Path) -> None:
        ts = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)
        _seed_session(home, project_id="p1", session_id="s1", last_ts=ts, event_count=1)
        with _client(home) as client:
            r = client.get(
                "/api/sessions/s1/segments/99",
                params={"project_id": "p1"},
            )
        assert r.status_code == 404, r.text

    def test_returns_full_event_timeline(self, home: Path) -> None:
        ts = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)
        _seed_session(
            home,
            project_id="p1",
            session_id="s1",
            last_ts=ts,
            event_count=3,
            segment_index=0,
        )

        with _client(home) as client:
            r = client.get(
                "/api/sessions/s1/segments/0",
                params={"project_id": "p1"},
            )

        assert r.status_code == 200
        body = r.json()
        assert body["session_id"] == "s1"
        assert body["segment_index"] == 0
        assert len(body["events"]) == 3
        # Ordered by sequence_number ascending.
        assert [e["sequence_number"] for e in body["events"]] == [0, 1, 2]


# ---------------------------------------------------------------------------
# DC-7 — ETag round-trip on listing endpoints.
# ---------------------------------------------------------------------------


class TestDC7Etag:
    def test_session_list_returns_etag(self, home: Path) -> None:
        ts = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)
        _seed_session(home, project_id="p1", session_id="s1", last_ts=ts, event_count=1)
        with _client(home) as client:
            r = client.get("/api/sessions", params={"project_id": "p1"})
        assert r.status_code == 200
        assert r.headers.get("etag"), r.headers

    def test_session_list_if_none_match_returns_304(self, home: Path) -> None:
        ts = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)
        _seed_session(home, project_id="p1", session_id="s1", last_ts=ts, event_count=1)
        with _client(home) as client:
            first = client.get("/api/sessions", params={"project_id": "p1"})
            etag = first.headers["etag"]
            second = client.get(
                "/api/sessions",
                params={"project_id": "p1"},
                headers={"If-None-Match": etag},
            )
        assert second.status_code == 304, second.text
        # 304 must not carry a body.
        assert second.content == b""

    def test_segment_list_returns_etag_and_304_round_trip(self, home: Path) -> None:
        ts = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)
        _seed_session(home, project_id="p1", session_id="s1", last_ts=ts, event_count=1)
        with _client(home) as client:
            first = client.get(
                "/api/sessions/s1/segments",
                params={"project_id": "p1"},
            )
            etag = first.headers["etag"]
            second = client.get(
                "/api/sessions/s1/segments",
                params={"project_id": "p1"},
                headers={"If-None-Match": etag},
            )
        assert first.status_code == 200
        assert etag, first.headers
        assert second.status_code == 304

    def test_etag_changes_when_new_event_arrives(self, home: Path) -> None:
        ts = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)
        _seed_session(home, project_id="p1", session_id="s1", last_ts=ts, event_count=1)
        with _client(home) as client:
            first_etag = client.get("/api/sessions", params={"project_id": "p1"}).headers["etag"]

        # Append a newer event — boundary advances, ETag must change.
        _seed_session(
            home,
            project_id="p1",
            session_id="s2",
            last_ts=ts + timedelta(hours=1),
            event_count=1,
        )
        with _client(home) as client:
            second_etag = client.get("/api/sessions", params={"project_id": "p1"}).headers["etag"]

        assert first_etag != second_etag
