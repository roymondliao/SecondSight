# Codex Fixture Audit

Date: 2026-05-13

## Question

Do `tests/fixtures/codex` payloads match the real local Codex data under `~/.codex` for these hook events?

- session start
- user prompt
- pre-tool-use
- post-tool-use
- session end

## Local Evidence

Observed local Codex state:

- `~/.codex/hooks.json` registers `SessionStart`, `UserPromptSubmit`, and `Stop` only.
- `~/.codex/hooks.json` does not register `PreToolUse` or `PostToolUse`.
- Local `codex --version` reports `codex-cli 0.130.0`.
- The local Codex binary contains hook schema/event strings for `PreToolUse`, `PostToolUse`, `SessionStart`, `UserPromptSubmit`, and `Stop`.
- `~/.codex/sessions/2026/05/13/rollout-2026-05-13T15-29-49-019e203d-ea9f-72e3-ae17-e029a7a7cd3b.jsonl` contains session transcript events: `session_meta`, `event_msg`, `response_item`, `turn_context`, and `compacted`.
- The rollout JSONL contains user prompts as `response_item.payload.role == "user"` / `content[].text`.
- The rollout JSONL contains tool activity as `response_item.payload.type == "function_call"` and `function_call_output`.
- The rollout JSONL is not the same thing as the stdin payload delivered to a configured hook command.

Observed local Superset wrapper:

- `/Users/yuyu_liao/.superset/bin/codex` starts Codex with `--enable codex_hooks -c 'notify=["bash","/Users/yuyu_liao/.superset/hooks/notify.sh"]'`.
- The wrapper also watches the TUI session log and emits synthetic `Start` / `PermissionRequest` notifications to Superset.
- `/Users/yuyu_liao/.superset/hooks/notify.sh` treats Codex `type` values like `task_started`, `task_complete`, and approval requests as notification events, not as SecondSight observation payloads.

Observed upstream Codex source state:

- `openai/codex` declares hook event names including `PreToolUse`, `PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `SessionStart`, `UserPromptSubmit`, and `Stop`.
- `PreToolUse` and `PostToolUse` are matcher-aware hook events.
- `PreToolUse` stdin includes `session_id`, `turn_id`, `transcript_path`, `cwd`, `hook_event_name`, `model`, `permission_mode`, `tool_name`, `tool_input`, and `tool_use_id`.
- `PostToolUse` stdin includes the same tool identity/input context plus `tool_response`.
- Codex tool hook coverage is handler-dependent: a tool only fires these hooks if its handler provides pre/post hook payloads.

Observed SecondSight installer state:

- `src/secondsight/installer/codex_hooks.py` maps `tool_use_end` to `PostToolUse`.
- It does not map `tool_use_start` to `PreToolUse`.
- `tests/installer/test_codex_hooks.py` asserts fresh Codex install writes `PostToolUse`, `SessionStart`, `Stop`, and `UserPromptSubmit`, but does not assert `PreToolUse`.

## Event Findings

### Session Start

Fixture: `tests/fixtures/codex/session_start.json`

The fixture uses a normalized payload shape:

```json
{
  "session_id": "...",
  "cwd": "...",
  "triggered_at": "...",
  "hook_event_name": "session_start"
}
```

The local `~/.codex/hooks.json` event key is `SessionStart`, not `session_start`. The local rollout has equivalent session context under `session_meta`, but does not prove the hook stdin payload has the fixture shape.

Status: partially aligned by intent, not verified as real hook stdin.

### User Prompt

Fixture: `tests/fixtures/codex/user_prompt_submit.json`

The fixture says Codex does not expose raw prompt text and expects only `cwd` in `action_metadata`.

The local rollout contradicts the "Codex has no prompt data" conclusion at the session-data level: user prompts are present in `response_item` records. However, no captured real hook stdin was found proving whether `UserPromptSubmit` stdin includes `prompt`.

Status: fixture is suspect. At minimum, its `_capture_origin` overclaims verification. It used rollout data plus hook registration to infer a hook payload that was not actually captured.

### Pre Tool Use

There is no `tests/fixtures/codex/pre_tool_use.json`.

The local `~/.codex/hooks.json` has no `PreToolUse` registration. Current `CodexAdapter` also explicitly does not support `tool_use_start`.

Upstream Codex supports `PreToolUse` as a hook event. Therefore, the lack of `PreToolUse` registration in SecondSight is not justified by "Codex does not support it"; it is a SecondSight installer/adapter gap. Actual trigger coverage still needs runtime capture because Codex only fires tool hooks for tool handlers that opt in.

Status: missing fixture and likely SecondSight bug. Needs registration plus real stdin capture.

### Post Tool Use

Fixture: `tests/fixtures/codex/post_tool_use.json`

The fixture models a `post_tool_use` hook payload with nested `hook_event` fields like `tool_name`, `tool_kind`, `tool_input`, `success`, `duration_ms`, and `output_preview`.

The local `~/.codex/hooks.json` does not register `PostToolUse`. The local rollout has tool activity, but its real transcript shape is `response_item.function_call` / `function_call_output`, not the nested `hook_event` shape in the fixture.

Upstream Codex supports `PostToolUse` as a hook event, and SecondSight installer code intends to register it. Its absence from the current local `~/.codex/hooks.json` means the local install is stale, failed, conflicted, or was overwritten by another hook manager. The fixture may be directionally compatible with Codex's expected hook contract, but it is still not verified by a local hook stdin capture.

Status: supported upstream, intended by SecondSight, absent from current local registration. Needs install-path diagnosis plus real stdin capture.

### Session End

Fixture: `tests/fixtures/codex/stop.json`

The fixture uses:

```json
{
  "session_id": "...",
  "cwd": "...",
  "triggered_at": "...",
  "hook_event_name": "stop"
}
```

The local `~/.codex/hooks.json` event key is `Stop`. The local rollout has `task_complete` turn events, not a clear session-end hook stdin payload equivalent to the fixture.

Status: partially aligned by intent, not verified as real hook stdin.

## Conclusion

`tests/fixtures/codex` are not a faithful capture of current local `~/.codex` reality. They mix three different concepts:

- Codex hook registration keys in `~/.codex/hooks.json` such as `SessionStart`, `UserPromptSubmit`, and `Stop`.
- Codex rollout transcript records in `~/.codex/sessions/**/*.jsonl` such as `session_meta`, `response_item.message`, `function_call`, and `function_call_output`.
- A normalized SecondSight adapter payload shape using lower-case `hook_event_name` values such as `session_start`, `user_prompt_submit`, `post_tool_use`, and `stop`.

The highest-risk fixture is `user_prompt_submit.json`: it encodes "no prompt text" as verified behavior, but the local Codex session data does contain prompt text. The missing proof is the actual `UserPromptSubmit` hook stdin payload. The next death-first step should be to capture real Codex hook stdin for `UserPromptSubmit` before finalizing the adapter fix.

Separately, `PreToolUse` should be added to Codex installation and adapter coverage. `PostToolUse` should already be installed by current SecondSight code, so the fact that local `~/.codex/hooks.json` lacks it indicates an install/config drift that should be investigated before trusting any Codex fixture as verified.
