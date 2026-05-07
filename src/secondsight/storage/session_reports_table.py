"""session_reports table — SQLAlchemy Core schema (GUR-102 task-1).

Artifact persistence for per-session analysis output. One row per
session (UNIQUE on session_id); upserted by SessionReportsRepository
to allow re-runs to update without accumulating duplicate rows.

Notes:
- `key_findings` is JSON-encoded list[str]; decoded at the repository
  layer.
- UNIQUE(session_id) named "uq_session_reports_session_id" enables the
  artifact-identity UPSERT pattern.
- idx_sr_project_created index supports list_for_project ORDER BY
  created_at DESC.
- Reuses events_table.metadata.
"""

from __future__ import annotations

import sqlalchemy as sa

from secondsight.storage.events_table import metadata

session_reports = sa.Table(
    "session_reports",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("analysis_run_id", sa.Text, nullable=False),
    sa.Column("headline", sa.Text, nullable=False),
    sa.Column(
        "key_findings",
        sa.Text,
        nullable=False,
    ),  # JSON-encoded list[str]
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False),
    sa.Column("updated_at", sa.DateTime, nullable=False),
    sa.UniqueConstraint(
        "session_id",
        name="uq_session_reports_session_id",
    ),
)

sa.Index(
    "idx_sr_project_created",
    session_reports.c.project_id,
    session_reports.c.created_at.desc(),
)
