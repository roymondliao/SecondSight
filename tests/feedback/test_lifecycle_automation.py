"""Tests for lifecycle automation (GUR-108, P3B-2 + P3B-3)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from secondsight.analysis.schemas import (
    BehaviorFlag,
    BehaviorFlagType,
    Directive,
    DirectiveStatus,
    DirectiveType,
)
from secondsight.feedback.lifecycle_automation import (
    _enforce_capacity_ceiling,
    enforce_expiry,
    enforce_reactivation,
    run_lifecycle_automation,
)
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.directives_repository import DirectivesRepository
from secondsight.storage.behavior_flags_repository import BehaviorFlagsRepository

_PROJECT_ID = "proj-lifecycle-test"


@pytest.fixture()
def db_engine(tmp_path) -> DBEngine:
    db = DBEngine(tmp_path / "test.db")
    return db


@pytest.fixture()
def directives_repo(db_engine: DBEngine) -> DirectivesRepository:
    repo = DirectivesRepository(db_engine)
    repo.create_schema()
    return repo


@pytest.fixture()
def flags_repo(db_engine: DBEngine) -> BehaviorFlagsRepository:
    repo = BehaviorFlagsRepository(db_engine)
    repo.create_schema()
    return repo


def _make_directive(
    *,
    status: DirectiveStatus = DirectiveStatus.ACTIVE,
    expires_at: datetime | None = None,
    source_flag_type: str | None = None,
    directive_id: str | None = None,
    frequency: float | None = 0.5,
    weight: float = 0.7,
) -> Directive:
    now = datetime.now(tz=timezone.utc)
    return Directive(
        id=directive_id or str(uuid.uuid4()),
        project_id=_PROJECT_ID,
        type=DirectiveType.CONVENTION,
        status=status,
        instruction="Test convention instruction",
        frequency=frequency,
        source_flag_type=source_flag_type,
        source_sessions=["sess-1"],
        identity_key=str(uuid.uuid4()),
        weight=weight,
        created_at=now,
        expires_at=expires_at,
        updated_at=now,
    )


def _make_flag(
    flag_type: BehaviorFlagType = BehaviorFlagType.UNNECESSARY_READ,
    created_at: datetime | None = None,
) -> BehaviorFlag:
    return BehaviorFlag(
        id=str(uuid.uuid4()),
        project_id=_PROJECT_ID,
        session_id="sess-recent",
        segment_index=0,
        flag_type=flag_type,
        event_ids=["evt-1"],
        intent_summary="test summary",
        reason="test reason",
        confidence="high",
        created_at=created_at or datetime.now(tz=timezone.utc),
    )


class TestEnforceExpiry:
    def test_expires_past_conventions(
        self,
        directives_repo: DirectivesRepository,
    ) -> None:
        past = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        d = _make_directive(expires_at=past)
        directives_repo.insert(d)

        count = enforce_expiry(_PROJECT_ID, directives_repo)
        assert count == 1

        updated = directives_repo.get_by_id(d.id)
        assert updated is not None
        assert updated.status == DirectiveStatus.EXPIRED

    def test_does_not_expire_future_conventions(
        self,
        directives_repo: DirectivesRepository,
    ) -> None:
        future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        d = _make_directive(expires_at=future)
        directives_repo.insert(d)

        count = enforce_expiry(_PROJECT_ID, directives_repo)
        assert count == 0

    def test_does_not_expire_conventions_without_ttl(
        self,
        directives_repo: DirectivesRepository,
    ) -> None:
        d = _make_directive(expires_at=None)
        directives_repo.insert(d)

        count = enforce_expiry(_PROJECT_ID, directives_repo)
        assert count == 0


class TestEnforceReactivation:
    def test_does_not_reactivate_obsolete_with_recent_flags_anymore(
        self,
        directives_repo: DirectivesRepository,
        flags_repo: BehaviorFlagsRepository,
        db_engine: DBEngine,
    ) -> None:
        d = _make_directive(
            status=DirectiveStatus.OBSOLETE,
            source_flag_type=BehaviorFlagType.UNNECESSARY_READ.value,
            directive_id="d-reactivate",
        )
        directives_repo.insert(d)

        flag = _make_flag(flag_type=BehaviorFlagType.UNNECESSARY_READ)
        flags_repo.insert(flag)

        count = enforce_reactivation(
            _PROJECT_ID,
            directives_repo,
            db_engine,
        )
        assert count == 0

        updated = directives_repo.get_by_id("d-reactivate")
        assert updated is not None
        assert updated.status == DirectiveStatus.OBSOLETE

    def test_does_not_reactivate_without_recent_flags(
        self,
        directives_repo: DirectivesRepository,
        flags_repo: BehaviorFlagsRepository,
        db_engine: DBEngine,
    ) -> None:
        d = _make_directive(
            status=DirectiveStatus.OBSOLETE,
            source_flag_type=BehaviorFlagType.UNNECESSARY_READ.value,
        )
        directives_repo.insert(d)
        # No flags inserted

        count = enforce_reactivation(
            _PROJECT_ID,
            directives_repo,
            db_engine,
        )
        assert count == 0


class TestEnforceCapacityCeiling:
    def test_sheds_lowest_weight_even_if_frequency_is_highest(
        self,
        directives_repo: DirectivesRepository,
    ) -> None:
        directives_repo.insert(
            _make_directive(
                directive_id="d-high-frequency-low-weight",
                frequency=0.95,
                weight=0.10,
            )
        )
        directives_repo.insert(
            _make_directive(
                directive_id="d-low-frequency-high-weight",
                frequency=0.10,
                weight=0.90,
            )
        )
        directives_repo.insert(
            _make_directive(
                directive_id="d-mid",
                frequency=0.50,
                weight=0.80,
            )
        )

        count = _enforce_capacity_ceiling(_PROJECT_ID, directives_repo, ceiling=2)

        assert count == 1
        low_weight = directives_repo.get_by_id("d-high-frequency-low-weight")
        assert low_weight is not None
        assert low_weight.status is DirectiveStatus.OBSOLETE

        retained = directives_repo.get_active_conventions(_PROJECT_ID)
        assert {directive.id for directive in retained} == {
            "d-low-frequency-high-weight",
            "d-mid",
        }

    def test_does_not_reactivate_active_conventions(
        self,
        directives_repo: DirectivesRepository,
        flags_repo: BehaviorFlagsRepository,
        db_engine: DBEngine,
    ) -> None:
        d = _make_directive(
            status=DirectiveStatus.ACTIVE,
            source_flag_type=BehaviorFlagType.UNNECESSARY_READ.value,
        )
        directives_repo.insert(d)

        flag = _make_flag(flag_type=BehaviorFlagType.UNNECESSARY_READ)
        flags_repo.insert(flag)

        count = enforce_reactivation(
            _PROJECT_ID,
            directives_repo,
            db_engine,
        )
        assert count == 0


class TestRunLifecycleAutomation:
    def test_combined_expiry_and_reactivation(
        self,
        directives_repo: DirectivesRepository,
        flags_repo: BehaviorFlagsRepository,
        db_engine: DBEngine,
    ) -> None:
        past = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        d_expired = _make_directive(expires_at=past, directive_id="d-expired")
        directives_repo.insert(d_expired)

        d_obsolete = _make_directive(
            status=DirectiveStatus.OBSOLETE,
            source_flag_type=BehaviorFlagType.UNNECESSARY_READ.value,
            directive_id="d-obsolete",
        )
        directives_repo.insert(d_obsolete)
        flag = _make_flag(flag_type=BehaviorFlagType.UNNECESSARY_READ)
        flags_repo.insert(flag)

        result = run_lifecycle_automation(
            _PROJECT_ID,
            directives_repo,
            db_engine,
        )
        assert result.expired_count == 1
        assert result.reactivated_count == 0
