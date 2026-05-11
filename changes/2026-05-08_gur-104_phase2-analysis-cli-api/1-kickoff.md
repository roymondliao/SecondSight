# Kickoff: gur-104-phase2-analysis-cli-api

## Problem Statement

Phase 2 now produces data — GUR-103 wired the PydanticAI agent and the
trigger paths so that finished sessions yield `behavior_flags`,
`session_reports`, and `directives` rows. None of that data is
reachable from anywhere a human or downstream agent can consume it.
The dashboard (GUR-106) cannot render. The agent self-query path
(`secondsight directive --active --format json` per SD §9.3) does not
exist. Operators have no way to disable a misfired directive without
hand-editing SQLite. GUR-104 is the read- and control-surface that
turns the persisted analysis state into something a dashboard, an
agent, and a human can use, while preserving the directive lifecycle
contract pinned in `project_directive_lifecycle_contract` memory.

## Evidence

- `secondsight analyze` **already exists** at `src/secondsight/cli/analyze.py`
  (shipped under GUR-103 P2-15 as the manual trigger path). The
  GUR-104 ticket lists it as P2-16; this is a real ticket-level
  overlap. P2-16's net-new work is therefore zero unless an explicit
  delta is identified — see "Out of Scope" for how this is resolved.
- `secondsight directive` does **not** exist — `ls src/secondsight/cli/`
  shows `analyze, app, cleanup, init, serve, status, sync` plus
  `_home`. SD §9.3 names `directive --active --format json` as the
  primary agent self-query path; without it, agents cannot read the
  directives the aggregator produces.
- `src/secondsight/api/` ships only `observation.py` (sessions /
  segments per SD §10.4 line 1543–1547) and the FastAPI factory.
  None of the six analysis endpoints (`/api/analysis/summary`,
  `/sessions`, `/sessions/{id}`, `/sessions/{id}/flags`, `/trends`,
  `/aggregation`) and neither directive endpoint
  (`GET /api/directives`, `PATCH /api/directives/{id}`) exist —
  `grep -rn "/api/analysis\|/api/directives" src/` returns zero hits.
- `BehaviorFlagsRepository` (`get_session_flags`,
  `get_project_flags_by_type`, `count_by_type`) and
  `DirectivesRepository` (`get_active_conventions`, `update_status`,
  `upsert_with_identity_key`) already expose enough query surface for
  most endpoints. What is **missing** in the repository layer:
  - "List sessions that have a `session_reports` row" — for
    `GET /api/analysis/sessions`. `SessionReportsRepository` would
    need a `list_for_project(project_id, limit, cursor)` method.
  - Time-bucketed counts for `/api/analysis/trends` — currently no
    `count_by_day` / `count_by_session` aggregator exists.
- `api/observation.py` has already locked the conventions analysis
  endpoints must follow: `frozen` Pydantic models with
  `extra="forbid"`, required `project_id: str = Query(...)` per
  DC-4, ETag derived from `MAX(events.timestamp)` for listings.
  Re-using the same conventions is mandatory; inventing a parallel
  pattern would split the dashboard's mental model.
- The repo currently has an **untracked** file
  `src/secondsight/api/observation_schemas.py` (165 lines, originated
  in GUR-147 task-A3 — a separate Observation API track). It is not
  GUR-104's responsibility to merge or delete it, but GUR-104 must
  not touch sibling files that depend on its shape until the
  operator decides.
- `cli/analyze.py:212` declares `POST /api/analyze` as a
  "future endpoint" with a server-mode fallback that today silently
  becomes in-process (via the `httpx.HTTPStatusError` branch at
  line 146–165 — actually now a non-silent ERROR exit, but the
  endpoint itself is still missing). GUR-104's ticket text does NOT
  include POST /api/analyze. Decision deferred to planning gate.
- Memory `dashboard_api_contracts` records: local-only bind
  (already enforced at `server.py:60`), 5s polling with
  ETag/cursor, single-project MVP. Memory
  `project_directive_lifecycle_contract` records: PATCH = soft-disable,
  status enum, no DELETE in MVP, lives in GUR-104.
- Memory `behaviorflag_schema_contract` records: `BehaviorFlag`
  ships SD §5.5.1 vocabulary + `confidence` field. The flags
  endpoints must surface `confidence` to the dashboard or operators
  cannot triage low-confidence flags.

## Risk of Inaction

- **Phase 2 stays write-only.** GUR-103 unblocks data production
  but with no read path the data is forensic-only — operators must
  open SQLite to see what the agent wrote. The Phase 1→2→3 chain
  (memory `project_phase1_to_3_chain`) breaks at the read joint;
  GUR-106 (dashboard) cannot start.
