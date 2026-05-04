# Task 2: Claude Code hook payload fixtures (P1-9-fixtures)

## Context

Read: `2-plan.md` §1 (decision 6 — verified-vs-documented), §5 (drop_list), §7 (G1, G2 mapping table).

This task lands the JSON fixtures that `ClaudeCodeAdapter` (task-4) and the integration test (task-5) consume. **No production code in this task.** The fixtures are the empirical ground truth that prevents the "invented payload" failure mode named in the autopsy kill conditions.

**Plan refs:** P1-9 (fixture side, supports P1-10)
**SD refs:** §3.7.4 (drop rules — drives privacy canary placement)

## Files

- Create: `tests/fixtures/claude_code/__init__.py` (empty, for test discovery if needed)
- Create: `tests/fixtures/claude_code/pre_tool_use_bash.json` — verified
- Create: `tests/fixtures/claude_code/post_tool_use.json` — documented
- Create: `tests/fixtures/claude_code/user_prompt_submit.json` — documented
- Create: `tests/fixtures/claude_code/session_start.json` — documented
- Create: `tests/fixtures/claude_code/session_end.json` — documented
- Create: `tests/fixtures/claude_code/_README.md` — documents capture method, source URLs, drift policy

## Fixture shape

Each fixture is a JSON object with this top-level structure:

```json
{
  "_meta": {
    "_source": "verified" | "documented",
    "_capture_origin": "rtk-rewrite.sh observed input on 2026-05-04" | "https://docs.claude.com/en/docs/claude-code/hooks (snapshot 2026-05-04)",
    "_claude_code_hook_event_name": "PreToolUse",
    "_secondsight_event_type": "tool_use_start"
  },
  "payload": {
    // Verbatim Claude Code hook stdin JSON. Real or documented per _source.
  },
  "expected_partial_event_data": {
    // What ClaudeCodeAdapter.normalize().data should equal after drop_list applied.
    // Source of truth for AC-5 in acceptance.md.
  },
  "privacy_canary": "PRIVACY_CANARY_DO_NOT_STORE"
}
```

**Privacy canary placement:** at minimum one drop-listed field per fixture set to the canary string. For example, in `pre_tool_use_bash.json`:

```json
{
  "payload": {
    "session_id": "...",
    "tool_name": "Bash",
    "tool_input": {
      "command": "PRIVACY_CANARY_DO_NOT_STORE && echo hi"
    }
  },
  "expected_partial_event_data": {
    "tool_name": "Bash",
    "action_metadata": { "command_length": 41 }
  }
}
```

If `ClaudeCodeAdapter`'s drop logic ever regresses, the canary string surfaces in `Event.data` and the privacy test fails.

## Fixture content sources

| Fixture | _source | Origin |
|---------|---------|--------|
| `pre_tool_use_bash.json` | verified | Captured from `~/.claude/hooks/rtk-rewrite.sh` stdin shape during a real Claude Code session (the script reads stdin JSON and parses `.tool_input.command`, evidencing the field path). |
| `post_tool_use.json` | documented | Claude Code hooks documentation for PostToolUse event (fields: `session_id`, `tool_name`, `tool_input`, `tool_response`, `transcript_path`, `cwd`, `hook_event_name`). |
| `user_prompt_submit.json` | documented | Claude Code hooks documentation for UserPromptSubmit event (fields: `session_id`, `prompt`, `transcript_path`, `cwd`, `hook_event_name`). |
| `session_start.json` | documented | Claude Code hooks documentation for SessionStart event (fields: `session_id`, `source` ∈ {startup, resume, clear}, `transcript_path`, `cwd`, `hook_event_name`). |
| `session_end.json` | documented | Claude Code hooks documentation for SessionEnd event (fields: `session_id`, `reason`, `transcript_path`, `cwd`, `hook_event_name`). |

## Death tests / acceptance for this task

There is no production code, so death tests are JSON-validity tests:

DT-1: every fixture parses as JSON.
DT-2: every fixture has `_meta._source ∈ {"verified", "documented"}`.
DT-3: every fixture has a `privacy_canary` value AND that value appears in at least one drop-listed field of `payload`.
DT-4: `_meta._secondsight_event_type` ∈ EventType enum values.
DT-5: `_meta._claude_code_hook_event_name` ∈ {`PreToolUse`, `PostToolUse`, `UserPromptSubmit`, `SessionStart`, `SessionEnd`} (P1 floor).

These are implemented in `tests/adapters/test_fixtures.py` (created in this task). The same test file is then re-used by task-4 for `expected_partial_event_data` round-trip assertions.

## Implementation steps

- [ ] STEP 0
- [ ] Write fixture validation test (DT-1..DT-5) → red
- [ ] Author each fixture with `_meta`, `payload`, `expected_partial_event_data`, `privacy_canary`
- [ ] Run fixture validation tests → green
- [ ] Author `_README.md` with drift policy ("if Claude Code v2 hook protocol ships, regenerate fixtures from real captures; do not edit `expected_partial_event_data` to match new format without updating `_source`")

## Acceptance for this task

- All 5 P1-floor fixtures present and valid
- DT-1..DT-5 pass
- `_README.md` documents drift policy
