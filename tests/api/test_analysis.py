"""Death + happy-path tests for the Analysis API (GUR-104 task-3).

Death cases:
- DC-1: cross-project leak (session_id in project A, request project_id=B → 404)
- DC-3: ETag scope must include all 4 analysis tables; insert anywhere in
  scope must invalidate
- DC-6: session_reports row missing → 404 (NOT 200 with empty body)
- DC-7: trends LIMIT applies to session set (delegated to repo task-1)

Schema invariant: BehaviorFlagOut MUST include `confidence` (memory
contract). DT-3.7 inspects the Pydantic JSON schema.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

import pytest
from fastapi.testclient import TestClient

from secondsight.analysis.schemas import (
    BehaviorFlag,
    BehaviorFlagType,
    Directive,
    DirectiveStatus,
    DirectiveType,
    SessionReport,
)
from secondsight.api.analysis import BehaviorFlagOut
from secondsight.api.registry import ProjectRegistry
from secondsight.api.server import create_app
from secondsight.storage.behavior_flags_repository import (
    BehaviorFlagsRepository,
)
from secondsight.storage.directives_repository import DirectivesRepository
from secondsight.storage.session_reports_repository import (
    SessionReportsRepository,
)

UTC = timezone.utc
_BASE = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / ".secondsight"
    h.mkdir()
    return h


def _client(home: Path) -> TestClient:
    app = create_app(secondsight_home=home)
    return TestClient(app, raise_server_exceptions=False)


def _seed(
    home: Path,
    *,
    project_id: str,
    sessions: Iterable[tuple[str, list[BehaviorFlagType]]],
    directives_active: int = 0,
    directives_disabled: int = 0,
) -> None:
    """Seed session_reports + behavior_flags + directives.

    sessions = iterable of (session_id, [flag_types_for_that_session]).
    """
    registry = ProjectRegistry(secondsight_home=home)
    resources = asyncio.run(registry.get(project_id))
    flags_repo = BehaviorFlagsRepository(resources.db_engine)
    flags_repo.create_schema()
    reports_repo = SessionReportsRepository(resources.db_engine)
    reports_repo.create_schema()
    directives_repo = DirectivesRepository(resources.db_engine)
    directives_repo.create_schema()

    for i, (session_id, flag_types) in enumerate(sessions):
        ts = _BASE + timedelta(minutes=i)
        reports_repo.upsert(
            SessionReport(
                id=f"rep-{session_id}",
                project_id=project_id,
                session_id=session_id,
                analysis_run_id=f"run-{session_id}",
                headline=f"Session {session_id}",
                key_findings=[f"finding-{i}"],
                body=f"body for {session_id}",
                created_at=ts,
                updated_at=ts,
            )
        )
        for j, ft in enumerate(flag_types):
            flags_repo.insert(
                BehaviorFlag(
                    id=f"flag-{session_id}-{j}",
                    project_id=project_id,
                    session_id=session_id,
                    segment_index=0,
                    flag_type=ft,
                    event_ids=[f"e-{j}"],
                    intent_summary="x",
                    reason="y",
                    confidence="high",
                    created_at=ts,
                )
            )
    for k in range(directives_active):
        directives_repo.insert(
            Directive(
                id=f"d-active-{k}",
                project_id=project_id,
                type=DirectiveType.CONVENTION,
                status=DirectiveStatus.ACTIVE,
                instruction=f"convention {k}",
                identity_key=f"ka-{k}",
                created_at=_BASE,
                updated_at=_BASE,
            )
        )
    for k in range(directives_disabled):
        directives_repo.insert(
            Directive(
                id=f"d-disabled-{k}",
                project_id=project_id,
                type=DirectiveType.CONVENTION,
                status=DirectiveStatus.DISABLED,
                instruction=f"disabled {k}",
                identity_key=f"kd-{k}",
                created_at=_BASE,
                updated_at=_BASE,
                disabled_at=_BASE + timedelta(minutes=1),
                disabled_reason="x",
            )
        )
    asyncio.run(registry.aclose())


def _add_one_flag(home: Path, project_id: str, session_id: str) -> None:
    registry = ProjectRegistry(secondsight_home=home)
    resources = asyncio.run(registry.get(project_id))
    flags_repo = BehaviorFlagsRepository(resources.db_engine)
    flags_repo.create_schema()
    flags_repo.insert(
        BehaviorFlag(
            id=f"flag-extra-{session_id}",
            project_id=project_id,
            session_id=session_id,
            segment_index=0,
            flag_type=BehaviorFlagType.UNNECESSARY_READ,
            event_ids=["x"],
            intent_summary="x",
            reason="y",
            confidence="high",
            created_at=_BASE + timedelta(hours=1),
        )
    )
    asyncio.run(registry.aclose())


# =====================================================================
# DEATH PATHS
# =====================================================================


class TestDeathPaths:
    def test_dt_3_1_cross_project_session_returns_404(self, home: Path) -> None:
        """DC-1 — session in project A; GET with project_id=B → 404."""
        _seed(
            home,
            project_id="A",
            sessions=[("S1", [BehaviorFlagType.UNNECESSARY_READ])],
        )
        with _client(home) as client:
            r = client.get(
                "/api/analysis/sessions/S1", params={"project_id": "B"}
            )
        assert r.status_code == 404, r.text

    def test_dt_3_2_missing_session_report_returns_404(
        self, home: Path
    ) -> None:
        """DC-6 — session has no session_reports row → 404."""
        # Seed flags but NO report for the session.
        registry = ProjectRegistry(secondsight_home=home)
        resources = asyncio.run(registry.get("P"))
        flags_repo = BehaviorFlagsRepository(resources.db_engine)
        flags_repo.create_schema()
        SessionReportsRepository(resources.db_engine).create_schema()
        flags_repo.insert(
            BehaviorFlag(
                id="f-1",
                project_id="P",
                session_id="orphan",
                segment_index=0,
                flag_type=BehaviorFlagType.UNNECESSARY_READ,
                event_ids=["e1"],
                intent_summary="x",
                reason="y",
                confidence="high",
                created_at=_BASE,
            )
        )
        asyncio.run(registry.aclose())
        with _client(home) as client:
            r = client.get(
                "/api/analysis/sessions/orphan", params={"project_id": "P"}
            )
        assert r.status_code == 404, r.text
        assert "not analyzed" in r.text.lower()

    def test_dt_3_3_trends_limit_applies_to_session_set(
        self, home: Path
    ) -> None:
        """DC-7 — 50 sessions × 5 flags; limit=10 → 10 session buckets."""
        sessions = [
            (f"sess-{i:02d}", [BehaviorFlagType.UNNECESSARY_READ] * 5)
            for i in range(50)
        ]
        _seed(home, project_id="P", sessions=sessions)
        with _client(home) as client:
            r = client.get(
                "/api/analysis/trends",
                params={"project_id": "P", "limit": 10},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["buckets"]) == 10, (
            f"DC-7: expected 10 sessions, got {len(body['buckets'])}"
        )

    def test_dt_3_4_summary_etag_changes_after_flag_insert(
        self, home: Path
    ) -> None:
        """DC-3 — adding a behavior_flag must invalidate the summary ETag."""
        _seed(
            home,
            project_id="P",
            sessions=[("S1", [BehaviorFlagType.UNNECESSARY_READ])],
        )
        with _client(home) as client:
            r1 = client.get(
                "/api/analysis/summary", params={"project_id": "P"}
            )
            etag1 = r1.headers.get("etag")
            assert etag1, "summary endpoint must emit ETag"
        _add_one_flag(home, "P", "S1")
        with _client(home) as client:
            r2 = client.get(
                "/api/analysis/summary", params={"project_id": "P"}
            )
            etag2 = r2.headers.get("etag")
        assert etag1 != etag2, (
            f"DC-3: ETag stale after flag insert ({etag1} == {etag2})"
        )

    def test_dt_3_5_summary_etag_stable_when_unchanged(
        self, home: Path
    ) -> None:
        """DC-3 negative: no writes → ETag stable, If-None-Match → 304."""
        _seed(
            home,
            project_id="P",
            sessions=[("S1", [BehaviorFlagType.UNNECESSARY_READ])],
        )
        with _client(home) as client:
            r1 = client.get(
                "/api/analysis/summary", params={"project_id": "P"}
            )
            etag1 = r1.headers.get("etag")
            r2 = client.get(
                "/api/analysis/summary",
                params={"project_id": "P"},
                headers={"if-none-match": etag1},
            )
        assert r2.status_code == 304

    def test_dt_3_6_pagination_next_offset_null_on_last_page(
        self, home: Path
    ) -> None:
        sessions = [
            (f"S{i:03d}", [BehaviorFlagType.UNNECESSARY_READ])
            for i in range(15)
        ]
        _seed(home, project_id="P", sessions=sessions)
        with _client(home) as client:
            r1 = client.get(
                "/api/analysis/sessions",
                params={"project_id": "P", "limit": 10, "offset": 0},
            )
            r2 = client.get(
                "/api/analysis/sessions",
                params={"project_id": "P", "limit": 10, "offset": 10},
            )
        b1 = r1.json()
        b2 = r2.json()
        assert len(b1["items"]) == 10
        assert b1["next_offset"] == 10
        assert len(b2["items"]) == 5
        assert b2["next_offset"] is None

    def test_dt_3_7_behavior_flag_out_includes_confidence(self) -> None:
        """Memory contract — confidence MUST be a present field."""
        schema = BehaviorFlagOut.model_json_schema()
        assert "confidence" in schema["properties"], schema["properties"]
        confidence_schema = schema["properties"]["confidence"]
        # Pydantic Literal[...] renders as enum in json schema
        # — accept either 'enum' key or oneOf shape depending on version.
        assert (
            confidence_schema.get("enum")
            == ["high", "medium", "low"]
            or any(
                "high" in str(opt)
                for opt in confidence_schema.get("oneOf", [])
            )
            or "high" in str(confidence_schema)
        ), confidence_schema

    def test_dt_3_8_trends_includes_zero_flag_session(
        self, home: Path
    ) -> None:
        """DC-7-adjacent: a session with a report but ZERO flags must
        appear in the trends bucket list."""
        _seed(
            home,
            project_id="P",
            sessions=[
                ("WithFlag", [BehaviorFlagType.UNNECESSARY_READ]),
                ("Empty", []),
            ],
        )
        with _client(home) as client:
            r = client.get("/api/analysis/trends", params={"project_id": "P"})
        body = r.json()
        ids = {b["session_id"] for b in body["buckets"]}
        assert ids == {"WithFlag", "Empty"}
        empty_bucket = next(
            b for b in body["buckets"] if b["session_id"] == "Empty"
        )
        assert empty_bucket["counts_by_type"] == {}


class TestHappyPaths:
    def test_hp_3_1_summary_counts(self, home: Path) -> None:
        _seed(
            home,
            project_id="P",
            sessions=[
                ("S1", [BehaviorFlagType.UNNECESSARY_READ] * 5),
                ("S2", [BehaviorFlagType.REDUNDANT_EXPLORATION] * 4),
                (
                    "S3",
                    [
                        BehaviorFlagType.UNNECESSARY_READ,
                        BehaviorFlagType.MISSED_SHORTCUT,
                        BehaviorFlagType.MISSED_SHORTCUT,
                    ],
                ),
                ("S4", []),
                ("S5", []),
            ],
            directives_active=3,
            directives_disabled=2,
        )
        with _client(home) as client:
            r = client.get(
                "/api/analysis/summary", params={"project_id": "P"}
            )
        body = r.json()
        assert body["analyzed_session_count"] == 5
        # Total flags = 5 + 4 + 3 + 0 + 0 = 12
        assert sum(body["flag_counts_by_type"].values()) == 12
        assert body["active_directive_count"] == 3
        assert body["last_analyzed_at"] is not None

    def test_hp_3_2_session_detail_joins_report_and_flags(
        self, home: Path
    ) -> None:
        _seed(
            home,
            project_id="P",
            sessions=[
                (
                    "S1",
                    [
                        BehaviorFlagType.UNNECESSARY_READ,
                        BehaviorFlagType.UNNECESSARY_READ,
                        BehaviorFlagType.MISSED_SHORTCUT,
                    ],
                )
            ],
        )
        with _client(home) as client:
            r = client.get(
                "/api/analysis/sessions/S1",
                params={"project_id": "P"},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["session_id"] == "S1"
        assert body["headline"] == "Session S1"
        assert len(body["flags"]) == 3
        # Confidence surfaces on every flag (memory contract).
        assert all(f["confidence"] == "high" for f in body["flags"])

    def test_hp_3_3_aggregation_matches_count_by_type(
        self, home: Path
    ) -> None:
        _seed(
            home,
            project_id="P",
            sessions=[
                (
                    "S1",
                    [
                        BehaviorFlagType.UNNECESSARY_READ,
                        BehaviorFlagType.UNNECESSARY_READ,
                    ],
                ),
                ("S2", [BehaviorFlagType.UNNECESSARY_READ]),
                ("S3", [BehaviorFlagType.MISSED_SHORTCUT]),
            ],
        )
        with _client(home) as client:
            r = client.get(
                "/api/analysis/aggregation", params={"project_id": "P"}
            )
        body = r.json()
        # flag counts: UNNECESSARY_READ=3, MISSED_SHORTCUT=1
        assert body["flag_counts_by_type"]["unnecessary_read"] == 3
        assert body["flag_counts_by_type"]["missed_shortcut"] == 1
        # session counts: UNNECESSARY_READ in 2 sessions, MISSED_SHORTCUT in 1
        assert body["session_counts_by_type"]["unnecessary_read"] == 2
        assert body["session_counts_by_type"]["missed_shortcut"] == 1

    def test_hp_3_4_summary_304_on_if_none_match(self, home: Path) -> None:
        _seed(
            home,
            project_id="P",
            sessions=[("S1", [BehaviorFlagType.UNNECESSARY_READ])],
        )
        with _client(home) as client:
            r1 = client.get(
                "/api/analysis/summary", params={"project_id": "P"}
            )
            etag = r1.headers.get("etag")
            r2 = client.get(
                "/api/analysis/summary",
                params={"project_id": "P"},
                headers={"if-none-match": etag},
            )
        assert r2.status_code == 304


class TestDC4ProjectIdRequired:
    @pytest.mark.parametrize(
        "path",
        [
            "/api/analysis/summary",
            "/api/analysis/sessions",
            "/api/analysis/sessions/sess-1",
            "/api/analysis/sessions/sess-1/flags",
            "/api/analysis/trends",
            "/api/analysis/aggregation",
        ],
    )
    def test_no_project_id_returns_422(self, home: Path, path: str) -> None:
        with _client(home) as client:
            r = client.get(path)
        assert r.status_code == 422
