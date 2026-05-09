# Overview: GUR-104 Phase 2 Analysis CLI + API

## Goal

Expose the analysis state that GUR-103 produces (behavior_flags,
session_reports, directives) via a 6-endpoint analysis API,
2-endpoint directive API, and a `secondsight directive` CLI that
shares its JSON shape with the API by class import.

## Architecture

Three new modules + one repo method, no schema changes:

1. **`api/analysis.py`** — 6 GETs over the existing repos. All
   endpoints follow `api/observation.py`'s convention: frozen
   Pydantic + extra=forbid, required `project_id: str = Query(...)`,
   weak ETag from `MAX(updated_at)` in scope.
2. **`api/directives.py`** — `GET /api/directives` and
   idempotency-aware `PATCH /api/directives/{id}` over
   `DirectivesRepository`. Bidirectional `active ↔ disabled`
   transitions.
3. **`cli/directive.py`** — Typer app importing the Pydantic models
   from `api/directives.py` so `--format json` is byte-identical to
   the API. `--no-server` falls back via the same precedent as
   `cli/analyze.py` (ConnectError → in-process; HTTPStatusError →
   loud exit, no silent fallback).

## Tech Stack

- FastAPI (already present) — routers + ETag pattern
- SQLAlchemy Core (already present) — repository queries
- Pydantic v2 with `ConfigDict(frozen=True, extra="forbid")`
- Typer + Rich (already present) for CLI
- httpx (already present) for CLI server-mode dispatch
- pytest with FastAPI `TestClient` (already present)

## Key Decisions

- **D1.** Schema-as-contract — CLI imports Pydantic models from
  `api/directives.py`. Eliminates drift by construction (DC-4).
- **D2.** `unknown` is a first-class outcome state in PATCH timeout
  / DB-error scenarios; never silently coerced to success/failure.
- **D3.** Trends granularity is **per-session** (locked decision 1).
- **D4.** Pagination is **limit/offset** (locked decision 2),
  matching `observation_schemas.py` precedent.
- **D5.** PATCH is **bidirectional `active ↔ disabled`** (locked
  decision 3); status enum gates other transitions to analyzer-only.
- **D6.** **`POST /api/analyze` is out of scope** (locked
  decision 4). The TODO at `cli/analyze.py:212` stays.
- **D7.** Trends LIMIT is applied to the **session set first, then
  JOIN flags** (DC-7 defense).
- **D8.** PATCH no-op idempotency: `current.status == request.status`
  → return current row, do NOT advance `updated_at` (DC-2).
- **D9.** ETag is **weak** (`W/"…"`) — body equivalence not byte
  identity. Matches observation API.
- **D10.** OpenAPI description on PATCH explicitly documents the
  Phase 3 cache caveat (degradation acceptance scenario).

## Death Cases Summary

Top 3 most dangerous silent-failure paths (full list in
`acceptance.yaml`):

1. **DC-1: Cross-project leak** — request includes `project_id=A`
   but path id belongs to project B; endpoint returns B's data.
   Closed by `(project_id, x.project_id)` check before returning,
   404 on mismatch.
2. **DC-2: PATCH no-op advances `updated_at`** — operator PATCHes
   the current state, server writes a dummy UPDATE and pretends
   nothing happened. Closed by read-then-conditionally-update
   under transaction, no-op skips the UPDATE entirely.
3. **DC-7: Trends LIMIT off-by-window** — `LIMIT 10` against a
   joined `behavior_flags` table returns 10 flag rows, not 10
   sessions. Closed by SUBQUERY: SELECT session_ids ORDER BY
   analyzed_at DESC LIMIT N, then JOIN flags.

## File Map

- **NEW** `src/secondsight/api/analysis.py` (~350 LOC)
- **NEW** `src/secondsight/api/directives.py` (~200 LOC)
- **NEW** `src/secondsight/cli/directive.py` (~200 LOC)
- **MODIFIED** `src/secondsight/api/server.py` — register routers
- **MODIFIED** `src/secondsight/cli/app.py` — `add_typer(directive)`
- **MODIFIED** `src/secondsight/storage/behavior_flags_repository.py`
  — add `count_per_session_for_project` (~30 LOC)
- **NEW** `tests/api/test_analysis.py` (~30 cases)
- **NEW** `tests/api/test_directives.py` (~20 cases)
- **NEW** `tests/cli/test_directive.py` (~15 cases)
- **NEW** `tests/storage/test_behavior_flags_repository_count_per_session.py` (~8 cases)
