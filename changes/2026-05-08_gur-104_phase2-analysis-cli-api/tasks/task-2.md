# Task 2: api/directives.py — GET + PATCH endpoints

## Context

Read: `overview.md`. This task builds the directive control surface
that operators (CLI) and dashboard (GUR-106) use to read and disable
directives. The PATCH must be idempotent on no-op (DC-2) and the GET
must default to `active=true` (DC-5). Schemas in this file are
imported by `cli/directive.py` (task-4) — schema-as-contract is the
DC-4 defense.

Existing surface to study:
- `src/secondsight/api/observation.py` — convention reference (frozen
  Pydantic, required `project_id` Query, ETag pattern).
- `src/secondsight/api/_id_safety.py` — `is_safe_id` for path params.
- `src/secondsight/storage/directives_repository.py:106` —
  `get_active_conventions` (the GET-active backing query).
- `src/secondsight/storage/directives_repository.py:188` —
  `update_status` (lifecycle write; raises ValueError on rule
  violation, LookupError on missing id).
- `src/secondsight/analysis/schemas.py:142` — `Directive` model.
- `src/secondsight/analysis/schemas.py:58` — `DirectiveStatus` enum;
  user-PATCHable values are `{active, disabled}` only.

## Files

- Create: `src/secondsight/api/directives.py`
- Test: `tests/api/test_directives.py`

## Death Test Requirements

Write these tests **before** the implementation. Use FastAPI
`TestClient` against an app built via the existing `create_app()`
factory.

- **DT-2.1** (DC-2 PATCH no-op): Insert a directive with `status=active`
  and `updated_at=T0`. PATCH `{status: "active"}`. Assert response
  is 200 with the directive. Re-fetch from DB. Assert `updated_at
  == T0` (no advance).
- **DT-2.2** (DC-5 active default): Insert 2 active + 1 disabled.
  GET `/api/directives?project_id=P`. Assert `len(items) == 2` and
  no item has `status=disabled`.
- **DT-2.3** (re-enable clears lifecycle fields): Insert directive
  with `status=disabled, disabled_at=T, disabled_reason="bad"`.
  PATCH `{status: "active"}`. Assert response.disabled_at is null,
  response.disabled_reason is null, response.status is "active",
  response.updated_at > T.
- **DT-2.4** (lifecycle contract): PATCH `{status: "disabled",
  reason: ""}`. Assert 400 with body explaining "reason required for
  disabled status".
- **DT-2.5** (analyzer-only status): PATCH `{status: "expired"}`.
  Assert 400 with body containing `"allowed": ["active", "disabled"]`.
- **DT-2.6** (DC-1 cross-project): Insert directive D in project A.
  PATCH `/api/directives/D?project_id=B`. Assert 404.
- **DT-2.7** (ETag mutates on real change): GET `/api/directives?
  project_id=P` → ETag E. PATCH a directive in P. GET again. Assert
  new ETag != E.
- **DT-2.8** (ETag stable on no-op): GET → ETag E. PATCH no-op.
  GET → assert ETag == E.
- **DG-2.1** (degradation doc): Inspect the OpenAPI spec for the
  PATCH route. Assert the description string contains the substring
  "GUR-105" or "Phase 3" and "restart".
- **HP-2.1**: GET round-trip with the 2:1 fixture.
- **HP-2.2**: PATCH active→disabled, GET, assert disabled_reason
  persisted in DB.

## Implementation Steps

- [ ] Step 1: Write the 11 tests above; commit failing.
- [ ] Step 2: Run tests — verify all fail (router not registered yet).
- [ ] Step 3: Define `DirectiveOut` Pydantic model with
  `model_config = ConfigDict(frozen=True, extra="forbid")`. Fields
  exactly mirror `Directive` schema; `disabled_at` and
  `disabled_reason` are `datetime | None` and `str | None` (U2 lock).
- [ ] Step 4: Define `DirectivePatchRequest` with `status: Literal["active", "disabled"]`,
  `reason: str | None = None`, plus a `model_validator(mode="after")`:
  - `status == "disabled"` → `reason` must be a non-empty string
  - `status == "active"` → `reason` must be `None`
- [ ] Step 5: Implement `compute_directives_etag(project_id, db_engine)`
  → `W/"<sha256(MAX(updated_at) over directives WHERE project_id)>"`.
- [ ] Step 6: Implement `GET /api/directives` route. Required
  `project_id: str = Query(...)`, optional `active: bool = Query(True)`.
  Compute ETag; honor `If-None-Match`. Return `[DirectiveOut, ...]`.
- [ ] Step 7: Implement `PATCH /api/directives/{directive_id}`:
  ```python
  with db.engine.begin() as conn:
      current = repo.get_by_id(directive_id)
      if current is None or current.project_id != project_id:
          raise HTTPException(404)
      if current.status.value == request.status and (
          request.status != "disabled"
          or current.disabled_reason == request.reason
      ):
          # No-op: return current row, do NOT advance updated_at
          return DirectiveOut.model_validate(current)
      try:
          repo.update_status(directive_id, DirectiveStatus(request.status), request.reason)
      except ValueError as e:
          raise HTTPException(400, detail={"error": "lifecycle_violation", "message": str(e)})
      return DirectiveOut.model_validate(repo.get_by_id(directive_id))
  ```
- [ ] Step 8: Add OpenAPI route description on PATCH that includes
  the Phase 3 cache caveat (DG-2.1).
- [ ] Step 9: Export `directives_router` for task-5 wire-up.
- [ ] Step 10: Run tests — verify all 11 pass.
- [ ] Step 11: Scar report.
- [ ] Step 12: Commit `GUR-104 task-2: api/directives.py GET+PATCH`.

## Expected Scar Report Items

- The no-op detection compares `current.status` (an enum) to
  `request.status` (a Literal string). Convert one consistently.
- `DirectivePatchRequest` is `extra="forbid"`. A request with
  extra fields will return 422 (Pydantic) — make sure the test
  expects the right code (DT-2.5 expects 400 from explicit
  validation, not 422).
- ETag computation issued under a separate connection from the
  PATCH transaction can race. For PATCH, recompute the ETag from
  inside the transaction's connection.
- The `DirectiveOut` shape MUST include all nullable lifecycle
  fields even on active directives (U2 documented assumption).
  Drop them at your peril — DC-4 schema drift.

## Acceptance Criteria

Covers `acceptance.yaml`:
- "Silent failure - PATCH no-op advances updated_at" (DC-2)
- "Silent failure - active filter dropped from GET /api/directives" (DC-5)
- "Silent failure - cross-project session leak" — adapted for PATCH (DC-1)
- "Degradation - server-mode PATCH succeeds but Phase 3 cache not yet wired"
- "Success - PATCH /api/directives/{id} disabled→active transition"
- "Success - GET /api/directives ETag round-trip 304"
