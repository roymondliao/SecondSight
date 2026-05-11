# Planning Pre-thinking — GUR-104 Analysis CLI + API

Surface information assumptions and gaps before writing `2-plan.md`.
The 4 macro decisions from `1-kickoff.md` are already locked by the
research → planning gate (confirmation `92aeed81`, accepted by
local-board 2026-05-08T14:56:38Z):

1. `/api/analysis/trends` granularity = **per-session**
2. `/api/analysis/sessions` pagination = **limit/offset**
3. Directive PATCH transitions = **bidirectional `active ↔ disabled`**
4. `POST /api/analyze` = **out of scope**

The remaining unknowns are micro-gaps surfaced below. Each is given
a proposed default; if any is wrong, the planning confirmation card
should reject with that specific gap named.

## To write this plan, I am assuming

### A. Architecture (from research + locked decisions)

- **A1.** Three new modules + zero schema migrations:
  - `src/secondsight/api/analysis.py` — 6 GET endpoints + Pydantic
    response shapes. ~350 LOC.
  - `src/secondsight/api/directives.py` — `GET /api/directives`,
    `PATCH /api/directives/{id}` + Pydantic shapes. ~200 LOC.
  - `src/secondsight/cli/directive.py` — Typer subcommand with
    `--active`, `--format json`, `--disable ID --reason`,
    `--enable ID`, `--no-server`. ~200 LOC.
- **A2.** No new repository. The existing query surface is
  sufficient with one addition:
  - `BehaviorFlagsRepository.count_per_session_for_project(
    project_id)` → `dict[session_id, dict[flag_type, count]]`
    or a flat `[(session_id, flag_type, count), ...]` list. This
    is the single SQL query backing `/api/analysis/trends` (per-
    session) and the cross-session piece of `/api/analysis/aggregation`.
    Single new method, single GROUP BY query.
- **A3.** Schema-as-contract: the Pydantic response shapes for
  `GET /api/directives/*` are imported and re-used by `cli/directive.py`
  so the `--format json` output is byte-identical to the API.
  Implements North Star sub-metric "CLI directive --format json
  schema match with API" target = 1.0 by construction, not by test.
- **A4.** ETag derivation:
  - List endpoints with timestamp signal:
    `MAX(<table>.updated_at)` over the project scope. Lift the
    pattern from `api/observation.py` (which uses
    `MAX(events.timestamp)`). Hash via `hashlib.sha256` of the
    timestamp's ISO-8601 string + cardinality count, return as
    `W/"<hex>"` (weak ETag — body is JSON, ordering may vary
    across SQL engines).
  - The `summary` endpoint composes its ETag from `MAX(updated_at)`
    over `behavior_flags + directives + session_reports + analysis_runs`
    in the project scope; any cross-table change invalidates.
- **A5.** PATCH idempotency. Read-then-write under a transaction:
  - Fetch current row by `(project_id, directive_id)`. 404 if
    missing or project mismatch (DC-4 enforcement).
  - If `current.status == request.status` and (status==disabled →
    `current.disabled_reason == request.reason`), return 200 with
    the existing row, **no UPDATE**, `updated_at` does not advance.
  - Otherwise call `DirectivesRepository.update_status(id, new,
    reason)`, return the new row.
- **A6.** Reuse of `api/observation.py` conventions, mandatory:
  - `ConfigDict(frozen=True, extra="forbid")` on every response model
  - Required `project_id: str = Query(...)` per DC-4 (no implicit
    "first project" fallback)
  - Per-endpoint string-id safety via
    `secondsight.api._id_safety.is_safe_id`
  - 404 (not 400) for `(project_id, x)` mismatches
- **A7.** CLI `--no-server` path uses the same Pydantic models for
  encoding (model_dump_json) so the schema is one source. Server-
  mode goes through `httpx` to `http://127.0.0.1:8420`; on
  ConnectError, fall back **only when `--no-server` is implied**;
  on HTTPStatusError, do NOT silently fall back (mirror the
  existing `cli/analyze.py:146-165` discipline — server up but
  endpoint failed should be loud).
- **A8.** Routes registered in `api/server.py` lifespan via
  `app.include_router(analysis_router); app.include_router(
  directives_router)`. CLI command registered in `cli/app.py`
  via `app.add_typer(directive.app, name="directive")`. Pattern
  matches existing `analyze` registration.

### B. Operational defaults (from memory)

- **B1.** Server is local-only bind (`127.0.0.1`, `server.py:60`);
  GUR-104 inherits, no auth changes.
- **B2.** Dashboard polling cadence: 5s (memory
  `dashboard_api_contracts`). Endpoint p95 budget: 0.5s (north
  star sub-metric).
- **B3.** Single-project MVP (memory `dashboard_api_contracts`).
  Every endpoint requires `project_id`; no `/api/projects`
  discovery.
- **B4.** BehaviorFlag schema includes `confidence` (memory
  `behaviorflag_schema_contract`); the flags endpoints surface
  it. Flagging it as missing is a bug in the response shape.
