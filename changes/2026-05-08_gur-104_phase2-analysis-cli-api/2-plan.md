# Plan: GUR-104 Phase 2 Analysis CLI + API

Author: Tianqi
Reference docs: `1-kickoff.md`, `problem-autopsy.md`, `2-pre-thinking.md`
SD references: §9.2, §9.3, §10.4

## STEP 0 commitments (samsara — recorded for audit)

1. **Most-wanted shortcut REJECTED**: "Build the CLI by shelling out
   to a separate API call layer; let the CLI's `--no-server` use
   ad-hoc dict construction." Rejected because schema drift between
   CLI and API is a North Star sub-metric corruption signature.
   The CLI imports the API's Pydantic models so divergence is
   structurally impossible.

2. **This implementation MUST NOT ship when**:
   - PATCH `/api/directives/{id}` advances `updated_at` on a no-op
     re-PATCH (DC-2)
   - Any endpoint returns rows for `(project_id, x)` where `x.project_id
     != project_id` (DC-1, cross-project leak)
   - ETag is computed without including `MAX(updated_at)` over the
     proper scope and changes silently lag (DC-3)
   - CLI `--no-server` JSON differs by even one byte from API JSON
     for the same DB row (DC-4)
   - `secondsight directive --active` returns disabled directives
     because the SQL `WHERE status = 'active'` was lost in a refactor
     (DC-5)
   - `GET /api/analysis/sessions/{id}` returns 200 with empty payload
     instead of 404 when the session has no `session_reports` row
     (DC-6, "appears to succeed but didn't")
   - `GET /api/analysis/trends` returns the wrong session list
     because the LIMIT is applied before the per-session GROUP BY
     (DC-7, off-by-window pagination death)
   - `secondsight analyze` (already shipped from GUR-103) is
     touched in a way that regresses its trigger semantics

3. **Silent failure surface this PR closes**:
   - Cross-project data leak via missing `project_id` filter (DC-1)
   - PATCH no-op write that pretends to be a state change (DC-2)
   - Stale ETag returning 304 after data changed (DC-3)
   - CLI–API schema drift (DC-4)
   - Active filter accidentally dropped in a refactor (DC-5)
   - "Empty success" instead of 404 (DC-6)
   - Pagination-then-aggregation off-by-window (DC-7)
   - Re-enable that loses `disabled_reason` history (DC-8 — soft
     constraint)

4. **What lives one year from now**:
   - The Pydantic shapes in `api/analysis.py` and `api/directives.py`
     — sticky once GUR-105 + GUR-106 + agent self-query path read
     them. Adding a field is OK; renaming or removing is breaking.
   - `BehaviorFlagsRepository.count_per_session_for_project` —
     becomes the SQL surface every per-session aggregation builds
     on. Most replaceable thing in this PR is the CLI module
     (`cli/directive.py`) — entirely a UX layer over the API.

## Tech Spec

### Module layout

```
src/secondsight/
├── api/
│   ├── analysis.py       # NEW — 6 GET endpoints, shapes, router
│   ├── directives.py     # NEW — GET + PATCH endpoints, shapes, router
│   └── server.py         # MODIFIED — include both routers in lifespan
├── cli/
│   ├── directive.py      # NEW — Typer subcommand
│   └── app.py            # MODIFIED — add_typer(directive)
└── storage/
    └── behavior_flags_repository.py  # MODIFIED — +1 method
```

### Interfaces (with `unknown` output state)

#### `BehaviorFlagsRepository.count_per_session_for_project`

```python
def count_per_session_for_project(
    self,
    project_id: str,
    *,
    limit: int = 50,
) -> list[SessionFlagBreakdown]:
    """For each of the most-recent `limit` analyzed sessions in the
    project, return per-flag-type counts.

    Returns:
        list of SessionFlagBreakdown ordered by session.last_event_at DESC.
        Empty list if no sessions have flags (success state, not unknown).

    The "most-recent" set is bounded by the SAME-shape SUBQUERY used by
    GET /api/analysis/sessions: sessions that have a session_reports
    row, ordered by session_reports.created_at DESC, LIMIT first, then
    JOIN behavior_flags. This is DC-7's defense.

    States:
        success — list returned (possibly empty)
        failure — DB error, raised
        unknown — N/A (no async, no network)
    """
```

#### `GET /api/analysis/summary`

