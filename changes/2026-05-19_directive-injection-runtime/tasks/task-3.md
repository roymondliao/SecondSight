# Task 3: Implement UserPromptSubmit hit guidance runtime with bypass and mode-aware evaluator

## Context

Read: `overview.md`, `2-plan.md` §2.4-§2.5.

This task implements the B contract:

- `POST /hook/injection/user-prompt/{agent}`
- agent-scoped bypass registry
- SecondSight-owned LLM ambiguity evaluator
- fixed guidance template mapping
- `user-prompt.sh` sync fetch + async ingest

The evaluator must respect configured runtime mode. In CLI mode it must reuse
the existing hook-disable recursion guard.

## Files

- Modify: `src/secondsight/api/injection.py`
- Create: `src/secondsight/feedback/prompt_guidance.py`
- Create: `src/secondsight/feedback/prompt_evaluator.py`
- Modify: `scripts/hooks/user-prompt.sh`
- Test: `tests/api/test_injection_user_prompt.py`
- Test: `tests/feedback/test_prompt_guidance.py`
- Test: `tests/feedback/test_prompt_evaluator.py`

## Death Test Requirements

- Test: bypass prompt skips evaluator and returns `204`
- Test: CLI-mode evaluator subprocess inherits `SECONDSIGHT_DISABLE_HOOKS=1`
- Test: evaluator timeout or malformed JSON fails open and does not block observation ingest

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

- Potential shortcut: falling back to agent self-evaluation instead of SecondSight-owned classification
- Potential shortcut: using session-level `systemMessage` for Codex UserPromptSubmit
- Assumption to verify: accepted Codex event-scoped output contract is sufficient for v1 without a transcript-backed capture

## Acceptance Criteria

- Covers: "CLI-mode hit evaluator subprocess is hook-disabled"
- Covers: "Malformed or timed-out hit evaluator fails open"
- Covers: "Bypass patterns do not trigger ambiguity evaluation"
- Covers: "UserPromptSubmit returns event-scoped guidance only on semantic hit"
