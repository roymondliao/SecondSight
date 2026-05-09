# Task 3: api/analysis.py — 6 GET endpoints

## Context

Read: `overview.md`. This task ships the analysis read-side: 6
endpoints over the existing repos plus the new
`count_per_session_for_project` from task-1. Endpoint conventions
mirror `api/observation.py` exactly. ETag is weak; on listings, the
LIMIT must be applied to the right scope (DC-7); by-id endpoints
must 404 (not 200+empty) on missing rows (DC-6); cross-project leaks
must return 404 (DC-1).

Existing surface to study:
- `src/secondsight/api/observation.py` — all six endpoints follow the
  same ETag + Query patterns as the existing 4 observation endpoints.
- `src/secondsight/storage/session_reports_repository.py:81` —
  `list_for_project(project_id, limit, offset)` — backs `/sessions`.
- `src/secondsight/storage/session_reports_repository.py:60..79` —
  `get_by_session_id` — backs `/sessions/{id}`.
- `src/secondsight/storage/behavior_flags_repository.py:88` —
  `get_session_flags` — backs `/sessions/{id}/flags`.
- `src/secondsight/storage/behavior_flags_repository.py:113` —
  `count_by_type` — backs `aggregation.flag_counts_by_type`.
- task-1 — `count_per_session_for_project` — backs `/trends` and
  `aggregation.session_counts_by_type`.
- `src/secondsight/storage/directives_repository.py:106` —
  `get_active_conventions` (count of active directives for `summary`).

## Files

- Create: `src/secondsight/api/analysis.py`
- Test: `tests/api/test_analysis.py`

## Death Test Requirements

Write these tests **before** the implementation:

- **DT-3.1** (DC-1 cross-project session leak): Insert session S in
  project A with `session_reports`. GET
  `/api/analysis/sessions/S?project_id=B`. Assert 404.
- **DT-3.2** (DC-6 missing report): Insert session S with events
  but NO `session_reports` row. GET
  `/api/analysis/sessions/S?project_id=A`. Assert 404 with body
  containing "session not analyzed".
- **DT-3.3** (DC-7 trends LIMIT scope): Same as DT-1.1 but at the
  endpoint level. 50 sessions × 5 flags. GET
  `/api/analysis/trends?project_id=P&limit=10`. Assert
  `len(buckets) == 10`.
- **DT-3.4** (DC-3 ETag mutates): GET `/api/analysis/summary?
  project_id=P` → ETag E. Insert a behavior_flag. GET again.
  Assert new ETag != E.
- **DT-3.5** (DC-3 negative): GET → ETag E. Make NO writes. GET with
  `If-None-Match: E`. Assert 304 with empty body.
- **DT-3.6** (pagination boundary): 75 sessions; GET sessions with
  `limit=50&offset=0` → 50 items, `next_offset=50`. GET
  `limit=50&offset=50` → 25 items, `next_offset=null`.
- **DT-3.7** (BehaviorFlag confidence field): Assert
  `BehaviorFlagOut` schema includes `confidence: Literal["high",
  "medium", "low"]`. Inspect Pydantic JSON schema; field absence
  fails. Memory contract.
- **DT-3.8** (zero-flag bucket): Insert a session with a
  `session_reports` row but zero flags. GET trends; assert that
  session appears with empty `counts_by_type` (or all zeros, per
  task-1 convention).
- **HP-3.1** (summary): Fixture with 5 sessions, 12 flags, 3 active
  directives. Assert all summary fields exact.
- **HP-3.2** (sessions/{id} join): Insert session with report and 3
  flags. GET; assert response.flags has 3 items in created_at ASC.
- **HP-3.3** (aggregation): Assert `aggregation.flag_counts_by_type`
  matches `count_by_type` for the fixture; assert
  `aggregation.session_counts_by_type[type] == distinct sessions
  with that flag_type`.
- **HP-3.4** (ETag round-trip 304): GET → E; GET with
  `If-None-Match: E` → 304.

## Implementation Steps

- [ ] Step 1: Write all 12 tests; commit failing.
- [ ] Step 2: Run tests — verify all fail (routes not registered).
- [ ] Step 3: Define Pydantic shapes — all `frozen + extra="forbid"`:
  - `BehaviorFlagOut` (mirrors `analysis.schemas.BehaviorFlag`,
    confidence Literal)
  - `AnalysisSummary` (project_id, analyzed_session_count,
    flag_counts_by_type, active_directive_count, last_analyzed_at,
    as_of)
  - `SessionAnalysisItem` (session_id, analyzed_at, headline,
    flag_count, key_findings)
  - `ListSessionsResponse` (project_id, items, limit, offset,
    next_offset)
  - `SessionAnalysisDetail` (project_id, session_id, headline, body,
    key_findings, analyzed_at, flags: list[BehaviorFlagOut])
  - `TrendsBucket` (session_id, analyzed_at, counts_by_type:
    `dict[str, int]`)
  - `TrendsResponse` (project_id, buckets)
  - `AggregationResponse` (project_id, flag_counts_by_type,
    session_counts_by_type)
- [ ] Step 4: Implement `compute_analysis_etag(project_id, db_engine,
  scope: list[str])` helper. `scope` is the table set; helper does
  one SELECT MAX per table, hashes the joined timestamps + total
  row counts.
- [ ] Step 5: Implement the 6 routes with required `project_id`
  Query, `is_safe_id` validation on path params, ETag on listings/
  summary, 404 on by-id miss + cross-project mismatch.
- [ ] Step 6: Trends endpoint specifically — call task-1's
  `count_per_session_for_project(project_id, limit=request.limit)`.
  Map result to `TrendsBucket` list. Default `limit=50`, max 200.
- [ ] Step 7: Aggregation endpoint — `flag_counts_by_type` from
  `count_by_type`, `session_counts_by_type` from a separate SELECT
  COUNT(DISTINCT session_id) GROUP BY flag_type.
- [ ] Step 8: Export `analysis_router` for task-5.
- [ ] Step 9: Run tests — all pass.
- [ ] Step 10: Scar report.
- [ ] Step 11: Commit `GUR-104 task-3: api/analysis.py 6 endpoints`.

## Expected Scar Report Items

- Summary endpoint hits 4 tables (behavior_flags + directives +
  session_reports + analysis_runs). Forgetting one in the ETag
  scope = silent stale-cache bug. Code-review checklist: every
  read source must be in the ETag scope.
- `is_safe_id` belongs on every path param; observation.py is the
  precedent. Skip = 422-instead-of-400 surface bug.
- `BehaviorFlagOut.event_ids: list[str]` — event_ids in DB are
  serialized as JSON. Decode in the row-mapper, not in the
  response model.
- Empty list responses must be 200, NOT 404. Only by-id endpoints
  404 on missing.
- The `confidence` field on BehaviorFlag is mandatory (memory
  contract). If you copy from a stale snapshot of the schema, you
  miss it. DT-3.7 catches.

## Acceptance Criteria

Covers `acceptance.yaml`:
- "Silent failure - cross-project session leak" (DC-1)
- "Silent failure - 200 with empty body instead of 404" (DC-6)
- "Silent failure - trends LIMIT applied to flags table" (DC-7)
- "Silent failure - stale ETag returns 304" (DC-3)
- "Success - GET /api/analysis/summary returns counts with ETag"
- "Success - GET /api/analysis/sessions paginated"
- "Success - GET /api/analysis/trends respects per-session limit"
