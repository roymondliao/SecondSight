# Task 2: Migrate SessionStart convention injection to the new injection namespace

## Context

Read: `overview.md`, `2-plan.md` §2.3.

This task finishes the A contract:

- `POST /hook/injection/session-start/{agent}`
- convention selection using resolved feedback budget
- server-side convention template
- raw payload passthrough in `session-start.sh`

The hook must still separately ingest `session_start` through the existing
observation path after printing any injected payload.

## Files

- Modify: `src/secondsight/api/injection.py`
- Modify: `src/secondsight/feedback/convention.py`
- Modify: `scripts/hooks/session-start.sh`
- Modify: `tests/scripts/test_hook_fallback.py`
- Test: `tests/api/test_injection_session_start.py`
- Modify: `tests/installer/test_claude_settings.py`
- Modify: `tests/installer/test_codex_hooks.py`

## Death Test Requirements

- Test: no conventions returns `204` and hook exits `0`
- Test: SessionStart hook prints the raw server payload unchanged rather than plain text conventions
- Test: non-default feedback budget changes selection behavior at runtime

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

- Potential shortcut: leaving `session-start.sh` to print unwrapped convention text
- Potential shortcut: treating empty selection as `"conventions": ""` JSON instead of true no-op payload semantics
- Assumption to verify: installer command strings remain stable because scripts, not settings payloads, own the transport

## Acceptance Criteria

- Covers: "SessionStart returns agent-ready raw payload"
- Covers: "Configured convention budget is honored at runtime"
