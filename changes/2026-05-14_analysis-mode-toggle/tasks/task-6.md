# Task 6: Mode-aware dispatch in ProjectAnalysisRuntime + startup pre-check

## Context

Read: `overview.md`, `2-plan.md` §1, §7, §9 (DC5, DC6, DC10).

This task integrates Tasks 1, 2, 4, 5. It introduces the mode-aware dispatch branch in `ProjectAnalysisRuntime.dispatch()` and the server-startup `precheck()` validation.

Architecture invariant: **ALL callers** of analysis (session_end hook handler, sweeper timeout recovery, manual `secondsight analyze` CLI) go through `ProjectAnalysisRuntime.dispatch()`. None of them reference `[general].mode`. Mode-awareness is centralized in one place.

Pre-check runs at server startup ONLY (not at `secondsight init`). On `PrecheckResult.fail()`, server exits with non-zero status — does NOT start in degraded mode.

This task may also require an `intelligence.db` schema migration if the analysis row table doesn't already have `dispatched_via`, `cli_agent`, `primary_model`, `fallback_used`, `retry_count`, `status`, `error_details` columns. Verify and migrate if needed.

## Files

- Create: `src/secondsight/config/precheck.py`
- Modify: `src/secondsight/analysis/runtime.py` — `ProjectAnalysisRuntime.dispatch()` branches by `config.general.mode`; instantiates `CLIAnalysisDispatcher` or `SDKAnalysisDispatcher` as appropriate
- Modify: `src/secondsight/cli/serve.py` — call `precheck()` at startup; on fail, log actionable error and exit non-zero
- Modify: `src/secondsight/storage/<analysis row module>` — add columns if missing (`dispatched_via TEXT`, `cli_agent TEXT NULL`, `primary_model TEXT NULL`, `fallback_used BOOLEAN`, `retry_count INTEGER`, `status TEXT`, `error_details TEXT NULL`); write a small migration
- Test: `tests/config/test_precheck.py`
- Test: `tests/analysis/test_runtime_dispatch_mode.py`
- Test: `tests/analysis/test_runtime_concurrent_dispatch.py` — DC10
- Test: `tests/cli/test_serve_precheck.py`

## Death Test Requirements

Before any implementation:

