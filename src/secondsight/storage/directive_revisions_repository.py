"""DirectiveRevisionsRepository — append-only ledger for directive rewrites."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from secondsight.analysis.schemas import DirectiveRevision
from secondsight.storage.db_engine import DBEngine
from secondsight.storage.directive_revisions_table import (
    directive_revisions,
    metadata,
)


class DirectiveRevisionsRepository:
    def __init__(self, db_engine: DBEngine) -> None:
        self._db = db_engine

    def create_schema(self) -> None:
        metadata.create_all(self._db.engine, checkfirst=True)

    def append(self, revision: DirectiveRevision) -> None:
        row = self._revision_to_row(revision)
        try:
            with self._db.engine.begin() as conn:
                conn.execute(directive_revisions.insert().values(**row))
        except IntegrityError as exc:
            raise ValueError(
                "DirectiveRevisionsRepository.append: duplicate revision_index "
                f"for directive_id={revision.directive_id!r}, "
                f"revision_index={revision.revision_index!r}"
            ) from exc

    def list_for_directive(self, directive_id: str) -> list[DirectiveRevision]:
        stmt = (
            sa.select(directive_revisions)
            .where(directive_revisions.c.directive_id == directive_id)
            .order_by(
                directive_revisions.c.revision_index.asc(),
                directive_revisions.c.id.asc(),
            )
        )
        with self._db.engine.connect() as conn:
            return [self._row_to_revision(r) for r in conn.execute(stmt).mappings()]

    def next_revision_index(self, directive_id: str) -> int:
        stmt = sa.select(sa.func.max(directive_revisions.c.revision_index)).where(
            directive_revisions.c.directive_id == directive_id
        )
        with self._db.engine.connect() as conn:
            current = conn.execute(stmt).scalar()
        return int(current or 0) + 1

    @staticmethod
    def _revision_to_row(revision: DirectiveRevision) -> dict[str, Any]:
        return {
            "id": revision.id,
            "project_id": revision.project_id,
            "directive_id": revision.directive_id,
            "identity_key": revision.identity_key,
            "revision_index": revision.revision_index,
            "old_instruction": revision.old_instruction,
            "new_instruction": revision.new_instruction,
            "reason": revision.reason,
            "accepted": revision.accepted,
            "review_note": revision.review_note,
            "created_at": revision.created_at,
        }

    @staticmethod
    def _row_to_revision(row: sa.RowMapping) -> DirectiveRevision:
        return DirectiveRevision(
            id=row["id"],
            project_id=row["project_id"],
            directive_id=row["directive_id"],
            identity_key=row["identity_key"],
            revision_index=row["revision_index"],
            old_instruction=row["old_instruction"],
            new_instruction=row["new_instruction"],
            reason=row["reason"],
            accepted=row["accepted"],
            review_note=row["review_note"],
            created_at=row["created_at"],
        )
