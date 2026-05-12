"""Death + happy-path tests for SessionReportsRepository (GUR-102 task-1).

Death cases:
- Upsert idempotency: inserting twice with same session_id and different
  content must preserve created_at, update remaining fields, row count = 1.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from secondsight.analysis.schemas import SessionReport
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.session_reports_repository import SessionReportsRepository


def _now() -> datetime:
    return datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)


def _report(
    *,
    id: str = "report-1",
    project_id: str = "proj-1",
    session_id: str = "sess-1",
    analysis_run_id: str = "run-1",
    headline: str = "Agent avoided unnecessary reads",
    key_findings: list[str] | None = None,
    body: str = "Full analysis body text.",
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> SessionReport:
    return SessionReport(
        id=id,
        project_id=project_id,
        session_id=session_id,
        analysis_run_id=analysis_run_id,
        headline=headline,
        key_findings=key_findings
        if key_findings is not None
        else ["finding 1", "finding 2", "finding 3"],
        body=body,
        created_at=created_at if created_at is not None else _now(),
        updated_at=updated_at if updated_at is not None else _now(),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[SessionReportsRepository]:
    eng = DBEngine(tmp_path / "intel.db")
    r = SessionReportsRepository(eng)
    r.create_schema()
    try:
        yield r
    finally:
        eng.dispose()


# =====================================================================
# DEATH TESTS
# =====================================================================


class TestDeathPaths:
    def test_dt_guard_rejects_key_findings_overflow_via_model_construct(
        self, repo: SessionReportsRepository
    ) -> None:
        """IMPORTANT-2: upsert() must reject key_findings > 5 even when
        built via model_construct() (which bypasses Pydantic validation).

        Death case: model_construct() skips Field(max_length=5). Without
        the _guard(), a 6-item key_findings list would silently persist to
        the DB. The dashboard would then render a 6th finding that the
        summary.py prompt never intended to produce, corrupting the UX
        contract.
        """
        from secondsight.analysis.schemas import SessionReport

        # model_construct bypasses Field(max_length=5)
        bypassed = SessionReport.model_construct(
            id="guard-test-1",
            project_id="proj-1",
            session_id="sess-guard",
            analysis_run_id="run-1",
            headline="Valid headline",
            key_findings=["f1", "f2", "f3", "f4", "f5", "f6"],  # 6 items — over limit
            body="body text",
            created_at=datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc),
            updated_at=datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc),
        )
        with pytest.raises(ValueError) as exc:
            repo.upsert(bypassed)

        msg = str(exc.value)
        assert "key_findings" in msg, (
            "ValueError must mention key_findings so callers understand the constraint"
        )
        assert "6" in msg or "5" in msg, "ValueError must mention the count or limit"

        # No row was persisted
        assert repo.get_for_session("sess-guard") is None

    def test_dt_guard_rejects_empty_headline_via_model_construct(
        self, repo: SessionReportsRepository
    ) -> None:
        """IMPORTANT-2: upsert() must reject empty headline even when
        built via model_construct() (which bypasses Pydantic validation).
        """
        from secondsight.analysis.schemas import SessionReport

        bypassed = SessionReport.model_construct(
            id="guard-test-2",
            project_id="proj-1",
            session_id="sess-guard-headline",
            analysis_run_id="run-1",
            headline="",  # violates min_length=1
            key_findings=["f1"],
            body="body text",
            created_at=datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc),
            updated_at=datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc),
        )
        with pytest.raises(ValueError) as exc:
            repo.upsert(bypassed)

        assert "headline" in str(exc.value).lower()
        assert repo.get_for_session("sess-guard-headline") is None

    def test_dt_upsert_idempotency_preserves_created_at(
        self, repo: SessionReportsRepository
    ) -> None:
        """Death case: upsert must NOT overwrite created_at on conflict.

        If the upsert blindly overwrites all fields on conflict, the
        original creation timestamp is lost. The session artifact
        then reports an incorrect creation time after every re-run,
        making it impossible to audit when the first analysis happened.

        Specifically verifies:
        - row count remains 1
        - created_at is the ORIGINAL value
        - analysis_run_id, headline, key_findings, body, updated_at
          are updated to the second call's values
        """
        first_created_at = datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc)
        r1 = _report(
            session_id="sess-upsert",
            analysis_run_id="run-1",
            headline="First headline",
            key_findings=["first finding"],
            created_at=first_created_at,
            updated_at=first_created_at,
        )
        repo.upsert(r1)

        second_updated_at = datetime(2026, 5, 6, 14, 0, 0, tzinfo=timezone.utc)
        r2 = _report(
            id="report-2",  # different id, same session_id
            session_id="sess-upsert",
            analysis_run_id="run-2",
            headline="Updated headline",
            key_findings=["new finding 1", "new finding 2"],
            created_at=second_updated_at,
            updated_at=second_updated_at,
        )
        repo.upsert(r2)

        # Row count must be 1
        all_reports = repo.list_for_project("proj-1")
        assert len(all_reports) == 1, (
            f"Expected 1 row after upsert, got {len(all_reports)}. "
            "UNIQUE(session_id) upsert must not insert a second row."
        )

        got = repo.get_for_session("sess-upsert")
        assert got is not None

        # created_at must be FIRST value preserved
        assert got.created_at.replace(tzinfo=None) == first_created_at.replace(tzinfo=None), (
            "upsert overwrote created_at — must preserve original creation time"
        )

        # These must be updated to second call values
        assert got.headline == "Updated headline", "headline must be updated on conflict"
        assert got.analysis_run_id == "run-2", "analysis_run_id must be updated on conflict"
        assert got.key_findings == ["new finding 1", "new finding 2"], (
            "key_findings must be updated on conflict"
        )


# =====================================================================
# HAPPY PATHS
# =====================================================================


class TestHappyPaths:
    def test_round_trip_key_findings_preserves_order(self, repo: SessionReportsRepository) -> None:
        """JSON encoding/decoding of key_findings must preserve order.

        key_findings is stored as JSON-encoded list[str]. Order drift
        after serialization would cause deterministic test failures but
        more critically changes the semantics of "top finding" ordering
        in the dashboard.
        """
        findings = ["finding c", "finding a", "finding b"]  # non-alpha order
        r = _report(
            session_id="sess-order",
            key_findings=findings,
        )
        repo.upsert(r)

        got = repo.get_for_session("sess-order")
        assert got is not None
        assert got.key_findings == findings, (
            f"key_findings order changed during round-trip: "
            f"expected {findings}, got {got.key_findings}"
        )

    def test_get_for_session_returns_none_when_missing(
        self, repo: SessionReportsRepository
    ) -> None:
        result = repo.get_for_session("nonexistent-session")
        assert result is None

    def test_list_for_project_orders_by_created_at_desc(
        self, repo: SessionReportsRepository
    ) -> None:
        """list_for_project returns reports ordered by created_at DESC."""
        r1 = _report(
            id="r-1",
            session_id="sess-a",
            created_at=datetime(2026, 5, 6, 9, 0, 0, tzinfo=timezone.utc),
            updated_at=datetime(2026, 5, 6, 9, 0, 0, tzinfo=timezone.utc),
        )
        r2 = _report(
            id="r-2",
            session_id="sess-b",
            created_at=datetime(2026, 5, 6, 11, 0, 0, tzinfo=timezone.utc),
            updated_at=datetime(2026, 5, 6, 11, 0, 0, tzinfo=timezone.utc),
        )
        r3 = _report(
            id="r-3",
            session_id="sess-c",
            created_at=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc),
            updated_at=datetime(2026, 5, 6, 10, 0, 0, tzinfo=timezone.utc),
        )
        for r in (r1, r2, r3):
            repo.upsert(r)

        results = repo.list_for_project("proj-1")
        ids = [r.id for r in results]
        assert ids == ["r-2", "r-3", "r-1"], f"Expected created_at DESC order, got {ids}"

    def test_list_for_project_pagination(self, repo: SessionReportsRepository) -> None:
        """limit and offset correctly paginate results."""
        for i in range(5):
            repo.upsert(
                _report(
                    id=f"r-{i}",
                    session_id=f"sess-{i}",
                    created_at=datetime(2026, 5, 6, i, 0, 0, tzinfo=timezone.utc),
                    updated_at=datetime(2026, 5, 6, i, 0, 0, tzinfo=timezone.utc),
                )
            )

        page1 = repo.list_for_project("proj-1", limit=2, offset=0)
        page2 = repo.list_for_project("proj-1", limit=2, offset=2)
        assert len(page1) == 2
        assert len(page2) == 2
        # No overlap
        assert {r.id for r in page1}.isdisjoint({r.id for r in page2})

    def test_list_for_project_excludes_other_projects(self, repo: SessionReportsRepository) -> None:
        r_mine = _report(session_id="sess-mine", project_id="proj-1")
        r_other = _report(id="r-other", session_id="sess-other", project_id="proj-other")
        repo.upsert(r_mine)
        repo.upsert(r_other)

        results = repo.list_for_project("proj-1")
        assert len(results) == 1
        assert results[0].session_id == "sess-mine"

    def test_round_trip_all_fields(self, repo: SessionReportsRepository) -> None:
        """Every non-datetime field round-trips exactly."""
        original = _report(
            id="rr-1",
            project_id="proj-rt",
            session_id="sess-rt",
            analysis_run_id="run-rt",
            headline="Headline with special chars: 你好",
            key_findings=["a", "b", "c"],
            body="Body content\nMultiline.",
        )
        repo.upsert(original)
        got = repo.get_for_session("sess-rt")
        assert got is not None

        for field in (
            "id",
            "project_id",
            "session_id",
            "analysis_run_id",
            "headline",
            "key_findings",
            "body",
        ):
            assert getattr(got, field) == getattr(original, field), (
                f"{field} mutated during round-trip"
            )

    def test_create_schema_idempotent(self, tmp_path: Path) -> None:
        eng = DBEngine(tmp_path / "intel.db")
        try:
            r = SessionReportsRepository(eng)
            r.create_schema()
            r.create_schema()
        finally:
            eng.dispose()
