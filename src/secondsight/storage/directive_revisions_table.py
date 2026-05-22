"""directive_revisions table — append-only directive rewrite ledger."""

from __future__ import annotations

import sqlalchemy as sa

from secondsight.storage.events_table import metadata

directive_revisions = sa.Table(
    "directive_revisions",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("directive_id", sa.Text, nullable=False),
    sa.Column("identity_key", sa.Text, nullable=False),
    sa.Column("revision_index", sa.Integer, nullable=False),
    sa.Column("old_instruction", sa.Text, nullable=False),
    sa.Column("new_instruction", sa.Text, nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("accepted", sa.Boolean, nullable=False),
    sa.Column("review_note", sa.Text, nullable=True),
    sa.Column("created_at", sa.DateTime, nullable=False),
    sa.UniqueConstraint(
        "directive_id",
        "revision_index",
        name="uq_directive_revisions_directive_index",
    ),
)

sa.Index(
    "idx_directive_revisions_directive_index",
    directive_revisions.c.directive_id,
    directive_revisions.c.revision_index,
)
