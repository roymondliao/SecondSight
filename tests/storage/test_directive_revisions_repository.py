"""Death + happy-path tests for DirectiveRevisionsRepository."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest

from secondsight.analysis.schemas import DirectiveRevision
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.directive_revisions_repository import (
    DirectiveRevisionsRepository,
)


def _now() -> datetime:
    return datetime(2026, 5, 22, 12, 0, 0, tzinfo=timezone.utc)


def _revision(
    *,
    id: str = "rev-1",
    project_id: str = "proj-1",
    directive_id: str = "dir-1",
    identity_key: str = "lineage-1",
    revision_index: int = 1,
    old_instruction: str = "old instruction",
    new_instruction: str = "new instruction",
    reason: str = "identity_missed_but_flag_family_alive",
    accepted: bool = True,
    review_note: str | None = "accepted after review",
) -> DirectiveRevision:
    return DirectiveRevision(
        id=id,
        project_id=project_id,
        directive_id=directive_id,
        identity_key=identity_key,
        revision_index=revision_index,
        old_instruction=old_instruction,
        new_instruction=new_instruction,
        reason=reason,
        accepted=accepted,
        review_note=review_note,
        created_at=_now(),
    )


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[DirectiveRevisionsRepository]:
    eng = DBEngine(tmp_path / "intel.db")
    r = DirectiveRevisionsRepository(eng)
    r.create_schema()
    try:
        yield r
    finally:
        eng.dispose()


class TestDeathPaths:
    def test_dt_append_is_append_only_for_same_directive(
        self, repo: DirectiveRevisionsRepository
    ) -> None:
        """Accepted rewrites must append a second row, not mutate the first."""
        first = _revision(
            id="rev-1",
            revision_index=1,
            new_instruction="rev-1 instruction",
        )
        second = _revision(
            id="rev-2",
            revision_index=2,
            old_instruction="rev-1 instruction",
            new_instruction="rev-2 instruction",
        )

        repo.append(first)
        repo.append(second)

        rows = repo.list_for_directive("dir-1")
        assert [row.id for row in rows] == ["rev-1", "rev-2"]
        assert rows[0].new_instruction == "rev-1 instruction"
        assert rows[1].new_instruction == "rev-2 instruction"

    def test_dt_duplicate_revision_index_for_same_directive_raises(
        self, repo: DirectiveRevisionsRepository
    ) -> None:
        repo.append(_revision(id="rev-1", revision_index=1))
        with pytest.raises(ValueError) as exc:
            repo.append(_revision(id="rev-2", revision_index=1))
        assert "revision_index" in str(exc.value)


class TestQueries:
    def test_list_for_directive_orders_by_revision_index(
        self, repo: DirectiveRevisionsRepository
    ) -> None:
        repo.append(_revision(id="rev-2", revision_index=2))
        repo.append(_revision(id="rev-1", revision_index=1))

        rows = repo.list_for_directive("dir-1")
        assert [row.revision_index for row in rows] == [1, 2]

    def test_next_revision_index_advances_after_append(
        self, repo: DirectiveRevisionsRepository
    ) -> None:
        assert repo.next_revision_index("dir-1") == 1
        repo.append(_revision(id="rev-1", revision_index=1))
        assert repo.next_revision_index("dir-1") == 2
