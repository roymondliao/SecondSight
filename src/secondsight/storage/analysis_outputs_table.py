"""analysis_outputs table — stores AnalysisOutput from mode-aware dispatch (Task 6).

This table is separate from analysis_runs (the existing pipeline audit table).

analysis_runs = audit trail for the SDK orchestrator pipeline (stage transitions).
    Stores per-stage rows (started → in_progress → summary_written → aggregated).
    Written by Orchestrator.start_run() and subsequent stage updates.
    This table still exists and is still written by the legacy SDK orchestrator path.

analysis_outputs = results from ModeAwareDispatch.dispatch() (Task 6 new path).
    Stores one latest-result row per session_id. Written by
    ModeAwareDispatch.dispatch() after the dispatcher (CLI or SDK) returns an
    AnalysisOutput; sequential reruns update the existing row.
    This is the NEW authoritative output table for mode-aware dispatch results.

Justification for separate table (IMPORTANT FIX 4 from task-6 review):
    analysis_runs was designed for the SDK orchestrator pipeline (SDK-only path).
    analysis_outputs is designed for ModeAwareDispatch (CLI or SDK).
    Merging them would require adding CLI-specific columns (cli_agent) to a table
    that was built for SDK stage tracking — semantic mismatch.
    The two tables serve genuinely different domain concepts:
    - analysis_runs: "what stage is the SDK orchestrator in right now?"
    - analysis_outputs: "what was the final dispatch result, and via which mode?"

    Pre-existing analysis_runs rows: rows from before Task 6 (SDK-only path) are
    unaffected. analysis_outputs starts empty. No migration is needed for old rows
    because old rows were never written by ModeAwareDispatch (which didn't exist).
    Dashboard/reporting code that queries analysis_runs is not broken by this addition.

Design decisions:
- One latest-result row per session. Retries within one dispatch are reflected
  in retry_count; a later sequential rerun overwrites the row with the newest
  dispatch result instead of creating another row.
- session_id has a UNIQUE constraint so concurrent dispatch (DC10) produces
  at most one row per session. Repository writes use UPSERT on session_id.
- dispatched_via is TEXT with a CHECK constraint: "cli" or "sdk" only.
  Adding a third mode requires a schema migration AND updating the CHECK.
- cli_agent is NULL when dispatched_via='sdk'. primary_model is NULL when
  dispatched_via='cli'. Both constraints are enforced at application level
  (AnalysisOutput.check_cross_fields()) not at DB level (SQLite CHECK
  expressions cannot reference other columns in CREATE TABLE).
- error_details is stored as JSON TEXT (SQLite has no native JSON column type).
  Parse with json.loads() when reading.
- Backward compat: this table is NEW. Pre-existing intelligence.db files will
  not have this table. analysis_outputs_repository.create_schema() uses
  checkfirst=True to create it on first use.

Columns added at Task 6:
  dispatched_via TEXT NOT NULL CHECK (dispatched_via IN ('cli', 'sdk'))
  cli_agent TEXT NULL
  primary_model TEXT NULL
  fallback_used INTEGER NOT NULL DEFAULT 0  -- SQLite boolean as integer
  retry_count INTEGER NOT NULL DEFAULT 0
  status TEXT NOT NULL  -- 'success' | 'failure' | 'unknown'
  error_details TEXT NULL  -- JSON string
  session_id TEXT NOT NULL UNIQUE  -- deduplication (DC10)
  project_id TEXT NOT NULL
  created_at DATETIME NOT NULL
"""

from __future__ import annotations

import sqlalchemy as sa

from secondsight.storage.events_table import metadata

analysis_outputs = sa.Table(
    "analysis_outputs",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("session_id", sa.Text, nullable=False, unique=True),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column(
        "dispatched_via",
        sa.Text,
        # CHECK constraint: only "cli" or "sdk" are valid dispatch modes.
        # Adding a third mode requires: (a) updating this CHECK, (b) adding
        # new column invariants, (c) updating AnalysisOutput cross-field validators.
        sa.CheckConstraint("dispatched_via IN ('cli', 'sdk')", name="ck_ao_dispatched_via"),
        nullable=False,
    ),
    sa.Column("cli_agent", sa.Text, nullable=True),
    sa.Column("primary_model", sa.Text, nullable=True),
    sa.Column(
        "fallback_used",
        sa.Integer,
        nullable=False,
        server_default="0",
    ),
    sa.Column(
        "retry_count",
        sa.Integer,
        nullable=False,
        server_default="0",
    ),
    sa.Column(
        "status",
        sa.Text,
        # CHECK constraint: only the three AnalysisStatus values are valid.
        sa.CheckConstraint("status IN ('success', 'failure', 'unknown')", name="ck_ao_status"),
        nullable=False,
    ),
    sa.Column("error_details", sa.Text, nullable=True),  # JSON string
    sa.Column("created_at", sa.DateTime, nullable=False),
)

sa.Index(
    "idx_ao_session_id",
    analysis_outputs.c.session_id,
    unique=True,
)
sa.Index(
    "idx_ao_project_created",
    analysis_outputs.c.project_id,
    analysis_outputs.c.created_at,
)