Request: `?project_id=<str>` required.

Response (success):
```json
{
  "project_id": "<str>",
  "analyzed_session_count": <int >= 0>,
  "flag_counts_by_type": {"<flag_type>": <int >= 0>, ...},
  "active_directive_count": <int >= 0>,
  "last_analyzed_at": "<iso8601>" | null,
  "as_of": "<iso8601>"
}
```

States:
- `success` — counts retrieved
- `failure` — DB error → 500
- `unknown` — N/A

ETag: `W/"<sha256(MAX(updated_at) over flags+directives+reports+runs)>"`. On `If-None-Match` match → 304.

#### `GET /api/analysis/sessions`

Request: `?project_id=<str>&limit=50&offset=0`. Limit 1..200.

Response:
```json
{
  "project_id": "<str>",
  "items": [
    {
      "session_id": "<str>",
      "analyzed_at": "<iso8601>",
      "headline": "<str>",
      "flag_count": <int >= 0>,
      "key_findings": ["<str>", ...]
    }, ...
  ],
  "limit": 50, "offset": 0, "next_offset": 50 | null
}
```

#### `GET /api/analysis/sessions/{session_id}`

Request: path `session_id`, query `?project_id=<str>`.

Response (success): full report.
```json
{
  "project_id": "<str>",
  "session_id": "<str>",
  "headline": "<str>",
  "body": "<str>",
  "key_findings": ["<str>", ...],
  "analyzed_at": "<iso8601>",
  "flags": [<BehaviorFlagOut>, ...]
}
```

States:
- `success` — report exists, returned
- `404` — `session_reports` row absent OR `(project_id, session_id)` mismatch (DC-1, DC-6)
- `failure` — DB error → 500

#### `GET /api/analysis/sessions/{session_id}/flags`

Request: same as above.

Response: `[<BehaviorFlagOut>, ...]` (just the flags, sorted by `created_at` ASC).

#### `GET /api/analysis/trends`

Request: `?project_id=<str>&limit=50` (max 200, min 1).

Response:
```json
{
  "project_id": "<str>",
  "buckets": [
    {
      "session_id": "<str>",
      "analyzed_at": "<iso8601>",
      "counts_by_type": {"<flag_type>": <int>, ...}
    }, ...
  ]
}
```

