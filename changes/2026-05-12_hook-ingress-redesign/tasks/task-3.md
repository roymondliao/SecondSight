# Task 3: Adapter and Server Refactor

## Goal

Move project/session derivation into adapters and keep `SessionTracker` unchanged except for consuming the new adapter output.

## Files

- Modify: `src/secondsight/adapters/base.py`
- Modify: `src/secondsight/adapters/claude_code.py`
- Modify: `src/secondsight/api/hooks.py`
- Modify: `tests/adapters/test_claude_code.py`
- Modify: `tests/observation/test_tracker.py` only if adapter/tracker seams need fixture updates

## Claude Code rules

For Claude Code:

- `session_id` comes from raw `payload.session_id`
- `project_id` comes from derived `cwd`
- `hook_event_name` must still match route `event_type`
- `event_id`, `timestamp`, `sequence_number` are forwarded from ingress metadata

## Project ID rule

Default canonicalization:

- start from `cwd`
- extract basename
- slugify to safe filesystem ID

Recommended hardening:

- append short hash of full `cwd` if collision handling is desired in the same change

## Required invariants

- adapter never guesses `agent`
- tracker still does not generate `sequence_number`
- repository uniqueness semantics stay unchanged
- event ordering remains `sequence_number` ascending

## Death tests

1. Missing raw `session_id` is adapter error, not schema error.
2. Missing raw `cwd` on events that need `project_id` is adapter error.
3. Two different `cwd` values with the same basename do not silently collide if hash hardening is enabled.
