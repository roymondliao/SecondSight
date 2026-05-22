"""directives table — SQLAlchemy Core schema (SD §7.4 + memory contract).

The `disabled_at` and `disabled_reason` columns are additions to SD §7.4
mandated by `project_directive_lifecycle_contract`. The SD patch that
adds these columns to the canonical DDL is part of task-5 (D3 ship gate).

`status` and `type` columns are TEXT; validation lives at the
repository layer (D1 — mirrors events.event_type convention).

`identity_key` (GUR-102 task-1, lifecycle hygiene follow-up):
    Project-scoped directive identity. The current aggregator still fills this
    via its pre-lineage strategy; lifecycle hygiene later redefines the
    generator. The DDL contract here is only that the value is non-empty per
    project on real rows.
    server_default="" is a TRANSITIONAL DDL default only — valid because
    the table is empty pre-Phase 3 (G4). The DirectivesRepository
    upsert_with_identity_key() guard rejects empty identity_key, so no
    real row should ever hold "". If the table is NOT empty pre-Phase 3
    (early dogfooding), a backfill is required before the unique index
    can be enforced.

    UNIQUE(project_id, identity_key) named "uq_directives_project_identity"
    enables the aggregator UPSERT pattern in DirectivesRepository.
"""

from __future__ import annotations

import sqlalchemy as sa

from secondsight.storage.events_table import metadata

directives = sa.Table(
    "directives",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("type", sa.Text, nullable=False),  # convention | hint
    sa.Column(
        "status",
        sa.Text,
        nullable=False,
    ),  # active|disabled|expired|superseded|obsolete|stalled
    sa.Column("instruction", sa.Text, nullable=False),
    sa.Column("frequency", sa.Float, nullable=True),
    sa.Column("trigger_pattern", sa.Text, nullable=True),  # hint reserved
    sa.Column("confidence", sa.Float, nullable=True),  # hint reserved
    sa.Column("max_firing", sa.Integer, nullable=True),  # hint reserved
    sa.Column("source_flag_type", sa.Text, nullable=True),
    sa.Column(
        "source_sessions",
        sa.Text,
        nullable=False,
        server_default="[]",
    ),  # JSON-encoded list[str]
    sa.Column("weight", sa.Float, nullable=False, server_default="0.7"),
    sa.Column("miss_streak", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_promoted_at", sa.DateTime, nullable=True),
    sa.Column("last_source_flag_seen_at", sa.DateTime, nullable=True),
    sa.Column("revision_count", sa.Integer, nullable=False, server_default="0"),
    sa.Column("last_revised_at", sa.DateTime, nullable=True),
    sa.Column("created_at", sa.DateTime, nullable=False),
    sa.Column("expires_at", sa.DateTime, nullable=True),
    sa.Column("updated_at", sa.DateTime, nullable=False),
    sa.Column("disabled_at", sa.DateTime, nullable=True),
    sa.Column("disabled_reason", sa.Text, nullable=True),
    sa.Column(
        "identity_key",
        sa.Text,
        nullable=False,
        server_default="",
    ),  # transitional default — see module docstring; empty rejected by repo
    sa.UniqueConstraint(
        "project_id",
        "identity_key",
        name="uq_directives_project_identity",
    ),
)

sa.Index(
    "idx_directives_project_status",
    directives.c.project_id,
    directives.c.status,
)
sa.Index(
    "idx_directives_project_type",
    directives.c.project_id,
    directives.c.type,
)