Order: most recent first. Sessions with `0` flags are still included
(the dashboard's chart needs zero-points; absence is informative).

#### `GET /api/analysis/aggregation`

Request: `?project_id=<str>`.

Response:
```json
{
  "project_id": "<str>",
  "flag_counts_by_type": {"<flag_type>": <int>, ...},
  "session_counts_by_type": {"<flag_type>": <distinct sessions>, ...}
}
```

#### `GET /api/directives`

Request: `?project_id=<str>&active=true` (default `true`).

Response: `[<DirectiveOut>, ...]` ordered by `frequency DESC NULLS LAST`.
ETag: `W/"<sha256(MAX(updated_at) over scoped directives)>"`.

#### `PATCH /api/directives/{directive_id}`

Request: path `directive_id`, query `?project_id=<str>`, body
```json
{"status": "disabled", "reason": "<str>"}
```
or
```json
{"status": "active"}  // reason MUST be absent for active
```

Response: updated `<DirectiveOut>`.

States:
- `success` — status mutated, `updated_at` advanced; OR no-op (current
  state matches request, `updated_at` does NOT advance, return 200
  with current row). DC-2.
- `400` — invalid status (not in `{active, disabled}`), or status mismatched reason rules.
- `404` — `(project_id, directive_id)` mismatch.
- `failure` — DB error → 500.

#### `secondsight directive` CLI

```
secondsight directive --active [--format json|table] [--project P] [--no-server]
secondsight directive --disable ID --reason "..." [--project P] [--no-server]
secondsight directive --enable ID [--project P] [--no-server]
```

`--format json` output is `model_dump_json()` of the same Pydantic
model returned by `GET /api/directives`. Schema-as-contract.

### Death cases

| ID | Trigger | Lie | Truth | Detector |
|----|---------|-----|-------|----------|
| **DC-1** | Request includes `project_id=A`, path `session_id` belongs to project `B` | 200 with project B's data | Cross-project leak | Test `tests/api/test_analysis.py::test_cross_project_session_returns_404` |
| **DC-2** | PATCH `status=active` on a directive whose status is already `active` | "Update succeeded — `updated_at` advanced" | We wrote nothing but lied about it | Test asserts `updated_at` NOT advanced on no-op |
| **DC-3** | ETag returned for a project; flag added; ETag not refreshed (cache or wrong scope) | Client gets 304, sees stale data | Cache poisoning | Test inserts row, asserts ETag changed |
| **DC-4** | `secondsight directive --active --format json --no-server` produces JSON with `disabled_at` field; server-mode produces JSON without | Agent works in dev, breaks in prod | Schema drift | Test asserts `--no-server` JSON byte-equals API JSON for the same DB row |
| **DC-5** | Refactor accidentally drops `WHERE status='active'` from `GET /api/directives` default scope | Agent reads disabled directives as active, injects them | Disabled fires anyway | Test asserts disabled directive NOT in default response |
| **DC-6** | `GET /api/analysis/sessions/{id}` for a session with no report → 200 with empty body | "Looks fine, just no data" | Should be 404 | Test asserts 404 status |
| **DC-7** | `GET /api/analysis/trends?limit=10` applies LIMIT to the JOINed table; 10 = 10 flag rows, not 10 sessions | Trends chart "shows last 10 sessions" but actually shows last 10 flag occurrences | Off-by-window pagination | Test inserts 50 sessions × 5 flags each, asserts response has 10 sessions, not 10 flags |
| **DC-8** | Re-enable a directive — the previous `disabled_reason` is cleared by `update_status(ACTIVE, reason=None)` | History is lost | Soft DC; documented in API caveats | Test asserts the API audit log preserves prior `disabled_reason` (or document caveat in OpenAPI desc) |

### File map

- **NEW** `src/secondsight/api/analysis.py` — ~350 LOC. Pydantic
  shapes (`AnalysisSummary`, `SessionAnalysisItem`, `ListSessionsResponse`,
  `SessionAnalysisDetail`, `BehaviorFlagOut`, `TrendsResponse`,
  `TrendsBucket`, `AggregationResponse`), router with 6 GET routes,
  ETag computation helpers.
- **NEW** `src/secondsight/api/directives.py` — ~200 LOC. Pydantic
  shapes (`DirectiveOut`, `DirectivePatchRequest`), router with
  GET + PATCH, idempotency-aware update path.
- **NEW** `src/secondsight/cli/directive.py` — ~200 LOC. Typer
  app reusing `DirectiveOut` and `DirectivePatchRequest` from
  the API; in-process and server-mode paths.
- **MODIFIED** `src/secondsight/api/server.py` — `app.include_router(
  analysis_router)` and `app.include_router(directives_router)`
  inside `create_app()`.
- **MODIFIED** `src/secondsight/cli/app.py` — `app.add_typer(
  directive.app, name="directive")`.
- **MODIFIED** `src/secondsight/storage/behavior_flags_repository.py`
  — add `count_per_session_for_project` (~30 LOC).
- **NEW** `tests/api/test_analysis.py` — endpoint death + happy
  tests, ~30 cases.
- **NEW** `tests/api/test_directives.py` — endpoint death + happy
  tests, ~20 cases.
- **NEW** `tests/cli/test_directive.py` — CLI death + happy tests,
  ~15 cases.
- **NEW** `tests/storage/test_behavior_flags_repository_count_per_session.py`
  — repo death + happy tests, ~8 cases.

### Out-of-scope reaffirmed

- `cli/analyze.py` — already shipped under GUR-103 P2-15. NOT touched.
- `observation_schemas.py` — untracked artifact from GUR-147. NOT touched.
- `POST /api/analyze` — deferred per locked decision 4.

### Undocumented assumptions (carried forward from pre-thinking)

1. **A4-weak-etag** — ETag is `W/"…"` weak.
2. **G2-summary-fields** — exact field set as specified above; if
   GUR-106 needs more, follow-up.
3. **G3-trends-default-limit** — 50 sessions, max 200.
4. **G4-patch-validation-error-code** — 400 (not 422) for invalid
   transition with explicit body.
5. **G5-cli-enable-flag** — `--enable ID` is included for symmetry.
6. **U1-etag-no-consumer** — ETag implemented even if GUR-106
   doesn't yet send `If-None-Match`. Removable in a follow-up.
7. **U2-disabled-fields-always-emit** — `disabled_at` /
   `disabled_reason` always present in response, `null` for active.
