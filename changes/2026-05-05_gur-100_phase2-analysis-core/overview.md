# Overview: GUR-100 Phase 2 Analysis Core

## Goal

Ship the **data foundation** for the analysis pipeline: enum +
Pydantic contracts, two SQLAlchemy tables + repositories, a read-side
event segmenter, and pure-function supplementary metrics. No HTTP/CLI,
no LLM calls, no aggregation logic — those ship in GUR-101 and GUR-104.

## Architecture

A new package `src/secondsight/analysis/` holds analysis-only modules
(`schemas.py`, `segmenter.py`, `metrics.py`). New SQLAlchemy tables
(`behavior_flags`, `directives`) and their repositories live under
`src/secondsight/storage/` to keep DB schema co-located with the
existing `events_table.py` / `events_repository.py`. The segmenter is
a pure read-side **assembler** — events arrive pre-segmented from
`SessionTracker.bind()`, so the segmenter groups by `segment_index`
and pairs `tool_use_start`/`end` into `ToolUseSpan` instances.

## Tech Stack

- SQLAlchemy Core (existing) — declarative tables, idempotent
  `metadata.create_all(checkfirst=True)`.
- Pydantic v2 (existing) — `BehaviorFlag`, `Directive`, `SegmentData`,
  `ToolUseSpan` models with `Enum` / `Literal` validators.
- pytest (existing) — unit tests + adversarial fixtures.

## Key Decisions

- **D1 — Enum validation = Pydantic + repository defensive guard**;
  no DB CHECK constraints. Mirrors the existing `events.event_type`
  convention. `model_construct()` bypass is closed by re-validating
  at `repository.insert()`.
- **D2 — One PR with 5 internal tasks** (task-1 scaffold/schemas,
  task-2 behavior_flags table+repo, task-3 directives table+repo,
  task-4 segmenter, task-5 metrics). Tasks 2–5 depend only on task-1.
- **D3 — Same-PR SD update is a ship-manifest gate.** Merge blocked
  unless `git diff` shows §5.5.2 (`confidence`) and §7.4
  (`disabled_at`, `disabled_reason`) edits.
- **Wake-context P2-1..P2-4 → in-repo task-1..task-5** (renumbered to
  factor out `analysis/schemas.py` as a contract anchor).
- **Single Pydantic model** (`BehaviorFlag`, `Directive`, `SegmentData`)
  serves both DB persistence and LLM-output validation.
- **Segmenter is an assembler, not a re-segmenter.** Module docstring
  states this to prevent re-implementation drift.
- **Orphan tool-use is NEVER silently dropped.** `tool_use_start`
  without matching `end` → `ToolUseSpan(success=None,
  duration_ms=None)`. Symmetric for orphan `end`.

## Death Cases Summary

1. **Free-text `flag_type` drift via Pydantic bypass** — `model_construct()`
   skips validation; without a repository defensive guard the DB
   silently accepts an invented enum value. Closed by repository
   `insert()` re-validating against `BehaviorFlagType`.
2. **Soft-disable forgotten on directive lifecycle** — without
   `disabled_at` / `disabled_reason` columns at table-creation time,
   a future PATCH endpoint will write `status='disabled'` with no
   audit trail. Closed by shipping the columns now + repository
   `update_status()` requiring a non-None `reason` for `→disabled`.
3. **Segmenter drops orphan `tool_use_start`** — server crashed
   mid-tool; naive segmenter omits the orphan from the segment;
   LLM analyzes a session missing a real action. Closed by
   `ToolUseSpan(success=None)` for unpaired starts.

## File Map

### New (production)

- `src/secondsight/analysis/__init__.py`
- `src/secondsight/analysis/schemas.py`
- `src/secondsight/analysis/segmenter.py`
- `src/secondsight/analysis/metrics.py`
- `src/secondsight/storage/behavior_flags_table.py`
- `src/secondsight/storage/behavior_flags_repository.py`
- `src/secondsight/storage/directives_table.py`
- `src/secondsight/storage/directives_repository.py`

### New (tests)

- `tests/analysis/__init__.py`
- `tests/analysis/test_schemas.py`
- `tests/analysis/test_segmenter.py`
- `tests/analysis/test_metrics.py`
- `tests/storage/test_behavior_flags_repository.py`
- `tests/storage/test_directives_repository.py`

### Modified

- `docs/system_design.md` — §5.5.2 add `confidence` field,
  §7.4 add `disabled_at` + `disabled_reason` columns. **Required
  by D3 ship gate.**

## Out of Scope

- HTTP/REST/CLI surface (GUR-104).
- LLM analysis prompts (GUR-101).
- Cross-session aggregation / convention generation (GUR-101).
- Alembic migrations (project decision: no Alembic in MVP).
- Dashboard UI (GUR-106).
- `read_project_file` analysis tool.
- Span-splitting per SD §5.3.3 (GUR-101).
- Concurrent-writer mechanics, version columns.
- Expiry-checking logic on `expires_at` (GUR-101).