- **Directive injection has no kill switch.** Once Phase 3 wires
  directives into agent prompt prefixes, a hallucinated or stale
  directive will fire on every subsequent agent invocation until
  somebody hand-edits the DB. Without `PATCH /api/directives/{id}`
  the recovery path is "stop the server, edit SQLite, restart" —
  unacceptable as a steady-state operational mode.
- **Agent self-query path unbuilt.** SD §9.3 specifically names
  `secondsight directive --active --format json` as the primary
  way an agent reads its current conventions. Without it, the
  Phase 3 prompt-injection design has no source-of-truth pull.
- **API contract drift risk.** Each day GUR-104 stays open, the
  dashboard team (GUR-106) builds against an imagined shape. If
  the actual shape lands differently, GUR-106 absorbs the rebase
  cost. The longer the delay, the more invented assumptions
  accrue.
- **Hidden coupling in CLI re-use.** P2-16 nominally ships the
  same `secondsight analyze` GUR-103 already shipped. Without an
  explicit "P2-16 is satisfied by GUR-103's work, no further
  change" decision in the plan, a future implementer may
  re-implement and silently regress trigger semantics.

## Scope

### Must-Have (with death conditions)

- **`secondsight directive` CLI subcommand (P2-17)** —
  `src/secondsight/cli/directive.py` + register in
  `cli/app.py`. Required modes:
    - `secondsight directive --active --format json` — emit
      active directives as a JSON array, schema identical to
      `GET /api/directives`'s response (one source of truth).
    - `secondsight directive --active` (default human format) —
      Rich table, columns: `id`, `type`, `summary`, `frequency`,
      `created_at`. Truncate `summary` at 80 chars.
    - `secondsight directive --disable <ID> --reason "..."` —
      forwards to PATCH `/api/directives/{id}` (server-mode) or
      calls `DirectivesRepository.update_status` (no-server). The
      `--reason` flag is required when disabling, mirroring the
      repo's lifecycle contract (non-None reason ⇒ DISABLED).
  Death condition: if Phase 3 (GUR-105 prompt injection) finds
  the JSON shape from `--active --format json` insufficient
  (missing fields, wrong nesting), revisit before locking the
  shape externally; the schema is a contract once an agent reads
  it.
