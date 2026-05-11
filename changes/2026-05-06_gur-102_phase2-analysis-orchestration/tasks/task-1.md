# Task 1 — Storage layer: analysis_runs + session_reports + directives.identity_key migration

**Depends on:** none. **Blocks:** task-3, task-4, task-5.

## Goal

Create the two new tables (`analysis_runs`, `session_reports`) with
their repositories, and migrate the existing `directives` table to
add `identity_key TEXT NOT NULL` + `UNIQUE(project_id, identity_key)`.
This is foundational; nothing else in GUR-102 can land first.

## Files to create

- `src/secondsight/storage/analysis_runs_table.py`
- `src/secondsight/storage/analysis_runs_repository.py`
- `src/secondsight/storage/session_reports_table.py`
- `src/secondsight/storage/session_reports_repository.py`
- `tests/storage/test_analysis_runs_repository.py`
- `tests/storage/test_session_reports_repository.py`

## Files to modify

- `src/secondsight/storage/directives_table.py` — add `identity_key`
  column + unique constraint.
- `src/secondsight/storage/directives_repository.py` — add
  `upsert_with_identity_key(directive)` method that uses
  `INSERT ON CONFLICT(project_id, identity_key) DO UPDATE SET
  instruction, frequency, source_sessions, updated_at`. Existing
  `insert(directive)` remains for non-aggregator code paths.
- `tests/storage/test_directives_repository.py` — add tests for the
  new UPSERT path.

## Schema

### `analysis_runs`

```python
analysis_runs = sa.Table(
    "analysis_runs",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("stage", sa.Text, nullable=False),
    # 'pending' | 'segmented' | 'behavior_done' | 'summary_written' | 'aggregated' | 'failed'
    sa.Column("started_at", sa.DateTime, nullable=False),
    sa.Column("updated_at", sa.DateTime, nullable=False),
    sa.Column("completed_at", sa.DateTime, nullable=True),
    sa.Column("error_message", sa.Text, nullable=True),
    sa.Column("flags_inserted", sa.Integer, nullable=False, server_default="0"),
)
sa.Index("idx_ar_project_session", analysis_runs.c.project_id, analysis_runs.c.session_id)
sa.Index("idx_ar_project_stage", analysis_runs.c.project_id, analysis_runs.c.stage)
```

### `session_reports`

```python
session_reports = sa.Table(
    "session_reports",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("analysis_run_id", sa.Text, nullable=False),
    sa.Column("headline", sa.Text, nullable=False),
    sa.Column("key_findings", sa.Text, nullable=False),  # JSON-encoded list[str]
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False),
    sa.Column("updated_at", sa.DateTime, nullable=False),
    sa.UniqueConstraint("session_id", name="uq_session_reports_session_id"),
)
sa.Index("idx_sr_project_created", session_reports.c.project_id, session_reports.c.created_at.desc())
```

### `directives` migration

```python
sa.Column("identity_key", sa.Text, nullable=False, server_default=""),
sa.UniqueConstraint("project_id", "identity_key", name="uq_directives_project_identity"),
```

`server_default=""` is a transitional default — only valid because
the table is empty pre-Phase 3 (G4 of pre-thinking). Repository
guard rejects empty `identity_key` on insert.

## Repository methods

### `AnalysisRunsRepository`

```python
class AnalysisRunsRepository:
    def create_schema(self) -> None: ...
    def start_run(self, project_id: str, session_id: str) -> str:
        """Insert a new row at stage='pending'. Returns the run ID.
        IMPORTANT: this is called BEFORE any pipeline work begins —
        if the process dies after this insert, the row is the audit
        trail. Stage transitions are explicit advance_stage() calls."""
    def advance_stage(self, run_id: str, stage: str, *, flags_inserted: int = 0) -> None:
        """Move a run to the next stage. Validates the enum at the
        repository layer (no DB CHECK). flags_inserted is recorded
        on the 'behavior_done' transition; ignored on others."""
    def record_failure(self, run_id: str, error_message: str) -> None:
        """Mark a run as failed with completed_at = now()."""
    def get_latest_for_session(self, session_id: str) -> AnalysisRun | None:
        """Latest run by started_at DESC. Used to decide resume vs.
        new run vs. SessionAlreadyAnalyzedError."""
    def count_recent_partial(self, since: datetime) -> int:
        """Audit query: rows where stage NOT IN ('summary_written',
        'aggregated', 'failed') AND updated_at < since.
        Used by ship-manifest review."""
```