- **B5.** Directive PATCH writes the DB; runtime cache invalidation
  is GUR-105's responsibility. API docs MUST surface this caveat
  explicitly so an operator knows a server restart may be
  required for the disabled directive to stop firing in Phase 3
  prompt injection (when that ships).

### C. Testing strategy

- **C1.** Per-task: death tests first (failing tests committed
  before implementation), then unit tests, then minimal impl.
- **C2.** Endpoint death tests prefer FastAPI `TestClient` over
  raw httpx: same import surface as observation tests,
  `tests/api/test_observation.py` is the precedent.
- **C3.** Repo death tests run against an in-memory SQLite
  (existing `_db_engine_in_memory` fixture pattern from
  `tests/storage/test_behavior_flags_repository.py`).

## Gaps I cannot resolve from Research

(All proposed-default; flag in confirmation card if you disagree.)

- **G1: ETag weak vs strong.** Observation API uses weak ETag
  (`W/"…"`). Proposed default: **same — weak**. Strong ETag
  requires byte-stable JSON serialization which Pydantic does
  not guarantee across version bumps; weak is correct
  semantically (representations are "equivalent" not "byte-
  identical").
  Trade-off if wrong: dashboards using `If-Match` for
  optimistic-concurrency on PATCH would silently get
  precondition-failed mismatches. PATCH does not require
  `If-Match` in the locked decisions (idempotent on no-op),
  so weak is correct.

- **G2: `summary` endpoint exact field set.** The kickoff lists
  "counts of analyzed sessions, total flags by type, active
  directive count, last-analyzed session timestamp" but the SD
  doesn't enumerate fields. Proposed default — emit exactly:
  ```json
  {
    "project_id": "...",
    "analyzed_session_count": 0,
    "flag_counts_by_type": {"missing_intent": 0, ...},
    "active_directive_count": 0,
    "last_analyzed_at": "2026-05-08T..." | null,
    "as_of": "2026-05-08T...",
    "etag": "W/\"...\""
  }
  ```
  Trade-off if wrong: dashboard's "Project-level Summary" card
  (SD §10.3 line 1527) needs more than this (e.g.,
  improvement_rate). The kickoff's nice-to-have list flags
  improvement-rate as Phase 3+; defer.

- **G3: Trends time window.** Per-session is locked, but
  "last N sessions" is open. Proposed default — `?limit=50` query
  param, max 200. SD §10.3 line 1528 says "最近 N 個 sessions";
  50 matches the SessionReports default at
  `session_reports_repository.py:85`.
  Trade-off if wrong: dashboard wants 100 by default. Easy to
  change at planning gate; locking 50 now keeps the contract
  small.

- **G4: PATCH error code for invalid transition.** If a user
  PATCHes `status=expired` (analyzer-only), the request must be
  rejected. Proposed default: **400** (validation error) with
  body `{"error": "invalid_transition", "allowed":
  ["active", "disabled"]}`. The DirectiveStatus enum has 5
  values but only `{active, disabled}` are user-PATCHable per
  `directives_repository.py:60` docstring.
  Trade-off if wrong: 422 is more REST-conventional for
  validation errors; FastAPI returns 422 for Pydantic
  validation failures by default. Decision: 400 with explicit
  body; 422 from FastAPI's default Pydantic guard handles
  shape errors (missing field, wrong type) and is fine.

- **G5: `--enable ID` CLI semantics.** The kickoff lists
  `--disable ID --reason`; doesn't list explicit `--enable`.
  Proposed default — add `--enable ID` (no `--reason` flag) for
  symmetry with bidirectional PATCH. Without it, the only way to
  re-enable a directive via CLI is `--disable ID --reason ""`
  which is wrong (empty reason violates the lifecycle contract).
  Trade-off if wrong: CLI surface grows by one flag. Symmetry
  is worth it.

## Uncertainties (treat as gaps)

- **U1: GUR-106 dashboard polling shape.** Memory says 5s with
  ETag/cursor; we don't have a code reference. If GUR-106
  doesn't actually send `If-None-Match` headers, the ETag work
  is overhead with no payoff. Proposed default — implement ETag
  anyway; cost is ~10 lines per endpoint; benefit is real
  iff/when GUR-106 wires it.
  Resolution: documented assumption — "ETag implemented in
  case GUR-106 sends `If-None-Match`; if GUR-106 ships without
  it, ETag is dead code and can be removed in a follow-up."

- **U2: CLI `--active --format json` shape for disabled fields.**
  When `--active` filters status=active rows only, the response
  technically never has `disabled_at` / `disabled_reason`
  populated. Should the response model omit these fields, or
  return them as `null`? Proposed default — **always include,
  always null when active** — matches the API's GET shape
  exactly (Pydantic ConfigDict with default values surfaces
  None). Schema-as-contract requires the same shape on both
  sides.
  Resolution: documented assumption — "GET /api/directives
  always includes nullable lifecycle fields; CLI mirrors."

## Summary

3 hard gaps + 2 uncertainties. All have proposed defaults that
are individually small. Given the macro decisions are locked, the
planning gate proceeds with these defaults documented as accepted
gaps. The confirmation card will list them so the human can
override any specific one before implementation begins.