- **`/api/directives` endpoints (P2-19)** in
  `src/secondsight/api/directives.py`:
    - `GET /api/directives` — list active directives for the
      project (delegates to `get_active_conventions`). Response
      shape mirrors the CLI JSON exactly. ETag via
      `MAX(directives.updated_at)` over the project scope.
    - `PATCH /api/directives/{id}` — soft-disable / re-activate
      via the repo's `update_status` lifecycle. Allowed
      transitions: `active → disabled` (requires `reason`),
      `disabled → active` (no `reason`). Other transitions
      (delete, expire, supersede) are out of scope. Returns the
      updated directive row. **Idempotent on the same status
      payload** (re-PATCH with current status → 200, no DB
      write).
  Death condition: if PATCH cannot invalidate a downstream
  prompt-injection cache (Phase 3's directive cache layer
  doesn't yet exist, so this is forward-coupled), the soft-
  disable is operationally useless — directives keep firing
  until restart. Demote to "PATCH writes DB, restart required to
  take effect" with a documented warning until GUR-105 ships
  cache-invalidation hooks.
- **`/api/analysis/*` endpoints (P2-18)** in
  `src/secondsight/api/analysis.py`. Six endpoints, all GET,
  all read-only, all required `project_id: str = Query(...)`,
  all `frozen + extra="forbid"`, all ETag where listing:
    - `GET /api/analysis/summary` — single object: counts of
      analyzed sessions, total flags by type, active directive
      count, last-analyzed session timestamp.
    - `GET /api/analysis/sessions` — paginated list of sessions
      that have a `session_reports` row (i.e., analysis
      completed). Drives the Analysis Dashboard's "Session
      Analysis List" (SD §10.3 line 1530).
    - `GET /api/analysis/sessions/{id}` — full per-session report
      (joins `session_reports` + `behavior_flags` for the
      session). DC-4: 404 if `(project_id, session_id)` doesn't
      match.
    - `GET /api/analysis/sessions/{id}/flags` — flags-only view
      of the same data, sorted by `created_at`. Useful for
      drill-down.
    - `GET /api/analysis/trends` — time-bucketed flag counts.
      Granularity decision deferred to planning gate (see
      User-decision points below).
    - `GET /api/analysis/aggregation` — cross-session statistics:
      flag counts per type for the project (delegates to
      `count_by_type`), plus per-flag-type session-count
      (how many distinct sessions have each flag).
  Death condition: if any of these endpoints requires a SELECT
  whose p95 exceeds 500ms with the dashboard's 5s polling cycle
  (memory `dashboard_api_contracts`) on a project of >1k
  sessions, demote that endpoint to opt-in or add a materialized
  view. Polling-driven endpoints must not become the bottleneck
  the dashboard sits behind.

### Nice-to-Have

- **`POST /api/analyze`** — close the TODO in `cli/analyze.py:212`
  by implementing the server-mode counterpart. Currently the
  CLI falls back to in-process if the endpoint is missing; a
  proper POST endpoint would let the CLI dispatch from machines
  without local repo access. **Excluded from must-have** because
  the issue text doesn't list it and the in-process fallback
  works.
- **Cursor pagination for `/api/analysis/sessions`** — the
  observation list endpoints today use `limit/offset` (per
  `observation_schemas.py` GUR-147). Match that for v1; cursor
  is an upgrade path.
- **Streaming JSON output for `secondsight directive --active`**
  for very large directive sets — load-all into memory is fine
  for v1 (typical project: <100 active directives).
- **Per-flag drill-down endpoint** —
  `GET /api/analysis/flags/{id}` exposing the source events. SD
  §10.3 implies this drill-down via the `Link 到 Observation
  Level 3` arrow, but it can be served by joining flag.event_ids
  to the existing observation endpoint client-side.

### Explicitly Out of Scope

- **`secondsight analyze` CLI re-implementation** — already
  shipped by GUR-103 P2-15 at `cli/analyze.py`. P2-16 in the
  ticket text is **satisfied by GUR-103**; no change needed.
  Recorded explicitly so a future implementer doesn't re-do it.
- **`POST /api/analyze`** — see Nice-to-Have above; only included
  if scope room allows.
- **Dashboard rendering** — GUR-106. GUR-104 ships the API; it
  does not render anything.
- **Prompt-injection cache invalidation on PATCH** — that cache
  doesn't exist yet (lives in GUR-105). PATCH ships as
  "DB-correct, runtime takes effect on restart" with an
  explicit caveat in the API docs.
- **DELETE on directives** — memory
  `project_directive_lifecycle_contract` records: no DELETE in
  MVP. PATCH soft-disable only.
- **Cross-project endpoints** — memory `dashboard_api_contracts`
  records single-project MVP. Every endpoint requires
  `project_id`. No `/api/projects` discovery endpoint.
- **Auth changes** — server is local-only bind (`127.0.0.1` per
  `server.py:60`). GUR-104 inherits this and adds nothing.
- **Untracked `observation_schemas.py`** — out of scope.
  Operator decides whether to merge under GUR-147 or delete;
  GUR-104 does not modify it.

## North Star

```yaml
metric:
  name: "first_directive_dashboard_render_to_first_directive_PATCH_disable_e2e"
  definition: |
    For a project with at least one active directive, the wall-clock
    time from "operator opens dashboard and sees the directive list"
    to "operator-issued PATCH succeeds AND a subsequent
    GET /api/directives no longer returns the disabled directive in
    the active list." Measured end-to-end against a running server
    with one project and ~10 directives.
  current: null  # endpoints don't exist yet
  target: 5  # seconds end-to-end (dashboard load < 1s; PATCH < 200ms)
  invalidation_condition: |
    The metric is wrong if the dominant cost is dashboard rendering
    (GUR-106) rather than the API. If GUR-106 measures p95
    dashboard-render >2s on the directive list, the e2e budget is
    governed by the frontend, not GUR-104; switch to the
    sub-metric "PATCH-to-list-consistency latency p95" as the
    metric this layer is responsible for.
  corruption_signature: |
    PATCH-to-list latency stays under target while disabled
    directives keep showing up in `secondsight directive --active`
    or in agent prompt injection — the soft-disable wrote the DB
    but the read path or the prompt-injection cache (Phase 3)
    didn't pick up the change. Detect by tagging each PATCH with a
    request_id and asserting the next GET reflects the new status
    in the same project scope.

sub_metrics:
  - name: "GET /api/analysis/* p95 latency under 5s polling"
    current: null
    target: 0.5  # seconds, p95
    proxy_confidence: high
    decoupling_detection: |
      Polling 5s cycle stays steady but per-call duration creeps to
      >1s under a 1k-session project — the dashboard "feels live"
      but the server is queueing. Detect via FastAPI access-log
      duration field; alert when p95 over 1-min window exceeds
      target by 2x.

  - name: "directive PATCH idempotency"
    current: null
    target: 1.0  # exactly: a re-PATCH to current status causes 0 DB writes
    proxy_confidence: high
    decoupling_detection: |
      Idempotency is binary, but corruption shows as
      `directives.updated_at` advancing on every PATCH even when
      status is unchanged. A row whose updated_at moves without a
      status change indicates a bug (we wrote when we shouldn't
      have) and is detectable by an integration test plus a
      production assertion.

  - name: "CLI directive --format json schema match with API"
    current: null
    target: 1.0  # exact equality
    proxy_confidence: high
    decoupling_detection: |
      The CLI calls the API or the repo (per --no-server). If the
      no-server path returns a slightly different shape (e.g.,
      missing a field the agent depends on), agent integration
      breaks differently in each path. Detect via a shared schema
      module and a test that asserts the two code paths emit
      byte-identical JSON for the same DB row.
```

## Stakeholders

- **Decision maker:** Project lead (board user) — locks the
  pagination strategy for `/api/analysis/sessions`, the trends
  granularity, the directive PATCH idempotency contract, and
  whether `POST /api/analyze` ships in this issue.
- **Impacted teams:**
  - GUR-106 (dashboard) — direct consumer of every analysis +
    directives endpoint. Schema changes after lock = rebase tax.
  - GUR-105 (Phase 3 directive injection) — consumer of the
    `secondsight directive --active --format json` shape and the
    `GET /api/directives` shape. Once an agent reads the shape,
    it is sticky.
  - Operators running multi-project deployments — every
    endpoint requires `project_id`; if their UX is "no
    project_id in URL bar" the v1 single-project assumption
    surfaces immediately.
- **Damage recipients:**
  - **GUR-106 implementer** — locked into whatever shape we ship.
    A trends granularity that doesn't match the dashboard's
    intended chart axis = silent rebuild.
  - **Future operator with a misfiring directive** — if PATCH's
    runtime effect is "restart the server", the operator pays
    server-restart cost on every directive disable until GUR-105
    ships cache invalidation. We need to surface this in API
    docs explicitly, not silently.
  - **API server event loop** — adds 8 new SQL-bound endpoints
    polled at 5s. A bad query plan on `/api/analysis/aggregation`
    or `/trends` could starve `/api/sessions` (the observation
    polling). First place that surfaces is dashboard "lag"
    when many flags accumulate.
  - **Agent calling `secondsight directive`** — if the
    `--no-server` path's JSON shape diverges from the API path,
    the same agent works against a server but fails against
    a no-server installation. Failure mode is silent (wrong
    field) until the agent's downstream consumer breaks.

## User-decision points (planning gate)

These are the shape-defining choices the planning gate must lock.
Each has multiple valid answers; the user's pick will be encoded
in `2-plan.md`.

1. **Trends granularity**
   - **(A) Per-day**: `GET /api/analysis/trends` returns
     `[{date, flag_type, count}, ...]` for the last N days
     (default 30). Charts naturally on a date axis.
   - **(B) Per-session**: returns
     `[{session_id, analyzed_at, flag_type, count}, ...]` for the
     last N sessions (default 50). Charts on a session-index
     axis (matches SD §10.3 line 1528 "最近 N 個 sessions 的
     behavior flag 數量趨勢").
   - **(C) Both**: query param `?bucket=day|session` selects.
   - Trade-off: (B) matches SD literal but (A) matches operator
     intuition for trends; (C) is most flexible at 2x the SQL
     surface to test. SD §10.3 line 1528 leans (B).

2. **Pagination for `/api/analysis/sessions`**
   - **(A) `limit/offset`** — match `observation_schemas.py`
     (GUR-147) for consistency.
   - **(B) Cursor (last_seen_id)** — matches memory
     `dashboard_api_contracts` "ETag/cursor".
   - Trade-off: (A) simpler and matches existing precedent; (B)
     better with high-churn data but the analysis surface is
     write-once-per-session (low churn). Default recommendation:
     (A) for v1, leave (B) as nice-to-have.

3. **Directive PATCH allowed transitions**
   - **(A) `active → disabled` only** — strictly soft-disable;
     re-enable requires re-running the aggregator.
   - **(B) `active ↔ disabled`** — both directions, no `reason`
     on re-enable.
   - Trade-off: (A) is more conservative (operator never
     re-enables a stale directive accidentally); (B) is more
     useful if the operator disabled the wrong one and wants to
     undo. Memory `project_directive_lifecycle_contract` allows
     re-activation conceptually; SD does not specify either way.

4. **`POST /api/analyze` inclusion**
   - **(A) In scope** — close the TODO at `cli/analyze.py:212`;
     CLI no longer falls back silently to in-process when run
     against a remote server.
   - **(B) Out of scope** — defer to a follow-up issue. Current
     in-process fallback works.
   - Trade-off: (A) is +~50 lines, +1 endpoint; (B) keeps the
     issue small and the TODO visible. Recommendation: (B)
     unless scope room is comfortable.