- Test (DC5): `mode=cli, default_agent="auto"`, state.json missing → `precheck()` returns `PrecheckResult.fail(reason="state_missing", message="Run `secondsight init --agent <claude_code|codex>` before starting the server")`
- Test (DC5): same config + state.json present with `init_agent="claude_code"` BUT `claude` binary not in PATH → `precheck()` returns `PrecheckResult.fail(reason="cli_binary_missing", message="`claude` CLI not found in PATH")`
- Test (DC5): `mode=cli, default_agent="opencode"` → `precheck()` returns `PrecheckResult.fail(reason="opencode_not_supported", message="opencode CLI mode out of scope in this release; set default_agent to 'claude_code' or 'codex'")`
- Test (DC7): `mode=sdk`, all three providers empty after `${VAR}` resolution → `precheck()` returns `PrecheckResult.fail(reason="no_providers", message="mode=sdk requires at least one provider key resolvable; if you intended to use $ANTHROPIC_API_KEY from shell env, write `ANTHROPIC_API_KEY = \"${ANTHROPIC_API_KEY}\"` in config")`
- Test: `mode=sdk`, primary_model is empty → `precheck()` returns `PrecheckResult.fail(reason="primary_model_missing", ...)`
- Test (DC6 forensics): on `precheck()` success for cli mode, INFO log line is emitted containing the resolved binary path (`shutil.which(...)` result) for forensics
- Test: `secondsight serve` invocation with failing precheck → exits with non-zero status code (not 0)
- Test: `secondsight serve` invocation with passing precheck → starts normally (verify by mocking the precheck and the daemon entry)
- Test (mode dispatch): `mode=cli` + valid config → `ProjectAnalysisRuntime.dispatch()` calls `CLIAnalysisDispatcher.dispatch()` (verify via injected mock); `dispatched_via` field on the returned `AnalysisOutput` equals `"cli"`
- Test (mode dispatch): `mode=sdk` + valid config → `ProjectAnalysisRuntime.dispatch()` calls `SDKAnalysisDispatcher.dispatch()`; `dispatched_via` equals `"sdk"`
- Test (DC10): two `ProjectAnalysisRuntime.dispatch()` calls for the same `session_id` running in parallel via `asyncio.gather` → only ONE `intelligence.db` row is created; the second call returns existing row (no duplicate LLM tokens)
- Test (sweeper mode-agnosticism): grep the sweeper module for `config.general.mode` or `mode == "cli"` etc. → must return ZERO matches (the sweeper does NOT reference mode)
- Test (manual analyze mode-agnosticism): same grep on `src/secondsight/cli/analyze.py` → must return ZERO matches
- Test (DB schema): row written for an analysis has all of {`dispatched_via`, `cli_agent`, `primary_model`, `fallback_used`, `retry_count`, `status`} populated (none NULL for required ones; null OK for cli_agent when sdk and primary_model when cli)

## Implementation Steps

- [ ] Step 1: Audit current `intelligence.db` analysis row schema. List existing columns. Determine which need adding.
- [ ] Step 2: Write death tests for `precheck()` (above)
- [ ] Step 3: Run death tests — verify import failure
- [ ] Step 4: Implement `src/secondsight/config/precheck.py`:
  - `class PrecheckResult` with `.ok()` / `.fail(reason, message)` factory methods, `.is_ok` property
  - `def precheck(config: SecondSightConfig, state: SecondSightState | None, resolved_keys: dict[str, str]) -> PrecheckResult`
  - Resolves `default_agent="auto"` via state; if state missing returns fail
  - For cli mode: check binary in PATH, reject opencode
  - For sdk mode: check primary_model and at least one resolved_key non-empty
- [ ] Step 5: Write death tests for `ProjectAnalysisRuntime.dispatch()` mode branching
- [ ] Step 6: Run them — verify failure
- [ ] Step 7: Refactor `ProjectAnalysisRuntime` to branch by `config.general.mode`. Inject both dispatcher types (or factory) at runtime construction. SDK dispatcher uses the resolved_keys from config load.
- [ ] Step 8: Write DC10 concurrent dispatch test. Make it pass either by: (a) verifying existing orchestrator-level lock works, or (b) adding `INSERT ... ON CONFLICT DO NOTHING` semantics on the analysis row.
- [ ] Step 9: Add DB columns if needed; write the schema migration following existing migration conventions (probably alembic-style or a versioned SQL file — check `src/secondsight/storage/migrations/` if it exists).
- [ ] Step 10: Wire `precheck()` into `src/secondsight/cli/serve.py`. On fail, log via `loguru` and `raise typer.Exit(code=1)` or equivalent.
- [ ] Step 11: Verify sweeper + manual analyze code do NOT reference mode. If they do (legacy code), refactor to remove (route everything through `ProjectAnalysisRuntime.dispatch()`).
- [ ] Step 12: Run all tests
- [ ] Step 13: Run `pre-commit run --all-files`
- [ ] Step 14: Write scar report
- [ ] Step 15: Commit

## Expected Scar Report Items

- Potential shortcut: skipping the mode-agnosticism grep test on sweeper/manual analyze. **Don't.** Without it, future code can re-introduce mode checks elsewhere and silently re-create the dishonest-naming rot in a different layer.
- Potential shortcut: adding `default_agent` and other mode-specific fields directly to `intelligence.db` analysis row as nullable, then writing application code that assumes they're nullable everywhere. **Don't.** Use CHECK constraints or application-level validators tied to `dispatched_via`.
- Potential shortcut: pre-check called per-request instead of once at server startup. **Don't.** Per-request precheck would mask DC8 (cache-once env mutation) and waste cycles.
- Potential shortcut: starting the server in "degraded mode" when precheck fails (e.g., disable analysis but keep observation). **Don't.** The whole point of the production bug was that silent degraded operation hid the failure for weeks. Hard fail at startup is the correct behavior.
- Assumption to verify: `secondsight serve` is the actual server entry. There may be alternate entries (daemon mode, dev mode) that bypass it. Trace all server start paths.
- Assumption to verify: the existing dispatcher entry in `ProjectAnalysisRuntime` is genuinely the only path. If sweeper bypasses it (DA-5), Task 6 must consolidate. If consolidation is large, that's a scar to document, not silently shortcut.
- Watch for: `intelligence.db` migration — backward compat. Existing rows from before this task won't have the new columns. Migration must set defaults (e.g., `dispatched_via='sdk'` for pre-existing rows, since SDK was the only path before). Document this in the migration.

## Acceptance Criteria

Covers from `acceptance.yaml`:
- DC5 (state.json + binary checks at server startup)
- DC6 (binary missing — startup + dispatch-time forensics)
- DC10 (concurrent dispatch dedup)
- DC11 partial (state.json overwrite — init side is Task 1; precheck reads what init wrote)
- "Pre-check at server start logs resolved binary paths for forensics" (degradation)
- "Happy path — sweeper-triggered dispatch is mode-agnostic at caller"
- Mode-dispatch happy paths for both cli and sdk
