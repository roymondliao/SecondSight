# Task 2: Prove Codex thin ingress persistence with verified payloads

## Context
Read: `overview.md`

Adapter-level correctness is not enough. This task proves the real observation path:

`/hook/codex/{event_type}` -> adapter -> tracker -> persisted event row / event JSON

The source payloads must come from the verified Codex fixtures, not hand-written mini-payloads that omit the dangerous fields.

## Files
- Create: `tests/api/test_ingress_codex.py`
- Modify: `tests/fixtures/codex/pre_tool_use.json`
- Modify: `tests/fixtures/codex/post_tool_use.json`
- Modify: `tests/fixtures/codex/session_start.json`
- Modify: `tests/fixtures/codex/user_prompt_submit.json`
- Modify: `tests/fixtures/codex/stop.json`
- Modify: `src/secondsight/api/hooks.py:112-248` only if the verified Codex payloads reveal an ingress rejection or persistence mismatch
- Test: `tests/api/test_ingress_codex.py`

## Death Test Requirements
- Test: POSTing a verified `UserPromptSubmit` payload must persist exact `prompt_text` into the stored event JSON.
- Test: POSTing verified `PostToolUse` and `Stop` payloads must not leak `tool_response` or `last_assistant_message` into persisted `Event.data`.
- Test: route/payload mismatch must return explicit 422 rather than silently persisting the wrong event type.

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
- Potential shortcut: asserting only HTTP 200 and never inspecting persisted event JSON / DB state.
- Potential shortcut: replacing verified fixtures with simplified ingress payloads that do not exercise privacy boundaries.
- Assumption to verify: tracker/pipeline should preserve `tool_use_id` and `turn_id` exactly as normalized by the adapter.

## Acceptance Criteria
- Covers: `Unknown outcome - adapter unit tests pass but the thin ingress path loses Codex fields`
- Covers: `Success - verified Codex hook payloads survive the observation path with evidence`

