# Task 5: Wire-up — register routers + CLI command + integration smoke tests

## Context

Read: `overview.md`. The previous tasks deliver three new
modules with their own routers and Typer apps but nothing imports
them yet. This task plumbs them into the FastAPI factory and the CLI
top-level Typer app, then proves end-to-end with two small
integration tests. It also includes a regression test that GUR-103's
existing `secondsight analyze` continues to work — silent-breaking it
during wire-up is the most likely accidental regression.

Existing surface to study:
- `src/secondsight/api/server.py` — `create_app()` factory; routers
  must be registered inside the function body, BEFORE returning the
  app, but AFTER lifespan setup.
- `src/secondsight/cli/app.py` — top-level Typer app; existing
  `add_typer` registrations for analyze / init / serve / status /
  sync / cleanup.
- `src/secondsight/api/observation.py` — the established router
  registration precedent.

## Files

- Modify: `src/secondsight/api/server.py` — `app.include_router(
  analysis_router)` and `app.include_router(directives_router)`.
- Modify: `src/secondsight/cli/app.py` — `app.add_typer(directive.app,
  name="directive")`.
- Test: `tests/api/test_server_routes.py` — additive route-presence
  tests; OR extend an existing test if one exists for observation
  registration.
- Test: `tests/integration/test_gur104_e2e.py` — two smoke tests.

## Death Test Requirements

Write these tests **before** the wire-up:

- **DT-5.1** (analysis routes registered): Use `TestClient` against
  `create_app()`. GET `/api/analysis/summary?project_id=test`. Assert
  status is NOT 404 (route is registered). It's OK if the response
  is 200 with empty counts or 500 because no DB exists — just NOT
  the FastAPI route-not-found 404.
- **DT-5.2** (directives GET registered): Same pattern, GET
  `/api/directives?project_id=test`. NOT 404.
- **DT-5.3** (directives PATCH method registered): PATCH
  `/api/directives/test?project_id=p` with valid JSON body. Assert
  the method is allowed (405 would mean the route exists but PATCH
  isn't accepted).
- **DT-5.4** (CLI help lists modes): Run
  `secondsight directive --help`. Assert stdout contains the strings
  "--active", "--disable", "--enable", "--format", "--no-server".
- **DT-5.5** (regression — analyze still works): Run
  `secondsight analyze --help`. Assert exit 0 and stdout contains
  "--session" and "--force". This catches accidentally breaking
  GUR-103's CLI during the registration churn.
- **HP-5.1** (E2E summary): Spin up a TestClient. Insert directly to
  the DB: 1 session_reports row, 2 behavior_flags. GET summary.
  Assert counts match.
- **HP-5.2** (E2E PATCH→GET): Insert 1 active directive. PATCH it
  to disabled with reason. GET `/api/directives` (active=true
  default). Assert directive is NOT in the response.

## Implementation Steps

- [ ] Step 1: Write the 7 tests; commit failing.
- [ ] Step 2: Run tests — DT-5.1, 5.2, 5.3 fail with 404; DT-5.4
  fails because the directive subcommand doesn't exist yet; DT-5.5
  passes (existing); HP-5.1, 5.2 fail.
- [ ] Step 3: In `api/server.py`, import
  `analysis.analysis_router` and `directives.directives_router`,
  call `app.include_router(...)` for each inside `create_app()`.
- [ ] Step 4: In `cli/app.py`, import
  `secondsight.cli.directive as directive_module` and call
  `app.add_typer(directive_module.app, name="directive")`.
- [ ] Step 5: Run tests — all pass.
- [ ] Step 6: Run the full repo test suite (`pytest tests/`) and
  verify no unrelated regressions. Especially watch
  `tests/api/test_observation.py` and `tests/cli/test_analyze.py`
  for incidental breakage.
- [ ] Step 7: Scar report.
- [ ] Step 8: Commit `GUR-104 task-5: register routers + CLI`.

## Expected Scar Report Items

- Router registration order: register `directives_router` and
  `analysis_router` AFTER the existing observation router so URL
  resolution is in a stable, code-reviewable order.
- Circular import risk: `cli/directive.py` imports from
  `api/directives.py`. If `api/__init__.py` later imports from
  `cli/`, you'll get an ImportError at server startup. Verify
  `api/__init__.py` does NOT import from `cli/`.
- The integration tests need the FastAPI lifespan to run so the
  `ProjectRegistry` is initialized. Use
  `with TestClient(app)` (context manager invokes lifespan).
- Pytest test_server_routes.py file may already exist; if so,
  extend rather than create — keep route-presence tests in one
  place.
- DT-5.5 is the regression net for GUR-103. If it fails, your
  wire-up introduced a top-level import error. Don't paper over —
  diagnose.

## Acceptance Criteria

Covers `acceptance.yaml`:
- All "Success" scenarios via end-to-end E2E tests
- DT-5.5 specifically guards against breaking GUR-103's existing
  trigger semantics during wire-up
