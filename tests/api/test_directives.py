"""Death + happy-path tests for the Directives API (GUR-104 task-2).

Death cases pinned in plan §Tech Spec / acceptance.yaml:

- DC-1: cross-project leak — PATCH /api/directives/{id}?project_id=B for a
  directive in project A returns 404, never B's data.
- DC-2: PATCH no-op idempotency — a PATCH whose target state matches the
  current state returns 200 with the row but does NOT advance updated_at,
  does NOT issue a DB UPDATE, and does NOT change the listing's ETag.
- DC-5: GET /api/directives default scope — active=true is the default;
  disabled directives MUST NOT appear unless active=false is explicitly
  passed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest
from fastapi.testclient import TestClient

from secondsight.analysis.schemas import (
    Directive,
    DirectiveStatus,
    DirectiveType,
)
from secondsight.api.registry import ProjectRegistry
from secondsight.api.server import create_app
from secondsight.storage.directives_repository import DirectivesRepository

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fixtures + seed helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / ".secondsight"
    h.mkdir()
    return h


def _client(home: Path) -> TestClient:
    app = create_app(secondsight_home=home)
    return TestClient(app, raise_server_exceptions=False)


def _seed_directive(
    home: Path,
    *,
    project_id: str,
    directive_id: str,
    status: DirectiveStatus = DirectiveStatus.ACTIVE,
    instruction: str = "Always read AGENTS.md first.",
    frequency: float | None = 0.7,
    identity_key: Optional[str] = None,
    disabled_at: datetime | None = None,
    disabled_reason: str | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> Directive:
    """Materialize the project's resources, then directly write a directive
    row. Returns the Directive that was inserted."""
    registry = ProjectRegistry(secondsight_home=home)
    resources = asyncio.run(registry.get(project_id))
    db_engine = resources.db_engine
    repo = DirectivesRepository(db_engine)
    repo.create_schema()

    now = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)
    directive = Directive(
        id=directive_id,
        project_id=project_id,
        type=DirectiveType.CONVENTION,
        status=status,
        instruction=instruction,
        frequency=frequency,
        identity_key=identity_key or f"key-{directive_id}",
        source_sessions=["s1"],
        created_at=created_at or now,
        updated_at=updated_at or now,
        disabled_at=disabled_at,
        disabled_reason=disabled_reason,
    )
    repo.insert(directive)

    asyncio.run(registry.aclose())
    return directive


def _read_directive(home: Path, project_id: str, directive_id: str) -> Directive | None:
    """Read a directive row directly through the repository (test-only)."""
    registry = ProjectRegistry(secondsight_home=home)
    resources = asyncio.run(registry.get(project_id))
    repo = DirectivesRepository(resources.db_engine)
    repo.create_schema()
    out = repo.get_by_id(directive_id)
    asyncio.run(registry.aclose())
    return out


# =====================================================================
# DEATH PATHS
# =====================================================================


class TestDeathPaths:
    def test_dt_2_1_patch_noop_does_not_advance_updated_at(self, home: Path) -> None:
        """DC-2 — PATCH active→active must NOT mutate updated_at."""
        _seed_directive(home, project_id="P", directive_id="D1")
        # Read AFTER seed to get the stored (tz-naive) updated_at, so
        # the invariant tests value-equality, not tzinfo-equality.
        before = _read_directive(home, "P", "D1")
        assert before is not None
        original_updated_at = before.updated_at

        with _client(home) as client:
            r = client.patch(
                "/api/directives/D1",
                params={"project_id": "P"},
                json={"status": "active"},
            )
        assert r.status_code == 200, r.text

        after = _read_directive(home, "P", "D1")
        assert after is not None
        assert after.updated_at == original_updated_at, (
            f"DC-2 silent failure: PATCH no-op advanced updated_at from "
            f"{original_updated_at} to {after.updated_at}."
        )

    def test_dt_2_2_get_default_excludes_disabled(self, home: Path) -> None:
        """DC-5 — GET /api/directives default returns active only."""
        _seed_directive(home, project_id="P", directive_id="D-A1")
        _seed_directive(home, project_id="P", directive_id="D-A2")
        _seed_directive(
            home,
            project_id="P",
            directive_id="D-X",
            status=DirectiveStatus.DISABLED,
            disabled_at=datetime(2026, 5, 8, 13, 0, 0, tzinfo=UTC),
            disabled_reason="bad",
        )

        with _client(home) as client:
            r = client.get("/api/directives", params={"project_id": "P"})
        assert r.status_code == 200, r.text

        ids = {d["id"] for d in r.json()}
        assert ids == {"D-A1", "D-A2"}
        for d in r.json():
            assert d["status"] == "active"

    def test_dt_2_3_re_enable_clears_lifecycle_fields(self, home: Path) -> None:
        _seed_directive(
            home,
            project_id="P",
            directive_id="D1",
            status=DirectiveStatus.DISABLED,
            disabled_at=datetime(2026, 5, 8, 13, 0, 0, tzinfo=UTC),
            disabled_reason="wrong vocabulary",
        )
        before = _read_directive(home, "P", "D1")
        assert before is not None
        with _client(home) as client:
            r = client.patch(
                "/api/directives/D1",
                params={"project_id": "P"},
                json={"status": "active"},
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "active"
        assert body["disabled_at"] is None
        assert body["disabled_reason"] is None
        # updated_at strictly advanced — this is a real transition, not a no-op.
        # SQLite stores tz-naive datetimes; the API serializes whatever it has.
        # Read back from DB for tz-consistent comparison.
        after = _read_directive(home, "P", "D1")
        assert after is not None
        assert after.updated_at > before.updated_at

    def test_dt_2_4_disable_with_empty_reason_returns_400(self, home: Path) -> None:
        _seed_directive(home, project_id="P", directive_id="D1")
        with _client(home) as client:
            r = client.patch(
                "/api/directives/D1",
                params={"project_id": "P"},
                json={"status": "disabled", "reason": ""},
            )
        # Empty-string reason is structurally invalid (lifecycle contract).
        # FastAPI returns 422 for Pydantic validation; that's acceptable
        # because the message will name the failing field.
        assert r.status_code in (400, 422), r.text

    def test_dt_2_5_invalid_status_returns_400(self, home: Path) -> None:
        _seed_directive(home, project_id="P", directive_id="D1")
        with _client(home) as client:
            r = client.patch(
                "/api/directives/D1",
                params={"project_id": "P"},
                json={"status": "expired"},
            )
        # Pydantic Literal["active", "disabled"] rejects "expired" → 422.
        assert r.status_code in (400, 422), r.text

    def test_dt_2_6_cross_project_patch_returns_404(self, home: Path) -> None:
        """DC-1 — directive in project A; PATCH with project_id=B → 404."""
        _seed_directive(home, project_id="A", directive_id="D1")
        with _client(home) as client:
            r = client.patch(
                "/api/directives/D1",
                params={"project_id": "B"},
                json={"status": "disabled", "reason": "x"},
            )
        assert r.status_code == 404, r.text

    def test_dt_2_7_etag_changes_after_real_patch(self, home: Path) -> None:
        _seed_directive(home, project_id="P", directive_id="D1")
        with _client(home) as client:
            r1 = client.get("/api/directives", params={"project_id": "P"})
            etag1 = r1.headers.get("etag")
            assert etag1, "Expected ETag header on listing response"
            r2 = client.patch(
                "/api/directives/D1",
                params={"project_id": "P"},
                json={"status": "disabled", "reason": "bad"},
            )
            assert r2.status_code == 200, r2.text
            r3 = client.get(
                "/api/directives",
                params={"project_id": "P", "active": False},
            )
            etag3 = r3.headers.get("etag")
        assert etag1 != etag3, f"ETag must change after a real PATCH; got {etag1} == {etag3}"

    def test_dt_2_8_etag_unchanged_after_noop_patch(self, home: Path) -> None:
        _seed_directive(home, project_id="P", directive_id="D1")
        with _client(home) as client:
            r1 = client.get("/api/directives", params={"project_id": "P"})
            etag1 = r1.headers.get("etag")
            assert etag1
            r2 = client.patch(
                "/api/directives/D1",
                params={"project_id": "P"},
                json={"status": "active"},
            )
            assert r2.status_code == 200
            r3 = client.get("/api/directives", params={"project_id": "P"})
            etag3 = r3.headers.get("etag")
        assert etag1 == etag3, f"ETag must be stable across PATCH no-op; {etag1} != {etag3}"


class TestDegradation:
    def test_dg_2_1_openapi_patch_includes_phase3_caveat(self, home: Path) -> None:
        """DG-2.1 — PATCH route description must surface the Phase 3 cache
        runtime-effect caveat for operators."""
        with _client(home) as client:
            r = client.get("/openapi.json")
        assert r.status_code == 200
        spec = r.json()
        patch_path = (
            spec.get("paths", {}).get("/api/directives/{directive_id}", {}).get("patch", {})
        )
        description = patch_path.get("description") or patch_path.get("summary") or ""
        assert "GUR-105" in description or "Phase 3" in description
        assert "restart" in description.lower()


class TestHappyPaths:
    def test_hp_2_1_get_round_trip(self, home: Path) -> None:
        _seed_directive(home, project_id="P", directive_id="D1")
        _seed_directive(home, project_id="P", directive_id="D2")
        _seed_directive(
            home,
            project_id="P",
            directive_id="D3",
            status=DirectiveStatus.DISABLED,
            disabled_at=datetime(2026, 5, 8, 13, 0, 0, tzinfo=UTC),
            disabled_reason="x",
        )
        with _client(home) as client:
            r = client.get("/api/directives", params={"project_id": "P"})
        assert r.status_code == 200
        ids = {d["id"] for d in r.json()}
        assert ids == {"D1", "D2"}

    def test_hp_2_2_patch_active_to_disabled_persists_reason(self, home: Path) -> None:
        _seed_directive(home, project_id="P", directive_id="D1")
        with _client(home) as client:
            r = client.patch(
                "/api/directives/D1",
                params={"project_id": "P"},
                json={"status": "disabled", "reason": "wrong vocabulary"},
            )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "disabled"
        assert body["disabled_reason"] == "wrong vocabulary"
        assert body["disabled_at"] is not None

        after = _read_directive(home, "P", "D1")
        assert after is not None
        assert after.status == DirectiveStatus.DISABLED
        assert after.disabled_reason == "wrong vocabulary"
