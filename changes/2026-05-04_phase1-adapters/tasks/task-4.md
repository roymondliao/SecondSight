# Task 4: ClaudeCodeAdapter (P1-10)

## Context

Read: `2-plan.md` §1 (decision 6 — verified-vs-documented), §5 (drop_list — authoritative), §6 (AC-4..AC-7), §7 G1 (event-type mapping).

This task implements the first real adapter. It depends on task-1 (ABC) and task-2 (fixtures). Privacy is a first-class concern: the drop_list is enforced by death test #5 from plan §4 (privacy canary).

**Plan refs:** P1-10
**SD refs:** §3.7.4 (drop rules), §4.2 (adapter contract), §4.3 (Claude Code mapping)
**Depends on:** task-1, task-2

## Files

- Create: `src/secondsight/adapters/claude_code.py` — `ClaudeCodeAdapter`
- Create: `tests/adapters/test_claude_code.py` — unit tests + death tests + per-fixture round-trip
- Modify: `src/secondsight/adapters/__init__.py` — export `ClaudeCodeAdapter`
- Modify: `src/secondsight/api/server.py` — register `ClaudeCodeAdapter()` alongside `IdentityAdapter()` in the registry boot

## Public Contract

```python
# adapters/claude_code.py

from secondsight.adapters.base import AgentAdapter
from secondsight.api.schemas import HookEnvelope
from secondsight.event import EventType
from secondsight.observation.tracker import PartialEvent


_HOOK_TO_EVENT_TYPE: dict[str, EventType] = {
    "PreToolUse": EventType.TOOL_USE_START,
    "PostToolUse": EventType.TOOL_USE_END,
    "UserPromptSubmit": EventType.USER_PROMPT,
    "SessionStart": EventType.SESSION_START,
    "SessionEnd": EventType.SESSION_END,
}

_AGENT_NAME = "claude_code"


class ClaudeCodeAdapter(AgentAdapter):
    """Claude Code v1.x hook payload → SecondSight PartialEvent.

    Privacy contract (SD §3.7.4): tool_input/tool_response/prompt content
    is NEVER stored. Only metadata (sizes, types, paths) flows into
    Event.data. See plan §5 drop_list.
    """

    def supports(self, agent: str, event_type: str) -> bool:
        if agent != _AGENT_NAME:
            return False
        return event_type in {e.value for e in _HOOK_TO_EVENT_TYPE.values()}

    def supported_event_types(self) -> set[str]:
        return {e.value for e in _HOOK_TO_EVENT_TYPE.values()}

    def normalize(self, envelope: HookEnvelope, event_type: str) -> PartialEvent:
        # Switch on event_type. Each branch:
        # 1. Validate required hook fields are present (raise ValueError if not)
        # 2. Build data dict per drop_list (length, type, target — never raw content)
        # 3. Return PartialEvent
        ...
```

## Drop_list enforcement (authoritative — plan §5)

| Hook event | Source field | Treatment |
|------------|--------------|-----------|
| PreToolUse / PostToolUse (Bash) | `tool_input.command` | Length only → `data.action_metadata.command_length`. Raw DROPPED. |
| All tool hooks | `tool_input.file_path` | Stored as `data.action_target` (path is metadata). |
| Write | `tool_input.content` | Length only → `data.action_metadata.content_size`. |
| Edit | `tool_input.{old_string, new_string}` | Length only → `data.action_metadata.{old_size, new_size}`. |
| PostToolUse | `tool_response.output` | `len(str(...))` → `data.output_size`. Raw DROPPED. |
| PostToolUse | `tool_response.error` | Type only → `data.error_type`. |
| UserPromptSubmit | `prompt` | Length only → `data.action_metadata.prompt_length`. |
| All | `transcript_path` | Path → `data.action_metadata.transcript_path`. |
| All | `cwd` | Path → `data.action_metadata.cwd`. |

The drop_list is **declarative** in the source — a module-level `DROP_LIST: set[str]` set whose membership is asserted by the privacy death test. Adding a field to `data` that came from a drop-listed source path requires the developer to also remove the source path from `DROP_LIST` (intentional), making accidental leakage detectable.

## Death tests (write red first)

DT-1: Unknown agent — `ClaudeCodeAdapter().supports("nonexistent", ...) is False`.
DT-2: Unknown event_type for known agent — `supports("claude_code", "blarg") is False`.
DT-3: **Privacy canary.** For every fixture in `tests/fixtures/claude_code/*.json`, normalize the payload and assert `"PRIVACY_CANARY_DO_NOT_STORE"` does NOT appear in the resulting `PartialEvent.data` (deep search, not just top-level).
DT-4: Malformed payload — missing `session_id` → `ValueError` whose message names the missing field.
DT-5: Malformed payload — missing `hook_event_name` for a generic shape → `ValueError`.
DT-6: Drop_list / data skew — for every key declared in `DROP_LIST`, that exact source path's raw value MUST NOT appear anywhere in `Event.data` JSON serialization. (Generalized canary.)
DT-7: `inject_hint` raises (inherited from base, but assert here too — guards against override regression).
DT-8: Round-trip fidelity — for every fixture, the produced `PartialEvent.data` deep-equals the fixture's `expected_partial_event_data`.

## Unit tests

- `supported_event_types()` ⊇ {`session_start`, `user_prompt`, `tool_use_start`, `tool_use_end`, `session_end`}.
- Per-event-type happy path with a fixture: `normalize(envelope, "user_prompt")` produces a `PartialEvent` with `event_type=USER_PROMPT`, `data.action_metadata.prompt_length` set.
- `supports(agent_name, event_type)` ↔ `supported_event_types()` — for every value in the latter, the former returns True for `agent="claude_code"`.

## Implementation steps

- [ ] STEP 0
- [ ] Write death tests (DT-1..DT-8) → red
- [ ] Write unit tests → red
- [ ] Implement `_HOOK_TO_EVENT_TYPE`, `_AGENT_NAME`, `DROP_LIST`, `ClaudeCodeAdapter`
- [ ] Per-event-type normalize branches with explicit drop_list application
- [ ] Run all tests → green
- [ ] mypy clean
- [ ] Wire `ClaudeCodeAdapter()` registration in `api/server.py` lifespan startup
- [ ] Re-run hook integration tests (existing) → green

## Acceptance for this task

- AC-4, AC-5, AC-6 pass
- Privacy canary test green for every fixture
- Drop_list assertions in DT-6 green
- Task-4 scar report committed (especially the documented-vs-verified split for non-PreToolUse events)
