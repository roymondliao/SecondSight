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
    identity_key: str | None = None,
) -> Directive:
    # Default identity_key to the directive id to avoid UNIQUE(project_id,
    # identity_key) conflicts when multiple directives are inserted in tests.
    # Pre-GUR-102 code that creates directives without identity_key still works
    # since each directive has a unique id.
    return Directive(
        id=id,
        project_id=project_id,
        type=type,
        status=status,
        instruction=instruction,
        frequency=frequency,
        source_flag_type="unnecessary_read",
        source_sessions=["s1", "s2"],
        identity_key=identity_key if identity_key is not None else id,
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

    def test_dt_3_2_non_disabled_with_reason_raises(self, repo: DirectivesRepository) -> None:
        """DT-3.2 — non-disabled transitions cannot carry a reason."""
        repo.insert(_directive())
        with pytest.raises(ValueError) as exc:
            repo.update_status("dir-1", DirectiveStatus.ACTIVE, reason="late note")
        assert "reason" in str(exc.value).lower()

    def test_dt_3_3_insert_rejects_model_construct_bypass(self, repo: DirectivesRepository) -> None:
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

    def test_dt_3_4_re_active_clears_disabled_metadata(self, repo: DirectivesRepository) -> None:
        """DT-3.4 — re-active must clear disabled_at + disabled_reason.

        Without the clear, re-active rows still report stale "disabled
        at..." metadata, which kills the audit-trail invariant
        (only disabled rows own those fields).
        """
        repo.insert(_directive())
        repo.update_status("dir-1", DirectiveStatus.DISABLED, reason="superseded by D-2")

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

    def test_dt_3_5_update_status_unknown_id_raises(self, repo: DirectivesRepository) -> None:
        """update_status on a missing id must raise, not silently no-op."""
        with pytest.raises(LookupError):
            repo.update_status("does-not-exist", DirectiveStatus.SUPERSEDED)

    def test_dt_3_6_invalid_status_type_raises(self, repo: DirectivesRepository) -> None:
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
    def test_active_disabled_active_full_cycle(self, repo: DirectivesRepository) -> None:
        repo.insert(_directive())

        repo.update_status("dir-1", DirectiveStatus.DISABLED, reason="testing")
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
        repo.update_status("dir-1", DirectiveStatus.DISABLED, reason="legacy")
        repo.update_status("dir-1", DirectiveStatus.SUPERSEDED)
        d = repo.get_by_id("dir-1")
        assert d is not None
        assert d.status is DirectiveStatus.SUPERSEDED
        assert d.disabled_at is None
        assert d.disabled_reason is None


class TestQueries:
    def test_get_active_conventions_filters_and_sorts(self, repo: DirectivesRepository) -> None:
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
        repo.insert(_directive(id="d-4", type=DirectiveType.HINT, frequency=0.99))
        # other project — should not appear
        repo.insert(_directive(id="d-5", project_id="proj-2"))

        rows = repo.get_active_conventions("proj-1")
        ids = [r.id for r in rows]
        assert ids == ["d-2", "d-1"], (
            f"Expected active conventions sorted by frequency desc; got {ids}"
        )

    def test_get_by_id_returns_none_when_missing(self, repo: DirectivesRepository) -> None:
        assert repo.get_by_id("nope") is None

    def test_insert_idempotent_first_wins(self, repo: DirectivesRepository) -> None:
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


# =====================================================================
# UPSERT WITH IDENTITY KEY — DEATH TESTS
# =====================================================================


def _directive_with_identity(
    *,
    id: str = "dir-uk-1",
    project_id: str = "proj-1",
    identity_key: str = "sha256:abc123",
    instruction: str = "Skip exploration when path is given",
    frequency: float = 0.7,
    source_sessions: list[str] | None = None,
) -> Directive:
    from secondsight.analysis.schemas import Directive

    return Directive(
        id=id,
        project_id=project_id,
        type=DirectiveType.CONVENTION,
        status=DirectiveStatus.ACTIVE,
        instruction=instruction,
        frequency=frequency,
        source_flag_type="unnecessary_read",
        source_sessions=source_sessions if source_sessions is not None else ["s1"],
        identity_key=identity_key,
        created_at=_now(),
        updated_at=_now(),
    )


class TestUpsertWithIdentityKey:
    def test_dt_empty_identity_key_raises(self, repo: DirectivesRepository) -> None:
        """Death case: calling upsert_with_identity_key with identity_key=''
        must raise ValueError. The server_default='' is a transitional
        DDL default only; no row should ever reach the DB with that value
        via the repository.

        If this guard is absent, empty-key rows from a pre-Phase-3
        partial migration leak into the UNIQUE(project_id, identity_key)
        constraint: the second directive in the same project would
        conflict on ('proj-1', '') and be silently dropped.
        """
        d = _directive_with_identity(identity_key="")
        with pytest.raises(ValueError) as exc:
            repo.upsert_with_identity_key(d)
        assert "identity_key" in str(exc.value).lower()

        # No row landed
        assert repo.get_active_conventions("proj-1") == []

    def test_dt_upsert_same_identity_key_updates_instruction(
        self, repo: DirectivesRepository
    ) -> None:
        """Death case: two upsert calls with same (project_id, identity_key)
        and different instruction. The second call's instruction and
        updated_at must win; status, type, created_at preserved.

        If this is INSERT-only, the second call would silently no-op
        (ON CONFLICT DO NOTHING) or crash (ON CONFLICT ERROR), leaving
        stale instruction text in the directive — the aggregator's
        updated analysis would never surface.
        """
        d1 = _directive_with_identity(
            id="d-uk-1",
            identity_key="sha256:abc",
            instruction="Original instruction",
            frequency=0.5,
            source_sessions=["s1"],
        )
        d2 = _directive_with_identity(
            id="d-uk-2",  # different id, same (project_id, identity_key)
            identity_key="sha256:abc",
            instruction="Updated instruction",
            frequency=0.9,
            source_sessions=["s1", "s2", "s3"],
        )

        repo.upsert_with_identity_key(d1)
        repo.upsert_with_identity_key(d2)

        rows = repo.get_active_conventions("proj-1")
        assert len(rows) == 1, (
            f"Expected 1 row after upsert, got {len(rows)}. "
            "UNIQUE(project_id, identity_key) must not insert a second row."
        )
        got = rows[0]

        # These must be updated:
        assert got.instruction == "Updated instruction", "instruction must be updated on conflict"
        assert got.frequency == 0.9, "frequency must be updated on conflict"
        assert got.source_sessions == ["s1", "s2", "s3"], (
            "source_sessions must be updated on conflict"
        )

        # These must be preserved from the first call:
        assert got.status is DirectiveStatus.ACTIVE, "status must be preserved on conflict"
        assert got.type is DirectiveType.CONVENTION, "type must be preserved on conflict"
        assert got.created_at.replace(tzinfo=None) == _now().replace(tzinfo=None), (
            "created_at must be preserved on conflict"
        )

    def test_upsert_insert_then_get_active_conventions(self, repo: DirectivesRepository) -> None:
        """Happy path: insert via upsert_with_identity_key then
        get_active_conventions returns it.
        """
        d = _directive_with_identity(identity_key="sha256:xyz")
        repo.upsert_with_identity_key(d)

        rows = repo.get_active_conventions("proj-1")
        assert len(rows) == 1
        assert rows[0].identity_key == "sha256:xyz"

    def test_upsert_different_identity_keys_create_separate_rows(
        self, repo: DirectivesRepository
    ) -> None:
        """Different identity_keys in same project create separate rows."""
        d1 = _directive_with_identity(
            id="d-a", identity_key="sha256:aaa", instruction="Directive A"
        )
        d2 = _directive_with_identity(
            id="d-b", identity_key="sha256:bbb", instruction="Directive B"
        )
        repo.upsert_with_identity_key(d1)
        repo.upsert_with_identity_key(d2)

        rows = repo.get_active_conventions("proj-1")
        assert len(rows) == 2

    def test_upsert_rejects_model_construct_bypass_empty_key(
        self, repo: DirectivesRepository
    ) -> None:
        """model_construct bypass with empty identity_key must be rejected."""
        from secondsight.analysis.schemas import Directive

        bypassed = Directive.model_construct(
            id="bp-uk-1",
            project_id="proj-1",
            type=DirectiveType.CONVENTION,
            status=DirectiveStatus.ACTIVE,
            instruction="x",
            frequency=0.5,
            source_sessions=[],
            identity_key="",  # bypass guard
            created_at=_now(),
            updated_at=_now(),
        )
        with pytest.raises(ValueError):
            repo.upsert_with_identity_key(bypassed)
        assert repo.get_active_conventions("proj-1") == []


# =====================================================================
# INSERT INTEGRITY ERROR → VALUEERROR (CRITICAL-2 fix)
# =====================================================================


class TestInsertIntegrityErrorConversion:
    def test_dt_insert_empty_identity_key_second_insert_raises_value_error(
        self, repo: DirectivesRepository
    ) -> None:
        """CRITICAL-2: Second insert() with identity_key='' in same project
        must raise ValueError (not raw IntegrityError) from the
        uq_directives_project_identity constraint.

        Death case: before the fix, a raw sqlalchemy.exc.IntegrityError
        propagated. Callers expecting idempotency on `id` (the ON CONFLICT
        DO NOTHING target) could not distinguish:
        - Expected no-op: duplicate `id` → silent
        - Unexpected: (project_id, identity_key='') UNIQUE violation → crash

        After the fix, IntegrityError is caught and re-raised as ValueError
        with a message naming the constraint and the offending values.
        """
        from secondsight.analysis.schemas import Directive

        # First insert with identity_key="" succeeds (with WARNING log)
        d1 = Directive(
            id="dup-empty-key-1",
            project_id="proj-conflict",
            type=DirectiveType.CONVENTION,
            status=DirectiveStatus.ACTIVE,
            instruction="first directive",
            frequency=0.5,
            source_flag_type="unnecessary_read",
            source_sessions=["s1"],
            identity_key="",  # transitional default
            created_at=_now(),
            updated_at=_now(),
        )
        repo.insert(d1)

        # Second insert with identity_key="" in same project must raise
        # ValueError — not raw IntegrityError
        d2 = Directive(
            id="dup-empty-key-2",  # different id, same project + identity_key=""
            project_id="proj-conflict",
            type=DirectiveType.CONVENTION,
            status=DirectiveStatus.ACTIVE,
            instruction="second directive",
            frequency=0.6,
            source_flag_type="unnecessary_read",
            source_sessions=["s2"],
            identity_key="",  # same empty key — collision
            created_at=_now(),
            updated_at=_now(),
        )
        with pytest.raises(ValueError) as exc:
            repo.insert(d2)

        # The message must name the constraint and the values involved
        msg = str(exc.value)
        assert "uq_directives_project_identity" in msg, (
            "ValueError must name the constraint so callers can distinguish "
            "from other ValueError paths"
        )
        assert "identity_key" in msg, (
            "ValueError must name identity_key so callers understand the issue"
        )

        # First directive is unaffected
        d1_fetched = repo.get_by_id("dup-empty-key-1")
        assert d1_fetched is not None

        # Second directive was never inserted
        assert repo.get_by_id("dup-empty-key-2") is None
