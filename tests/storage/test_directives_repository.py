"""Death + lifecycle + happy-path tests for DirectivesRepository (task-3)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from secondsight.analysis.schemas import (
    Directive,
    DirectiveStatus,
    DirectiveType,
)
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.directives_repository import DirectivesRepository


def _now() -> datetime:
    return datetime(2026, 5, 6, 12, 0, 0, tzinfo=timezone.utc)


def _directive(
    *,
    id: str = "dir-1",
    project_id: str = "proj-1",
    type: DirectiveType = DirectiveType.CONVENTION,
    status: DirectiveStatus = DirectiveStatus.ACTIVE,
    frequency: float | None = 0.7,
    instruction: str = "Skip exploration when path is given",
    disabled_at: datetime | None = None,
    disabled_reason: str | None = None,
) -> Directive:
    return Directive(
        id=id,
        project_id=project_id,
        type=type,
        status=status,
        instruction=instruction,
        frequency=frequency,
        source_flag_type="unnecessary_read",
        source_sessions=["s1", "s2"],
        created_at=_now(),
        updated_at=_now(),
        disabled_at=disabled_at,
        disabled_reason=disabled_reason,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[DirectivesRepository]:
    eng = DBEngine(tmp_path / "intel.db")
    r = DirectivesRepository(eng)
    r.create_schema()
    try:
        yield r
    finally:
        eng.dispose()


# =====================================================================
# DEATH TESTS
# =====================================================================


class TestDeathPaths:
    def test_dt_3_1_disabled_without_reason_raises_no_update(
        self, repo: DirectivesRepository
    ) -> None:
        """DT-3.1 — disabled transition must require a non-None reason.

        Audit-trail contract: every disable carries a reason.
        """
        repo.insert(_directive())
        with pytest.raises(ValueError) as exc:
            repo.update_status("dir-1", DirectiveStatus.DISABLED, reason=None)
        assert "reason" in str(exc.value).lower()

        # Status unchanged, disabled_at still null.
        d = repo.get_by_id("dir-1")
        assert d is not None
        assert d.status is DirectiveStatus.ACTIVE
        assert d.disabled_at is None

    def test_dt_3_2_non_disabled_with_reason_raises(
        self, repo: DirectivesRepository
    ) -> None:
        """DT-3.2 — non-disabled transitions cannot carry a reason."""
        repo.insert(_directive())
        with pytest.raises(ValueError) as exc:
            repo.update_status(
                "dir-1", DirectiveStatus.ACTIVE, reason="late note"
            )
        assert "reason" in str(exc.value).lower()

    def test_dt_3_3_insert_rejects_model_construct_bypass(
        self, repo: DirectivesRepository
    ) -> None:
        """DT-3.3 — Pydantic bypass on status must be rejected at repo."""
        bypassed = Directive.model_construct(
            id="bp-1",
            project_id="proj-1",
            type=DirectiveType.CONVENTION,
            status="frozen",  # invalid
            instruction="x",
            frequency=0.5,
            source_sessions=[],
            created_at=_now(),
            updated_at=_now(),
        )
        with pytest.raises(ValueError) as exc:
            repo.insert(bypassed)
        assert "status" in str(exc.value).lower()

        assert repo.get_by_id("bp-1") is None

    def test_dt_3_4_re_active_clears_disabled_metadata(
        self, repo: DirectivesRepository
    ) -> None:
        """DT-3.4 — re-active must clear disabled_at + disabled_reason.

        Without the clear, re-active rows still report stale "disabled
        at..." metadata, which kills the audit-trail invariant
        (only disabled rows own those fields).
        """
        repo.insert(_directive())
        repo.update_status(
            "dir-1", DirectiveStatus.DISABLED, reason="superseded by D-2"
        )

        # Sanity: disabled state has the metadata.
        before = repo.get_by_id("dir-1")
        assert before is not None
        assert before.status is DirectiveStatus.DISABLED
        assert before.disabled_at is not None
        assert before.disabled_reason == "superseded by D-2"

        # Re-active must clear.
        repo.update_status("dir-1", DirectiveStatus.ACTIVE)
        after = repo.get_by_id("dir-1")
        assert after is not None
        assert after.status is DirectiveStatus.ACTIVE
        assert after.disabled_at is None, "stale disabled_at leaked"
        assert after.disabled_reason is None, "stale disabled_reason leaked"

    def test_dt_3_5_update_status_unknown_id_raises(
        self, repo: DirectivesRepository
    ) -> None:
        """update_status on a missing id must raise, not silently no-op."""
        with pytest.raises(LookupError):
            repo.update_status("does-not-exist", DirectiveStatus.SUPERSEDED)

    def test_dt_3_6_invalid_status_type_raises(
        self, repo: DirectivesRepository
    ) -> None:
        """update_status requires a DirectiveStatus enum, not a string."""
        repo.insert(_directive())
        with pytest.raises(ValueError) as exc:
            repo.update_status("dir-1", "active")  # type: ignore[arg-type]
        assert "DirectiveStatus" in str(exc.value)

    def test_dt_3_7_insert_rejects_active_with_disabled_metadata(
        self, repo: DirectivesRepository
    ) -> None:
        """Lifecycle invariant at insert: status=active MUST NOT carry
        disabled_at or disabled_reason. update_status enforces this on
        UPDATE; insert must enforce it too (model_construct bypass).
        """
        d = _directive(
            status=DirectiveStatus.ACTIVE,
            disabled_at=_now(),
            disabled_reason="leaked",
        )
        with pytest.raises(ValueError) as exc:
            repo.insert(d)
        msg = str(exc.value).lower()
        assert "lifecycle" in msg or "disabled" in msg

    def test_dt_3_8_insert_rejects_disabled_without_metadata(
        self, repo: DirectivesRepository
    ) -> None:
        """Lifecycle invariant at insert: status=disabled MUST carry
        both disabled_at and disabled_reason.
        """
        d = _directive(
            status=DirectiveStatus.DISABLED,
            disabled_at=None,
            disabled_reason=None,
        )
        with pytest.raises(ValueError) as exc:
            repo.insert(d)
        msg = str(exc.value).lower()
        assert "disabled_at" in msg or "disabled_reason" in msg


# =====================================================================
# HAPPY PATHS / LIFECYCLE
# =====================================================================


class TestLifecycle:
    def test_active_disabled_active_full_cycle(
        self, repo: DirectivesRepository
    ) -> None:
        repo.insert(_directive())

        repo.update_status(
            "dir-1", DirectiveStatus.DISABLED, reason="testing"
        )
        d = repo.get_by_id("dir-1")
        assert d is not None
        assert d.status is DirectiveStatus.DISABLED
        assert d.disabled_at is not None
        assert d.disabled_reason == "testing"

        repo.update_status("dir-1", DirectiveStatus.ACTIVE)
        d = repo.get_by_id("dir-1")
        assert d is not None
        assert d.status is DirectiveStatus.ACTIVE
        assert d.disabled_at is None
        assert d.disabled_reason is None

    def test_superseded_transition_clears_disabled_metadata(
        self, repo: DirectivesRepository
    ) -> None:
        """Analyzer-set transitions also clear the disabled metadata."""
        repo.insert(_directive())
        repo.update_status(
            "dir-1", DirectiveStatus.DISABLED, reason="legacy"
        )
        repo.update_status("dir-1", DirectiveStatus.SUPERSEDED)
        d = repo.get_by_id("dir-1")
        assert d is not None
        assert d.status is DirectiveStatus.SUPERSEDED
        assert d.disabled_at is None
        assert d.disabled_reason is None


class TestQueries:
    def test_get_active_conventions_filters_and_sorts(
        self, repo: DirectivesRepository
    ) -> None:
        repo.insert(_directive(id="d-1", frequency=0.3))
        repo.insert(_directive(id="d-2", frequency=0.9))
        # disabled — should not appear
        repo.insert(
            _directive(
                id="d-3",
                status=DirectiveStatus.DISABLED,
                disabled_at=_now(),
                disabled_reason="test",
                frequency=0.99,
            )
        )
        # other type — should not appear
        repo.insert(
            _directive(id="d-4", type=DirectiveType.HINT, frequency=0.99)
        )
        # other project — should not appear
        repo.insert(_directive(id="d-5", project_id="proj-2"))

        rows = repo.get_active_conventions("proj-1")
        ids = [r.id for r in rows]
        assert ids == ["d-2", "d-1"], (
            "Expected active conventions sorted by frequency desc; "
            f"got {ids}"
        )

    def test_get_by_id_returns_none_when_missing(
        self, repo: DirectivesRepository
    ) -> None:
        assert repo.get_by_id("nope") is None

    def test_insert_idempotent_first_wins(
        self, repo: DirectivesRepository
    ) -> None:
        a = _directive(id="d-x", frequency=0.1)
        b = _directive(id="d-x", frequency=0.99)
        repo.insert(a)
        repo.insert(b)
        d = repo.get_by_id("d-x")
        assert d is not None
        assert d.frequency == 0.1

    def test_create_schema_idempotent(self, tmp_path: Path) -> None:
        eng = DBEngine(tmp_path / "intel.db")
        try:
            r = DirectivesRepository(eng)
            r.create_schema()
            r.create_schema()
        finally:
            eng.dispose()
