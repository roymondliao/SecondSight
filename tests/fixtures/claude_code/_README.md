# Claude Code Hook Payload Fixtures (P1-9-fixtures)

These fixtures are the empirical contract that `ClaudeCodeAdapter` (task-4)
and the integration test (task-5) are verified against. They exist so the
adapter is never trained on **invented** payloads — a kill condition named
in `changes/2026-05-04_phase1-adapters/problem-autopsy.md`.

This README is **load-bearing**: it documents the rules a future maintainer
must follow when regenerating these fixtures. Read it before editing any
JSON file in this directory.

## Fixture inventory (P1 floor)

| Fixture | `_source` | Hook event | SecondSight `EventType` |
|---|---|---|---|
| `pre_tool_use_bash.json` | `verified` | `PreToolUse` | `tool_use_start` |
| `post_tool_use.json` | `documented` | `PostToolUse` | `tool_use_end` |
| `user_prompt_submit.json` | `documented` | `UserPromptSubmit` | `user_prompt` |
| `session_start.json` | `documented` | `SessionStart` | `session_start` |
| `session_end.json` | `documented` | `SessionEnd` | `session_end` |

Out of P1 scope (deliberate non-goal — no verified source yet):
`Stop`, `SubagentStop`, `Notification`, `PreCompact`, `thinking`,
`sub_agent_*`, `task_*`. See plan §8.

## Current provenance status

As of 2026-05-12, these fixtures were refreshed against the local Claude Code
state under `~/.claude-personal`, using the real SecondSight session transcript
at:

- `~/.claude-personal/projects/-Users-yuyu-liao-vicone-SecondSight/3756b281-8769-424f-bb1a-aa3fe1aeecc9.jsonl`

What is locally aligned today:

- `session_id`
- `cwd`
- `transcript_path`
- `permission_mode` on session hooks
- `prompt` style and shape for `UserPromptSubmit`
- `tool_input` / `tool_response` style and shape for Bash tool events

What is still not a raw local hook capture:

- `session_end.json` remains docs-aligned for the `reason` field because no
  local raw `SessionEnd` stdin payload was available.
- `documented` fixtures may still contain wrapper fields whose exact stdin
  shape is sourced from Claude Code hook docs, even when the concrete values
  were copied from the local transcript.

## File schema

Every fixture is a JSON object with exactly four top-level keys:

```jsonc
{
  "_meta": {
    "_source": "verified" | "documented",
    "_capture_origin": "human-readable provenance string",
    "_claude_code_hook_event_name": "PreToolUse" | ...,  // P1 floor only
    "_secondsight_event_type": "tool_use_start" | ...,   // EventType enum value
    "_privacy_canary_field": "dotted.path.into.payload",
    "_privacy_canary_field_rationale": "..."   // optional; required for session_*
  },
  "payload": { /* verbatim Claude Code hook stdin JSON */ },
  "expected_partial_event_data": { /* what ClaudeCodeAdapter.normalize().data must equal */ },
  "privacy_canary": "PRIVACY_CANARY_DO_NOT_STORE"
}
```

`expected_partial_event_data` is the AC-5 source of truth: `task-4` asserts
that `ClaudeCodeAdapter().normalize(envelope_from(fixture.payload), event_type).data`
equals this dict, modulo additive keys explicitly allowed by the adapter
contract.

## `_source: verified` vs `documented`

- **`verified`** means the relevant field shape was empirically observed on
  this machine from the local `~/.claude-personal` transcript. Today that is
  only `pre_tool_use_bash.json`, where the `tool_input.command` path and Bash
  invocation style are backed by a real tool-use entry from the local session.
  Do not extrapolate that label to every sibling field in the same fixture;
  wrapper fields can still be docs-shaped.

- **`documented`** means the fixture still depends on the public Claude Code
  hooks contract for part of its shape, even if the concrete values were
  aligned to the local `~/.claude-personal` session. `session_start.json`,
  `user_prompt_submit.json`, and `post_tool_use.json` are in this bucket:
  they use real local values, but the top-level hook envelope shape is still
  treated as documentation-backed rather than raw stdin-captured. When a real
  raw hook capture becomes available, promote the fixture to `verified` only
  after updating `_capture_origin`.

## Privacy canary contract

