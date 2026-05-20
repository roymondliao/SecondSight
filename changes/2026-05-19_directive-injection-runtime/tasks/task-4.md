# Task 4: Rewrite contract tests for raw hook payloads, fail-open, and recursion guard

## Context

Read: `overview.md`, `2-plan.md` §4-§5.

Earlier tests accepted plain-text stdout as proof of injection. That is now a
bug. This task hardens the contract surface so payload shape, fail-open
behavior, and recursion guard become executable assertions.

## Files

- Modify: `tests/api/test_session_start.py`
- Test: `tests/api/test_injection_session_start.py`
- Test: `tests/api/test_injection_user_prompt.py`
- Modify: `tests/scripts/test_hook_fallback.py`
- Modify: `tests/adapters/test_claude_code.py`
- Modify: `tests/adapters/test_codex.py`
- Modify: `tests/installer/test_claude_settings.py`
- Modify: `tests/installer/test_codex_hooks.py`
- Modify: `changes/2026-05-19_directive-injection-runtime/index.yaml`

## Death Test Requirements

- Test: no test path treats plain-text conventions as sufficient proof of valid SessionStart injection
- Test: Codex SessionStart and UserPromptSubmit payload contracts are asserted separately
- Test: fail-open and hook-disable behavior are asserted at the API/script boundary, not only inside helper functions

## Implementation Steps

- [ ] Step 1: Write death tests
- [ ] Step 2: Run death tests — verify they fail
- [ ] Step 3: Write unit tests
- [ ] Step 4: Run unit tests — verify they fail
- [ ] Step 5: Implement minimal code to pass all tests
- [ ] Step 6: Run all tests — verify they pass
- [ ] Step 7: Write scar report
- [ ] Step 8: Commit

## Expected Scar Report Items

- Potential shortcut: updating hook scripts without updating their capture/contract tests
- Potential shortcut: leaving old route tests in place so obsolete behavior still passes CI
- Assumption to verify: coverage is still "verified" once old plain-text assumptions are removed

## Acceptance Criteria

- Covers: "SessionStart returns agent-ready raw payload"
- Covers: "UserPromptSubmit returns event-scoped guidance only on semantic hit"
- Covers: "Codex session-level and event-level injection shapes do not collapse into one payload"
