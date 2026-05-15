# Task 1: Lock verified Codex adapter and fixture contract

## Context
Read: `overview.md`

Codex real hook stdin was captured on 2026-05-13. That capture proved:

- `UserPromptSubmit` carries top-level `prompt`
- `PreToolUse` / `PostToolUse` are top-level payloads, not nested `hook_event`
- `PostToolUse.tool_response` is a raw string
- `Stop.last_assistant_message` is a raw string

This task turns those facts into a stable adapter + fixture contract.

## Files
- Create: `tests/fixtures/codex/_README.md`
- Modify: `src/secondsight/adapters/codex.py:1-231`
- Modify: `tests/fixtures/codex/pre_tool_use.json`
- Modify: `tests/fixtures/codex/post_tool_use.json`
- Modify: `tests/fixtures/codex/session_start.json`
- Modify: `tests/fixtures/codex/user_prompt_submit.json`
- Modify: `tests/fixtures/codex/stop.json`
- Modify: `tests/adapters/test_codex.py:1-302`
- Modify: `tests/adapters/test_codex_fixtures.py:1-87`
- Test: `tests/adapters/test_codex.py`
- Test: `tests/adapters/test_codex_fixtures.py`

## Death Test Requirements
- Test: `UserPromptSubmit.prompt` must survive as exact `action_metadata.prompt_text`, never `prompt_length`, empty string, or cwd-only metadata.
- Test: PascalCase `hook_event_name` is mandatory; lower-case or nested payload regressions must fail loudly.
- Test: `tool_response` and `last_assistant_message` canary values must never appear in serialized `PartialEvent.data`.

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
- Potential shortcut: copying captured payload values into fixtures without documenting provenance and drift rules.
- Potential shortcut: dropping raw fields by deleting them from fixtures instead of testing the drop boundary with canaries.
- Assumption to verify: the Codex adapter should preserve `prompt_text`, `turn_id`, and `tool_use_id`, but not raw response text.

## Acceptance Criteria
- Covers: `Silent failure - UserPromptSubmit stores anything other than the full prompt text`
- Covers: `Silent failure - Codex fixtures drift back to invented payload shapes`
- Covers: `Silent failure - raw tool_response or last_assistant_message leaks into Event.data`
