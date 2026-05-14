# Task 7: E2E smoke (both modes) + legacy-config regression

## Context

Read: `overview.md`, `2-plan.md` §10 (DA-4), `acceptance.yaml` Migration scenarios.

This task is the final integration check. After Tasks 1-6 are merged, this task verifies:
1. **Both modes work end-to-end** with the real `secondsight` CLI against a fixture session — no mocks. `mode=cli` + Claude Code, `mode=cli` + Codex (if PoC didn't drop it), `mode=sdk` + Anthropic primary, `mode=sdk` + fallback engaged.
2. **Legacy-config users upgrade cleanly**: a synthetic `~/.secondsight/config.toml` containing the pre-toggle flat `[analysis] default_agent` triggers WARN, server starts (after `secondsight init` per DC5), analysis runs in cli mode.
3. **Regression**: previously-passing SDK-only test cases still pass when `mode=sdk` is explicitly set.

If any E2E scenario fails, this task does NOT proceed to merge. Failures here likely point at integration bugs invisible to per-task unit tests.

## Files

- Create: `tests/e2e/test_mode_toggle_cli.py` — gated by `SECONDSIGHT_TEST_REAL_CLI=1`
- Create: `tests/e2e/test_mode_toggle_sdk.py` — gated by `SECONDSIGHT_TEST_REAL_LLM=1` (requires valid `$ANTHROPIC_API_KEY`)
- Create: `tests/e2e/test_legacy_config_upgrade.py` — uses fixture legacy config file, no external services required (just the loader + precheck path)
- Create: `tests/e2e/fixtures/legacy_config.toml` — synthetic pre-toggle config
- Create: `tests/e2e/fixtures/cli_mode_config.toml` — locked schema, cli mode
- Create: `tests/e2e/fixtures/sdk_mode_config.toml` — locked schema, sdk mode with `${ANTHROPIC_API_KEY}` reference

## Death Test Requirements

These are integration-level scenarios; the underlying death tests live in Tasks 1-6. This task verifies they compose correctly.

- Test: spin up `secondsight serve` in a temp `~/.secondsight/` with `cli_mode_config.toml` + state.json with `init_agent="claude_code"`; replay a fixture session_end event via HTTP POST; assert `intelligence.db` gets a row with `dispatched_via="cli"`, `cli_agent="claude_code"`, `status="success"` within `timeout_seconds` budget; assert the spawned process actually invoked `claude` (verify via process tree probe or via the analysis row's `error_details` not containing subprocess errors)
- Test: same but with `mode=sdk` config and a valid `$ANTHROPIC_API_KEY`; assert row has `dispatched_via="sdk"`, `primary_model=<configured>`, `status="success"`
- Test: `mode=sdk` with intentionally-invalid `ANTHROPIC_API_KEY` (e.g., "sk-this-is-invalid"); assert row has `dispatched_via="sdk"`, `status="failure"`, `error_details` contains the upstream API error message (not a generic "something failed")
- Test (DC4 happy reroute): `mode=sdk` with invalid primary but valid fallback; assert `status="success"`, `fallback_used=true`
- Test (legacy upgrade — no real CLI/LLM needed): start `secondsight serve` with `legacy_config.toml` AND no state.json → server exits non-zero (precheck fails with "run init first"). Then run `secondsight init --agent claude_code`. Re-start server → starts successfully with WARN log about legacy field; mode defaults to "cli"; default_agent resolves via state.json.
- Test (regression): a previously-passing SDK-only test from `tests/analysis/` (pick one already in the repo) is run twice — once unchanged (mode default cli), once with explicit `mode=sdk` config — second run passes; first run fails or skips with clear reason ("CLI mode requires real claude CLI, set SECONDSIGHT_TEST_REAL_CLI=1")

## Implementation Steps

- [ ] Step 1: Audit existing `tests/analysis/` and `tests/sdk/` for tests that previously depended on the implicit-SDK behavior. List them.
- [ ] Step 2: For each listed test, determine if it still passes with `mode=sdk` explicitly. If it doesn't, file as a regression and fix it in the appropriate prior task (likely Task 5).
- [ ] Step 3: Write `tests/e2e/test_legacy_config_upgrade.py` — does NOT need real CLI / LLM, uses loader + precheck + server bootstrap path only
- [ ] Step 4: Write `tests/e2e/test_mode_toggle_cli.py` — gated; tests real subprocess invocation
- [ ] Step 5: Write `tests/e2e/test_mode_toggle_sdk.py` — gated; tests real LLM call
- [ ] Step 6: Run the non-gated tests (`legacy_config_upgrade.py`) — verify pass
- [ ] Step 7: Locally export the gating env vars and run gated tests; capture output in `changes/2026-05-14_analysis-mode-toggle/e2e-results.md`
- [ ] Step 8: Update CI config (if applicable) to ensure gated tests do NOT run in PR CI but DO run nightly OR in a release pipeline
- [ ] Step 9: Run `pre-commit run --all-files`
- [ ] Step 10: Write scar report — must include the e2e-results.md reference and any flaky / surprising behavior
- [ ] Step 11: Commit

## Expected Scar Report Items

- Potential shortcut: marking gated tests as `@pytest.mark.skip` permanently. **Don't.** They must run somewhere (nightly, release pipeline, manual pre-merge). Skipping permanently = lying about coverage.
- Potential shortcut: making fallback path test use the same model alias for primary and fallback. **Don't.** That doesn't test fallback engagement; both must differ in failure mode (one unreachable, one healthy).
- Potential shortcut: using `subprocess.run` directly in E2E tests instead of going through the actual `secondsight serve` HTTP server. **Don't.** The point is to verify the full ingress → dispatch → storage chain, not just individual modules.
- Assumption to verify: existing test suite has fixtures for replaying session_end events. If not, this task may need to create them (audit `tests/conftest.py` and `tests/fixtures/` first).
- Assumption to verify: CI environment can NOT run real `claude` / `codex` CLI binaries (likely no auth). Gated tests are designed for local + release-pipeline only.
- Watch for: state.json pollution across tests — each test must use a temp `~/.secondsight/` (override via env var `SECONDSIGHT_HOME` if loader supports it; otherwise via monkey-patched home resolution). Don't leak between test runs.

## Acceptance Criteria

Covers from `acceptance.yaml`:
- All happy path scenarios (both modes)
- Migration scenarios (fresh install, legacy upgrade)
- DC4 (full both-fail path with real upstream failures)
- DC5 → re-init → success flow (legacy upgrade path)
- "Happy path — manual `secondsight analyze --session <id>` works in both modes" (verify via CLI invocation in tests)
