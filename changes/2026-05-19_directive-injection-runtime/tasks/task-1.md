# Task 1: Add feedback config and adapter render seams for injection payloads

## Context

Read: `overview.md`, `2-plan.md` §2.1-§2.2.

This task establishes the shared contract surface the rest of the feature
depends on:

- resolved `[feedback]` config
- final adapter render methods
- dedicated injection router scaffold

Do not implement SessionStart selection logic or UserPromptSubmit evaluator
logic in this task beyond the minimal scaffolding needed for route wiring.

## Files

- Create: `src/secondsight/api/injection.py`
- Modify: `src/secondsight/api/server.py`
- Modify: `src/secondsight/adapters/base.py`
- Modify: `src/secondsight/adapters/claude_code.py`
- Modify: `src/secondsight/adapters/codex.py`
- Modify: `src/secondsight/config/schema.py`
- Modify: `src/secondsight/config/loader.py`
- Modify: `src/secondsight/config/template.py`
- Test: `tests/api/test_injection_session_start.py`
- Modify: `tests/adapters/test_claude_code.py`
- Modify: `tests/adapters/test_codex.py`

## Death Test Requirements

- Test: feedback config is resolved from `[feedback]` and not silently hard-coded
- Test: Claude and Codex SessionStart/UserPromptSubmit render methods produce distinct payload shapes where required
- Test: injection router returns raw response bodies, not `{conventions, count, budget}` envelopes

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

- Potential shortcut: keeping the old `/hook/session-start` response envelope and forcing shell scripts to unwrap it
- Potential shortcut: encoding Codex SessionStart and UserPromptSubmit with the same payload shape
- Assumption to verify: `feedback` belongs in resolved config rather than an ad hoc loader helper

## Acceptance Criteria

- Covers: "Configured convention budget is honored at runtime"
- Covers: "Codex session-level and event-level injection shapes do not collapse into one payload"
