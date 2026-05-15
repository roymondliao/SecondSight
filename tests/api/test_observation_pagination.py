"""Pagination cursor + ETag wiring tests for the Observation API (task-A7).

Plan §3.2 / D6: dashboard polls at 5s with ``ETag`` + ``cursor`` so:
    - unchanged data → 304 short-circuit (already pinned in task-A5);
    - new data → fresh fetch follows ``next_cursor`` until it is null.

A7 has no new death cases (per index.yaml) — these tests pin the
end-to-end pagination flow A5's baseline left untested:

    1. Listing under the page limit returns ``next_cursor=None``.
    2. Listing over the page limit returns a non-null cursor whose
       follow-up call advances by exactly one page.
    3. Following the cursor across the entire dataset reaches a final
       page with ``next_cursor=None`` and no session is duplicated or
       skipped.
    4. Cursor parsing rejects garbage values and "both cursor and
       offset" with 422 — the dashboard contract is unambiguous.
    5. ETag stays stable across a cursor advance on identical data.
    6. ETag invalidates when a new event arrives mid-pagination, so
       the next request body is fresh (not a 304 from the cached
       cursor's prior ETag).
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
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / ".secondsight"
    h.mkdir()
    return h


def _client(home: Path) -> TestClient:
    app = create_app(secondsight_home=home)
    return TestClient(app, raise_server_exceptions=False)


def _seed_n_sessions(home: Path, *, project_id: str, count: int) -> list[str]:
    """Materialize ``count`` distinct sessions for one project.

    Returns the sorted session_id list so tests can assert on the
    exact order the listing endpoint emits.
    """
    from secondsight.api.registry import ProjectRegistry

    registry = ProjectRegistry(secondsight_home=home)
    resources = asyncio.run(registry.get(project_id))
    repo = resources.events_repository
    store = resources.raw_trace_store

    base_ts = datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC)
    ids = [f"s{i:03d}" for i in range(count)]

    async def _write_all() -> None:
        for i, sid in enumerate(ids):
            ev = make_event(
                event_id=f"{sid}-evt",
                session_id=sid,
                project_id=project_id,
                sequence_number=0,
                timestamp=base_ts + timedelta(seconds=i),
                event_type=EventType.USER_PROMPT,
            )
            repo.insert(ev)
            await store.write(ev)

    asyncio.run(_write_all())
    asyncio.run(registry.aclose())
    return sorted(ids)


def _append_event(home: Path, *, project_id: str, session_id: str, ts: datetime) -> None:
    """Add one new event to a session — bumps max(timestamp), invalidates ETag."""
    from secondsight.api.registry import ProjectRegistry

    registry = ProjectRegistry(secondsight_home=home)
    resources = asyncio.run(registry.get(project_id))

    async def _write() -> None:
        ev = make_event(
            event_id=f"{session_id}-bump",
            session_id=session_id,
            project_id=project_id,
            sequence_number=999,
            timestamp=ts,
            event_type=EventType.USER_PROMPT,
        )
        resources.events_repository.insert(ev)
        await resources.raw_trace_store.write(ev)

    asyncio.run(_write())
    asyncio.run(registry.aclose())


# ---------------------------------------------------------------------------
# Cursor flow — multi-page traversal.
# ---------------------------------------------------------------------------


class TestCursorFlow:
    def test_under_limit_returns_null_cursor(self, home: Path) -> None:
        _seed_n_sessions(home, project_id="p1", count=3)
        with _client(home) as client:
            r = client.get(
                "/api/sessions",
                params={"project_id": "p1", "limit": 50},
            )
        assert r.status_code == 200
        body = r.json()
        assert len(body["sessions"]) == 3
        assert body["next_cursor"] is None

    def test_over_limit_emits_cursor(self, home: Path) -> None:
        _seed_n_sessions(home, project_id="p1", count=5)
        with _client(home) as client:
            r = client.get(
                "/api/sessions",
                params={"project_id": "p1", "limit": 2},
            )
        assert r.status_code == 200
        body = r.json()
        assert len(body["sessions"]) == 2
        assert body["next_cursor"] == "2"

    def test_cursor_round_trip_walks_full_dataset(self, home: Path) -> None:
        all_ids = _seed_n_sessions(home, project_id="p1", count=7)
        seen: list[str] = []
        cursor: str | None = None

        with _client(home) as client:
            for _ in range(10):  # safety bound; real loop should terminate <=4 hops
                params: dict[str, str | int] = {"project_id": "p1", "limit": 2}
                if cursor is not None:
                    params["cursor"] = cursor
                r = client.get("/api/sessions", params=params)
                assert r.status_code == 200, r.text
                body = r.json()
                seen.extend(s["session_id"] for s in body["sessions"])
                cursor = body["next_cursor"]
                if cursor is None:
                    break
            else:
                pytest.fail("cursor never reached None — pagination loop didn't terminate")

        assert seen == all_ids
        assert len(seen) == len(set(seen))  # no duplicates

    def test_final_page_next_cursor_is_none(self, home: Path) -> None:
        _seed_n_sessions(home, project_id="p1", count=4)
        with _client(home) as client:
            # offset=2 + limit=2 → exactly the last 2 sessions, no more after.
            r = client.get(
                "/api/sessions",
                params={"project_id": "p1", "limit": 2, "cursor": "2"},
            )
        assert r.status_code == 200
        body = r.json()
        assert len(body["sessions"]) == 2
        assert body["next_cursor"] is None


# ---------------------------------------------------------------------------
# Cursor parsing contract — unambiguous or 422.
# ---------------------------------------------------------------------------


class TestCursorParsing:
    def test_garbage_cursor_is_422(self, home: Path) -> None:
        _seed_n_sessions(home, project_id="p1", count=2)
        with _client(home) as client:
            r = client.get(
                "/api/sessions",
                params={"project_id": "p1", "cursor": "not-a-number"},
            )
        assert r.status_code == 422, r.text

    def test_negative_cursor_is_422(self, home: Path) -> None:
        _seed_n_sessions(home, project_id="p1", count=2)
        with _client(home) as client:
            r = client.get(
                "/api/sessions",
                params={"project_id": "p1", "cursor": "-1"},
            )
        assert r.status_code == 422

    def test_cursor_and_nonzero_offset_together_is_422(self, home: Path) -> None:
        _seed_n_sessions(home, project_id="p1", count=4)
        with _client(home) as client:
            r = client.get(
                "/api/sessions",
                params={
                    "project_id": "p1",
                    "cursor": "2",
                    "offset": 1,
                },
            )
        assert r.status_code == 422

    def test_cursor_with_default_offset_zero_is_ok(self, home: Path) -> None:
        # Cursor + offset=0 (the default) is benign — server must not
        # reject pages just because a client serialises both.
        _seed_n_sessions(home, project_id="p1", count=4)
        with _client(home) as client:
            r = client.get(
                "/api/sessions",
                params={
                    "project_id": "p1",
                    "cursor": "2",
                    "offset": 0,
                    "limit": 2,
                },
            )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# ETag interaction with cursor advance.
# ---------------------------------------------------------------------------


class TestEtagAcrossCursor:
    def test_etag_stable_across_cursor_advance_on_unchanged_data(self, home: Path) -> None:
        _seed_n_sessions(home, project_id="p1", count=4)
        with _client(home) as client:
            page1 = client.get(
                "/api/sessions",
                params={"project_id": "p1", "limit": 2},
            )
            page2 = client.get(
                "/api/sessions",
                params={"project_id": "p1", "limit": 2, "cursor": "2"},
            )
        assert page1.status_code == 200
        assert page2.status_code == 200
        assert page1.headers["etag"] == page2.headers["etag"], (
            "ETag must be derived from project state, not page coordinates — "
            "otherwise dashboard polling cannot short-circuit on 304 across pages."
        )

    def test_etag_invalidates_when_new_event_arrives_mid_pagination(self, home: Path) -> None:
        _seed_n_sessions(home, project_id="p1", count=4)
        with _client(home) as client:
            page1 = client.get(
                "/api/sessions",
                params={"project_id": "p1", "limit": 2},
            )
            etag1 = page1.headers["etag"]

        # New event lands between pages — ETag must change so a poller
        # using If-None-Match=etag1 does NOT receive 304.
        _append_event(
            home,
            project_id="p1",
            session_id="s001",
            ts=datetime(2026, 5, 6, 12, 0, 0, tzinfo=UTC) + timedelta(hours=1),
        )

        with _client(home) as client:
            page2 = client.get(
                "/api/sessions",
                params={"project_id": "p1", "limit": 2, "cursor": "2"},
                headers={"If-None-Match": etag1},
            )
        assert page2.status_code == 200, page2.text
        assert page2.headers["etag"] != etag1
