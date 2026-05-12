"""GUR-104 Phase 2 wire-up integration smoke tests (task-5).

Test inventory:

Death tests (run first — expected to catch silent-registration failures):
- DT-5.1: analysis routes registered — GET /api/analysis/summary NOT 404
- DT-5.2: directives GET registered — GET /api/directives NOT 404
- DT-5.3: directives PATCH method registered — PATCH /api/directives/x NOT 405
- DT-5.4: CLI help lists modes — `secondsight directive --help` stdout
- DT-5.5: regression — analyze still works, exit 0, --session / --force present

Happy-path tests (real DB inserts, real HTTP round-trips):
- HP-5.1: E2E summary — insert session_report + 2 behavior_flags → GET summary
  counts match
- HP-5.2: E2E PATCH→GET — insert active directive, PATCH to disabled, GET
  active=true does NOT include the disabled directive

Notes:
- TestClient used with `with` context manager so FastAPI lifespan fires
  (ProjectRegistry initialization).
- DB injection helpers re-use the established fixture patterns from
  tests/api/test_directives.py and tests/api/test_analysis.py to avoid
  reinventing seeding logic.
- DTs 5.1–5.3 may PASS immediately due to pre-existing wiring from tasks 2/3
  (documented in task-2 and task-3 scar reports). That is expected and correct.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from secondsight.analysis.schemas import (
    BehaviorFlag,
    BehaviorFlagType,
    Directive,
    DirectiveStatus,
    DirectiveType,
    SessionReport,
)
from secondsight.api.registry import ProjectRegistry
from secondsight.api.server import create_app
from secondsight.cli.app import app as cli_app
from secondsight.storage.behavior_flags_repository import BehaviorFlagsRepository
from secondsight.storage.directives_repository import DirectivesRepository
from secondsight.storage.session_reports_repository import SessionReportsRepository

UTC = timezone.utc
_BASE = datetime(2026, 5, 8, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    h = tmp_path / ".secondsight"
    h.mkdir()
    return h


def _make_client(home: Path) -> TestClient:
    """Create a TestClient that fires lifespan on context-manager entry."""
    app = create_app(secondsight_home=home)
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# DB seeding helpers — mirrors test_analysis.py + test_directives.py patterns
# ---------------------------------------------------------------------------


def _seed_session_report(home: Path, *, project_id: str, session_id: str) -> None:
    registry = ProjectRegistry(secondsight_home=home)
    resources = asyncio.run(registry.get(project_id))
    reports_repo = SessionReportsRepository(resources.db_engine)
    reports_repo.create_schema()
    reports_repo.upsert(
        SessionReport(
            id=f"rep-{session_id}",
            project_id=project_id,
            session_id=session_id,
            analysis_run_id=f"run-{session_id}",
            headline=f"Session {session_id}",
            key_findings=["finding-1"],
            body=f"body for {session_id}",
            created_at=_BASE,
            updated_at=_BASE,
        )
    )
    asyncio.run(registry.aclose())


def _seed_behavior_flag(home: Path, *, project_id: str, session_id: str, flag_id: str) -> None:
    registry = ProjectRegistry(secondsight_home=home)
    resources = asyncio.run(registry.get(project_id))
    flags_repo = BehaviorFlagsRepository(resources.db_engine)
    flags_repo.create_schema()
    flags_repo.insert(
        BehaviorFlag(
            id=flag_id,
            project_id=project_id,
            session_id=session_id,
            segment_index=0,
            flag_type=BehaviorFlagType.UNNECESSARY_READ,
            event_ids=["e-1"],
            intent_summary="read unnecessarily",
            reason="already in context",
            confidence="high",
            created_at=_BASE,
        )
    )
    asyncio.run(registry.aclose())


def _seed_directive(
    home: Path,
    *,
    project_id: str,
    directive_id: str,
    status: DirectiveStatus = DirectiveStatus.ACTIVE,
) -> None:
    registry = ProjectRegistry(secondsight_home=home)
    resources = asyncio.run(registry.get(project_id))
    directives_repo = DirectivesRepository(resources.db_engine)
    directives_repo.create_schema()
    directives_repo.insert(
        Directive(
            id=directive_id,
            project_id=project_id,
            type=DirectiveType.CONVENTION,
            status=status,
            instruction="Always read AGENTS.md first.",
            identity_key=f"key-{directive_id}",
            source_sessions=["s1"],
            created_at=_BASE,
            updated_at=_BASE,
        )
    )
    asyncio.run(registry.aclose())


# =====================================================================
# DEATH PATHS
# =====================================================================


class TestDeathPaths:
    def test_dt_5_1_analysis_routes_registered(self, home: Path) -> None:
        """DT-5.1 — GET /api/analysis/summary must NOT return 404.

        If the analysis router is not registered, FastAPI returns 404 with
        {"detail": "Not Found"}. Any other status (200, 422, 500) is acceptable
        here — we only care that the route RESOLVES.
        """
        with _make_client(home) as client:
            r = client.get("/api/analysis/summary", params={"project_id": "nonexistent-proj"})
        assert r.status_code != 404, (
            f"DT-5.1 FAILED: analysis_router not registered — got 404. Body: {r.text}"
        )

    def test_dt_5_2_directives_get_registered(self, home: Path) -> None:
        """DT-5.2 — GET /api/directives must NOT return 404."""
        with _make_client(home) as client:
            r = client.get("/api/directives", params={"project_id": "nonexistent-proj"})
        assert r.status_code != 404, (
            f"DT-5.2 FAILED: directives_router GET not registered — got 404. Body: {r.text}"
        )

    def test_dt_5_3_directives_patch_method_registered(self, home: Path) -> None:
        """DT-5.3 — PATCH /api/directives/{id} must NOT return 405.

        405 = route exists but PATCH method not accepted. Any other status
        is acceptable (404 for unknown ID is fine, 422 for validation also fine).
        We only assert NOT 405 here.
        """
        with _make_client(home) as client:
            r = client.patch(
                "/api/directives/some-directive-id",
                params={"project_id": "nonexistent-proj"},
                json={"status": "active"},
            )
        assert r.status_code != 405, (
            f"DT-5.3 FAILED: PATCH method not registered on directives route. "
            f"Got 405. Body: {r.text}"
        )

    def test_dt_5_4_cli_directive_help_lists_flags(self) -> None:
        """DT-5.4 — `secondsight directive --help` must list key flags.

        If the directive subcommand is not registered in cli/app.py, the runner
        will fail with a non-zero exit code or output a 'No such command' error.
        """
        runner = CliRunner()
        result = runner.invoke(cli_app, ["directive", "--help"])
        assert result.exit_code == 0, (
            f"DT-5.4 FAILED: `secondsight directive --help` exited {result.exit_code}. "
            f"Output: {result.output}"
        )
        output = result.output
        assert "--active" in output, (
            f"DT-5.4: --active flag missing from directive help. Output: {output}"
        )
        assert "--disable" in output, (
            f"DT-5.4: --disable flag missing from directive help. Output: {output}"
        )
        assert "--enable" in output, (
            f"DT-5.4: --enable flag missing from directive help. Output: {output}"
        )
        assert "--format" in output, (
            f"DT-5.4: --format flag missing from directive help. Output: {output}"
        )
        assert "--no-server" in output, (
            f"DT-5.4: --no-server flag missing from directive help. Output: {output}"
        )

    def test_dt_5_5_regression_analyze_still_works(self) -> None:
        """DT-5.5 — regression net for GUR-103: `secondsight analyze --help`.

        If wire-up in task-5 introduces a top-level import error (e.g., a
        circular import in api/ or cli/), this test will fail with a non-zero
        exit code before the help text is printed. That signals a breaking
        regression, not a test issue — diagnose, don't paper over.
        """
        runner = CliRunner()
        result = runner.invoke(cli_app, ["analyze", "--help"])
        assert result.exit_code == 0, (
            f"DT-5.5 REGRESSION: `secondsight analyze --help` exited "
            f"{result.exit_code}. This means task-5 wire-up broke GUR-103 CLI. "
            f"Output: {result.output}"
        )
        output = result.output
        assert "--session" in output, (
            f"DT-5.5: --session flag missing from analyze help. Output: {output}"
        )
        assert "--force" in output, (
            f"DT-5.5: --force flag missing from analyze help. Output: {output}"
        )


# =====================================================================
# HAPPY PATHS (E2E with real DB inserts)
# =====================================================================


class TestHappyPaths:
    def test_hp_5_1_summary_counts_match_db_inserts(self, home: Path) -> None:
        """HP-5.1 — E2E summary: insert 1 session_report + 2 behavior_flags,
        then GET /api/analysis/summary and assert counts match.

        This test exercises the full chain:
        1. Direct DB insert (bypassing API — realistic to a post-analysis state)
        2. HTTP GET /api/analysis/summary through TestClient (lifespan active)
        3. Response counts must match what was inserted
        """
        project_id = "e2e-proj-hp51"

        # Insert 1 session report.
        _seed_session_report(home, project_id=project_id, session_id="s-e2e-1")

        # Insert 2 behavior flags for that session.
        _seed_behavior_flag(
            home,
            project_id=project_id,
            session_id="s-e2e-1",
            flag_id="flag-e2e-1",
        )
        _seed_behavior_flag(
            home,
            project_id=project_id,
            session_id="s-e2e-1",
            flag_id="flag-e2e-2",
        )

        with _make_client(home) as client:
            r = client.get("/api/analysis/summary", params={"project_id": project_id})

        assert r.status_code == 200, f"HP-5.1: expected 200, got {r.status_code}. Body: {r.text}"
        body = r.json()

        # 1 analyzed session.
        assert body["analyzed_session_count"] == 1, (
            f"HP-5.1: expected analyzed_session_count=1, got {body['analyzed_session_count']}"
        )

        # 2 behavior flags total.
        total_flags = sum(body["flag_counts_by_type"].values())
        assert total_flags == 2, (
            f"HP-5.1: expected 2 total flags across all types, got {total_flags}. "
            f"flag_counts_by_type={body['flag_counts_by_type']}"
        )

        # ETag must be present (dashboard polling contract).
        assert r.headers.get("etag"), (
            "HP-5.1: GET /api/analysis/summary must emit an ETag header for dashboard polling."
        )

    def test_hp_5_2_patch_to_disabled_excluded_from_active_listing(self, home: Path) -> None:
        """HP-5.2 — E2E PATCH→GET: insert 1 active directive, PATCH to disabled,
        then GET /api/directives with default active=true filter confirms the
        directive is absent.

        This is the "Success - PATCH /api/directives/{id}" acceptance scenario.
        Chain: DB insert → PATCH via API → GET via API (active filter default)
        """
        project_id = "e2e-proj-hp52"
        directive_id = "dir-e2e-1"

        # Insert 1 active directive directly.
        _seed_directive(
            home,
            project_id=project_id,
            directive_id=directive_id,
            status=DirectiveStatus.ACTIVE,
        )

        with _make_client(home) as client:
            # Confirm it appears in the active listing before PATCH.
            r_before = client.get("/api/directives", params={"project_id": project_id})
            assert r_before.status_code == 200, (
                f"HP-5.2 pre-PATCH GET failed: {r_before.status_code} {r_before.text}"
            )
            before_ids = {d["id"] for d in r_before.json()}
            assert directive_id in before_ids, (
                f"HP-5.2: directive {directive_id!r} not found in active listing "
                f"before PATCH. Got IDs: {before_ids}"
            )

            # PATCH to disabled with a reason.
            r_patch = client.patch(
                f"/api/directives/{directive_id}",
                params={"project_id": project_id},
                json={"status": "disabled", "reason": "wrong vocabulary — e2e test"},
            )
            assert r_patch.status_code == 200, (
                f"HP-5.2 PATCH failed: {r_patch.status_code} {r_patch.text}"
            )
            patch_body = r_patch.json()
            assert patch_body["status"] == "disabled", (
                f"HP-5.2: PATCH response status != 'disabled'. Body: {patch_body}"
            )
            assert patch_body["disabled_at"] is not None, (
                f"HP-5.2: disabled_at must be set after PATCH. Body: {patch_body}"
            )
            assert patch_body["disabled_reason"] == "wrong vocabulary — e2e test", (
                f"HP-5.2: disabled_reason mismatch. Body: {patch_body}"
            )

            # GET active=true (default): disabled directive must NOT appear.
            r_after = client.get("/api/directives", params={"project_id": project_id})
            assert r_after.status_code == 200, (
                f"HP-5.2 post-PATCH GET failed: {r_after.status_code} {r_after.text}"
            )
            after_ids = {d["id"] for d in r_after.json()}
            assert directive_id not in after_ids, (
                f"HP-5.2 FAILED: disabled directive {directive_id!r} still appears "
                f"in active listing after PATCH. IDs present: {after_ids}"
            )