Every fixture sets `privacy_canary: "PRIVACY_CANARY_DO_NOT_STORE"` and
plants the same string at the path declared in `_meta._privacy_canary_field`.
The adapter test asserts that the canary value never appears anywhere in
the JSON-serialized `Event.data`. If `ClaudeCodeAdapter`'s drop logic ever
regresses and starts copying a drop-listed raw value into `data`, the canary
surfaces and the test fails.

### Canary placement rules

| Fixture | Canary path | Why this path is "drop-from-data" |
|---|---|---|
| `pre_tool_use_bash.json` | `tool_input.command` | Plan §5: raw command DROPPED, only `len(...)` kept as `action_metadata.command_length`. |
| `post_tool_use.json` | `tool_response.output` | Plan §5: raw output DROPPED, only `len(str(...))` kept as `output_size`. |
| `user_prompt_submit.json` | `permission_mode` | SD §3.7.4 + ADR-005: prompt_text is STORED completely; permission_mode is ignored by the adapter and MUST NOT reach `data`. |
| `session_start.json` | `permission_mode` | See "Session-event canary rationale" below. |
| `session_end.json` | `permission_mode` | See "Session-event canary rationale" below. |

### Session-event canary rationale

`session_start` and `session_end` payloads have **no** `§5`-explicit
drop-listed content fields — `transcript_path` and `cwd` are *stored* as
`action_metadata`, and `session_id` is intentionally routed to the top-level
`PartialEvent.session_id` column. To keep the fixtures close to a real local
session while still having a meaningful canary, the canary lives in
`payload.permission_mode`, which is currently ignored by the adapter:

- `permission_mode` is present on real Claude Code session hooks.
- `permission_mode` MUST NOT appear inside `data` under the current contract.
- A future regression that starts persisting raw permission state into
  `data` therefore trips the canary without forcing the fixture to fake an
  entire session_id.

A regression that copies `permission_mode` into `data` trips the canary
the same way a `permission_mode` leak would on `user_prompt_submit.json`.

The `_meta._privacy_canary_field_rationale` field on each session-event
fixture restates this so a future maintainer who deletes this README still
has the reasoning inline.

## Drift policy

This is a strict policy. Treat it as a load-bearing invariant.

1. **Hook protocol drift.** When Claude Code ships a v2 hook protocol (or
   adds new fields to v1), do **not** edit `expected_partial_event_data` to
   match the new format in-place. Instead:
   - capture the new payload from a real session,
   - update `_capture_origin` with the date and capture method,
   - if previously `documented`, promote to `verified` only if the field
     shape was empirically observed,
   - update `expected_partial_event_data` and re-run the full test suite,
   - record the protocol change in `changes/.../scar-reports/` so the
     decision is visible to reviewers.

2. **Canary placement rewrites.** If `ClaudeCodeAdapter`'s drop_list
   changes (plan §5 expands or contracts), update `_meta._privacy_canary_field`
   in each affected fixture. The DT-3 death test checks placement is
   internally consistent; it cannot tell that a placement is *meaningful*.
   That is the maintainer's job, and the rationale field exists for it.

3. **Adding new fixtures.** A new fixture must:
   - declare a real `_source`,
   - declare a `_privacy_canary_field` whose value contains the canary
     string AND whose semantics is "raw value MUST NOT reach `data`",
   - extend `tests/adapters/test_fixtures.py::P1_HOOK_EVENT_NAMES` if it
     introduces a new hook event (otherwise DT-5 fails),
   - update this README's inventory table.

4. **Removing fixtures.** The P1-floor coverage test
   (`test_p1_floor_fully_covered`) requires exactly one fixture per P1
   hook event. Removing a fixture will fail that test until the floor set
   is amended in code. This is intentional: it forces the scope decision
   to land in version control.

## Where the death tests live

`tests/adapters/test_fixtures.py` — DT-1..DT-5 plus a P1-coverage guard.
Run with:

```sh
source .venv/bin/activate
pytest tests/adapters/test_fixtures.py -v
```

`task-4` (`ClaudeCodeAdapter`) reuses these fixtures for AC-5 round-trip
assertions and AC-6 privacy canary assertions. Do not change fixtures
without re-running both this file and `tests/adapters/test_claude_code.py`
once it lands.

## SD references

- `SD §3.7.4` — drop rules (drives `_meta._privacy_canary_field` semantics).
- `SD §3.7.5` — `Event` column shape (drives session-event canary placement).
- `SD §4.2` — adapter contract.
- Plan `§5` — drop_list table this README cross-references.
- Plan `§7 G1` — hook event → `EventType` mapping (drives DT-4/DT-5).
