# Codex Hook Fixture Contract

These fixtures are pinned to real Codex hook stdin captured on `2026-05-13`
from `codex-cli 0.130.0`.

Verified hook surface:

- `SessionStart`
- `UserPromptSubmit`
- `PreToolUse`
- `PostToolUse`
- `Stop`

Load-bearing contract:

- `UserPromptSubmit.prompt` is top-level input and must survive as
  `expected_partial_event_data.action_metadata.prompt_text`.
- `PreToolUse` and `PostToolUse` are top-level payloads, not nested
  `hook_event` objects.
- `PostToolUse.tool_response` is a raw string in hook stdin and must stay out
  of normalized `Event.data`.
- `Stop.last_assistant_message` is a raw string in hook stdin and must stay
  out of normalized `Event.data`.

Fixture mutation policy:

- The captured payload shape, field names, and non-sensitive routing fields stay
  aligned to the verified local capture.
- Every verified fixture must carry structured provenance in `_meta`:
  `_capture_date`, `_capture_file`, and `_raw_fields_substituted`.
- Privacy boundary fields may be replaced with canaries when the test needs to
  prove SecondSight drops the raw value. Current canary targets include
  `tool_response`, `last_assistant_message`, selected `session_id` values, and
  `tool_input.command`.
- If a verified fixture contains a substituted raw field, the fixture's
  `_capture_origin` and `_raw_fields_substituted` must say so explicitly. The
  verified claim applies to the payload shape and field contract, not to
  replaying sensitive raw strings.

Refresh rules:

- Refresh from real `~/.codex` hook capture files, not from rollout JSONL.
- Keep PascalCase `hook_event_name` values exactly as emitted by Codex.
- If Codex changes the hook schema, update this README and the fixture metadata
  in the same change as the adapter/tests.