### `SessionReportsRepository`

```python
class SessionReportsRepository:
    def create_schema(self) -> None: ...
    def upsert(self, report: SessionReport) -> None:
        """ON CONFLICT(session_id) DO UPDATE SET
           analysis_run_id, headline, key_findings, body, updated_at.
           created_at preserved on UPDATE."""
    def get_for_session(self, session_id: str) -> SessionReport | None: ...
    def list_for_project(
        self, project_id: str, *, limit: int = 50, offset: int = 0,
    ) -> list[SessionReport]:
        """Order by created_at DESC."""
```

### `DirectivesRepository.upsert_with_identity_key`

```python
def upsert_with_identity_key(self, directive: Directive) -> None:
    """ON CONFLICT(project_id, identity_key) DO UPDATE SET
       instruction, frequency, source_sessions, updated_at.
       status, type, source_flag_type, created_at preserved on UPDATE.
       Raises ValueError if identity_key is empty."""
```

## Death tests (write FIRST)

- **DT-1.1.a** — `start_run` writes the row before any other side
  effect. Mock the rest of the pipeline; assert the row exists at
  stage='pending' even when subsequent code raises immediately.
- **DT-1.1.b** — `advance_stage` validates the enum at the repository
  layer; passing `'bogus_stage'` raises `ValueError` (mirrors GUR-100
  D1 enum guard pattern on `behavior_flags.flag_type`).
- **session_reports.upsert idempotency** — Inserting twice with same
  `session_id` and different content: first row's `created_at`
  preserved; `analysis_run_id`, `headline`, `key_findings`, `body`,
  `updated_at` updated; row count = 1.
- **directives.upsert_with_identity_key empty-key guard** — Calling
  with `identity_key=""` raises `ValueError`. Server default `""` is
  only acceptable for the column's transitional default at table
  creation; no row should ever be inserted with that value through
  the repository.
- **directives.upsert_with_identity_key UPSERT path** — Two calls
  with same `(project_id, identity_key)` and different `instruction`:
  second call's `instruction` and `updated_at` win; `status`, `type`,
  `created_at` preserved.

## Happy-path tests

- AnalysisRunsRepository: full lifecycle pending → segmented →
  behavior_done → summary_written → aggregated; `completed_at`
  populated only on terminal stage.
- SessionReportsRepository: round-trip a `SessionReport` Pydantic
  model with non-trivial `key_findings` (3 items), assert JSON
  encoding/decoding preserves order.
- DirectivesRepository: insert via `upsert_with_identity_key` then
  `get_active_conventions(project_id)` returns it.

## Pydantic models needed

These should live in `secondsight.analysis.schemas` (extend the
existing module) or a new `storage.models` module — **prefer
extending `analysis.schemas`** for consistency with GUR-100's
`BehaviorFlag` / `Directive` pattern.

```python
class AnalysisRun(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    project_id: str
    session_id: str
    stage: AnalysisRunStage  # Literal[...] or Enum
    started_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    error_message: str | None
    flags_inserted: int = Field(ge=0)


class SessionReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    project_id: str
    session_id: str
    analysis_run_id: str
    headline: str = Field(min_length=1, max_length=200)
    key_findings: list[str] = Field(max_length=5)
    body: str
    created_at: datetime
    updated_at: datetime
```

## Scar items to record

- Hardcoded `server_default=""` on `directives.identity_key` is a
  transitional value only; document in the table's docstring that
  the empty string is rejected by the repository guard.
- `analysis_runs.stage` enum validation lives at the repository
  layer (mirrors GUR-100 D1).
- Migration assumes `directives` table is empty pre-Phase 3 (G4).
  If this becomes false (early dogfooding writes), a backfill is
  required before the unique index can be enforced.
- `key_findings` Pydantic `max_length=5` matches the prompt
  constraint in `prompts/summary.py:43`. Drift requires co-modification.
