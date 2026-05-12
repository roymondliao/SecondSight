"""ClaudeCodeAdapter — Claude Code v1.x hook payload → SecondSight PartialEvent.

Per SD §3.7.4 (drop rules) + §4.2 (adapter contract) + §4.3 (Claude Code
mapping). Translates Claude Code hook stdin payloads (wrapped in
HookEnvelope by the bash hook script) into PartialEvents that
SessionTracker.bind() finalises into Events.

Privacy contract (SD §3.7.4, plan §5):
    `tool_input` content, `tool_response` content, and raw `prompt` text are
    NEVER stored in Event.data. Only metadata (sizes, types, paths) flows
    through. The drop_list (DROP_LIST) is declarative — adding a key to
    `data` whose source path is in DROP_LIST requires removing the source
    path from DROP_LIST in the same commit, making accidental leakage
    visible at PR review time. The privacy canary fixtures
    (tests/fixtures/claude_code/*.json) plus DT-3 / DT-6 in
    tests/adapters/test_claude_code.py turn this contract into a runnable
    test.

Silent-failure surface this module deliberately closes:
    - `tool_input.command` raw string copied into data: DT-3 (privacy
      canary) + DT-6 (generalised drop_list assertion) catch it per fixture.
    - `session_id` duplicated into data (denormalises SD §3.7.5 column
      shape): DT-3 fires because session_id is the canary value in
      session_start / session_end fixtures.
    - Misrouted POST (e.g. `/hook/tool_use_start` carrying a `SessionEnd`
      body): `normalize()` cross-checks `hook_event_name` against the
      caller-supplied `event_type` and refuses to silently normalise across
      event types.
    - `inject_hint` returning "" via accidental override: DT-7 asserts the
      ABC's loud-failure default still fires.
    - `supports()` ↔ `supported_event_types()` skew: DT-8 verifies they
      agree across the P1 floor.
    - `inject_convention` returning "" for non-empty instruction: DT-10
      asserts that a convention with content always produces non-empty output.

Out of P1 scope (plan §8): `Stop`, `SubagentStop`, `Notification`,
`PreCompact`, `thinking`, `sub_agent_*`, `task_*`. These are unverified or
deferred — `supports()` returns False for them so the registry routes them
to NoAdapterError instead of silently dispatching here.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from secondsight.adapters.base import AgentAdapter
from secondsight.api.ingress import project_id_from_cwd
from secondsight.api.schemas import IngressEnvelope
from secondsight.event import EventType
from secondsight.feedback.convention import Convention
from secondsight.feedback.hint import Hint
from secondsight.observation.tracker import PartialEvent

_AGENT_NAME = "claude_code"

# Plan §7 G1: Claude Code hook event_name → SecondSight EventType.
# P1 floor only — out-of-floor types are deferred (plan §8).
_HOOK_TO_EVENT_TYPE: dict[str, EventType] = {
    "PreToolUse": EventType.TOOL_USE_START,
    "PostToolUse": EventType.TOOL_USE_END,
    "UserPromptSubmit": EventType.USER_PROMPT,
    "SessionStart": EventType.SESSION_START,
    "SessionEnd": EventType.SESSION_END,
}

# Reverse lookup: normalize() dispatches by EventType (the SecondSight
# canonical), but cross-checks hook_event_name (the Claude Code stdin field)
# to detect misrouted POSTs. Built once at import time.
_EVENT_TYPE_TO_HOOK: dict[EventType, str] = {et: hook for hook, et in _HOOK_TO_EVENT_TYPE.items()}

# Declarative drop_list (SD §3.7.4 + plan §5).
#
# Each entry is a dotted path INTO `envelope.payload` (i.e. into the Claude
# Code hook stdin JSON). The raw value at that path is NEVER copied into
# Event.data; only derived metadata (length, type) flows through. DT-6
# enforces this for every fixture: if a regression copies the raw value
# into data, the canary value (placed at one of these paths in fixtures)
# surfaces in `Event.data` JSON serialisation and the test fails.
#
# Adding a path here is FREE; removing one requires explicit code review.
# If `data` ever needs to carry one of these raw values, the developer must
# remove the path from DROP_LIST in the same commit — otherwise the privacy
# canary fixture detects the leak.
DROP_LIST: frozenset[str] = frozenset(
    {
        "tool_input.command",  # Bash: length only
        "tool_input.content",  # Write: length only
        "tool_input.old_string",  # Edit: length only
        "tool_input.new_string",  # Edit: length only
        "tool_response.output",  # PostToolUse: len(str(...)) only
        "tool_response.error",  # PostToolUse: type only, no message
        "prompt",  # UserPromptSubmit: length only
    }
)


def _action_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Extract envelope-level metadata that every event type carries.

    `transcript_path` and `cwd` are stored as `data.action_metadata` because
    they are file paths (metadata, not content per SD §3.7.4). Missing keys
    are silently omitted: the adapter does not synthesise placeholder strings,
    which would corrupt downstream queries that filter on path equality.
    """
    out: dict[str, Any] = {}
    for key in ("transcript_path", "cwd"):
        if key in payload:
            out[key] = payload[key]
    return out


def _tool_input_metadata(tool_input: Mapping[str, Any]) -> dict[str, Any]:
    """Compute drop_list-compliant size metadata from a tool_input mapping.

    Shared by PreToolUse and PostToolUse — both surface the same tool_input
    shape but differ in whether tool_response is present. Keeping this
    helper in one place ensures both event types apply the drop_list
    consistently; a regression that adds a new tool_input field must update
    BOTH the helper and DROP_LIST or DT-6 trips.
    """
    out: dict[str, Any] = {}
    if "command" in tool_input:
        out["command_length"] = len(str(tool_input["command"]))
    if "content" in tool_input:
        out["content_size"] = len(str(tool_input["content"]))
    if "old_string" in tool_input:
        out["old_size"] = len(str(tool_input["old_string"]))
    if "new_string" in tool_input:
        out["new_size"] = len(str(tool_input["new_string"]))
    return out


def _normalize_pre_tool_use(payload: Mapping[str, Any]) -> dict[str, Any]:
    """PreToolUse → tool_use_start. Tool-specific input dropped, sizes only."""
    tool_name = payload.get("tool_name")
    if not tool_name:
        raise ValueError("ClaudeCodeAdapter: PreToolUse payload missing required field 'tool_name'")
    metadata = _action_metadata(payload)
    tool_input = payload.get("tool_input") or {}
    if isinstance(tool_input, Mapping):
        metadata.update(_tool_input_metadata(tool_input))

    data: dict[str, Any] = {"tool_name": tool_name}
    if isinstance(tool_input, Mapping) and "file_path" in tool_input:
        # action_target: file_path is a path, not content (plan §5).
        data["action_target"] = tool_input["file_path"]
    if metadata:
        data["action_metadata"] = metadata
    return data


def _normalize_post_tool_use(payload: Mapping[str, Any]) -> dict[str, Any]:
    """PostToolUse → tool_use_end. Tool response content dropped; sizes/types kept.

    success/error semantics: presence of `tool_response.error` denotes
    failure (success=False, error_type=type-name); absence denotes success
    unless `tool_response.success=False` is provided explicitly. exit_code
    is intentionally NOT folded into success here — plan §5 lists only
    output_size and error_type; conflating exit_code would change the
    contract without an SD update. (Recorded in scar.)
    """
    tool_name = payload.get("tool_name")
    if not tool_name:
        raise ValueError(
            "ClaudeCodeAdapter: PostToolUse payload missing required field 'tool_name'"
        )
    metadata = _action_metadata(payload)
    tool_input = payload.get("tool_input") or {}
    if isinstance(tool_input, Mapping):
        metadata.update(_tool_input_metadata(tool_input))

    data: dict[str, Any] = {"tool_name": tool_name}
    if isinstance(tool_input, Mapping) and "file_path" in tool_input:
        data["action_target"] = tool_input["file_path"]

    tool_response = payload.get("tool_response") or {}
    if isinstance(tool_response, Mapping):
        if "output" in tool_response:
            data["output_size"] = len(str(tool_response["output"]))
        if "error" in tool_response:
            err = tool_response["error"]
            data["error_type"] = type(err).__name__ if err is not None else "NoneType"
            data["success"] = False
        else:
            if "success" in tool_response:
                data["success"] = bool(tool_response["success"])
            else:
                data["success"] = True

    if metadata:
        data["action_metadata"] = metadata
    return data


def _normalize_user_prompt_submit(payload: Mapping[str, Any]) -> dict[str, Any]:
    """UserPromptSubmit → user_prompt. Raw prompt text dropped; length only."""
    metadata = _action_metadata(payload)
    if "prompt" in payload:
        metadata["prompt_length"] = len(str(payload["prompt"]))
    return {"action_metadata": metadata} if metadata else {}


def _normalize_session_start(payload: Mapping[str, Any]) -> dict[str, Any]:
    """SessionStart → session_start. session_id routes to column, not data
    (per fixtures/claude_code/_README.md "Session-event canary rationale")."""
    metadata = _action_metadata(payload)
    if "source" in payload:
        metadata["source"] = payload["source"]
    return {"action_metadata": metadata} if metadata else {}


def _normalize_session_end(payload: Mapping[str, Any]) -> dict[str, Any]:
    """SessionEnd → session_end. session_id routes to column, not data."""
    metadata = _action_metadata(payload)
    if "reason" in payload:
        metadata["reason"] = payload["reason"]
    return {"action_metadata": metadata} if metadata else {}


# Per-EventType data-builder dispatch.
#
# Adding a new event type requires registering both _HOOK_TO_EVENT_TYPE (to
# publish capability via supported_event_types) AND this table (to actually
# normalise). DT-8 catches `_HOOK_TO_EVENT_TYPE` updated but supports() not
# updated; missing _DATA_BUILDERS entry surfaces as a ValueError at runtime
# (also asserted by `test_unsupported_event_type_raises_value_error`).
_DATA_BUILDERS: dict[EventType, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    EventType.TOOL_USE_START: _normalize_pre_tool_use,
    EventType.TOOL_USE_END: _normalize_post_tool_use,
    EventType.USER_PROMPT: _normalize_user_prompt_submit,
    EventType.SESSION_START: _normalize_session_start,
    EventType.SESSION_END: _normalize_session_end,
}

# Startup-time consistency guard (DT-9 enforces this invariant from tests).
# Fires at import, not at first request — divergence is named explicitly so
# the operator can fix it in seconds rather than tracing a 422 in production.
assert set(_HOOK_TO_EVENT_TYPE.values()) == set(_DATA_BUILDERS.keys()), (
    "_HOOK_TO_EVENT_TYPE / _DATA_BUILDERS divergence: "
    f"hook→event mapping publishes {set(_HOOK_TO_EVENT_TYPE.values()) - set(_DATA_BUILDERS.keys())!r} "
    f"that have no _DATA_BUILDERS entry, AND/OR _DATA_BUILDERS has builders for "
    f"{set(_DATA_BUILDERS.keys()) - set(_HOOK_TO_EVENT_TYPE.values())!r} that no hook publishes."
)


class ClaudeCodeAdapter(AgentAdapter):
    """Claude Code v1.x hook payload → SecondSight PartialEvent.

    See module docstring for the privacy contract. `supported_event_types()`
    publishes the P1 floor (5 event types); `supports()` answers True only
    for `agent="claude_code"` (snake_case per plan §7 G3) AND a published
    event_type. DT-8 enforces the supports() ↔ supported_event_types()
    consistency that AdapterRegistry's runtime guard alone cannot.

    The class deliberately does NOT override inject_convention / inject_hint
    — the ABC's loud-failure NotImplementedError defaults are the contract
    until Phase 2 (GUR-104) ships Convention injection runtime.
    """

    def supports(self, agent: str, event_type: str) -> bool:
        if agent != _AGENT_NAME:
            return False
        return event_type in self.supported_event_types()

    def supported_event_types(self) -> set[str]:
        return {et.value for et in _HOOK_TO_EVENT_TYPE.values()}

    _MAX_INSTRUCTION_CHARS = 1000

    def inject_hint(self, hint: Hint) -> str:  # type: ignore[override]
        """Pass-through stub for hint injection (GUR-108, P3B-5).

        Returns empty string. When the hint engine ships, this will
        format hints for Claude Code system prompt injection.
        """
        return ""

    def inject_convention(self, convention: Convention) -> str:  # type: ignore[override]
        """Format a convention for Claude Code system prompt injection.

        Returns a single-line bullet point. Empty instruction → empty string.
        Sanitization: collapses internal newlines to spaces, strips leading/
        trailing whitespace, and truncates to _MAX_INSTRUCTION_CHARS to bound
        blast radius of any malformed instruction content.
        """
        if not convention.instruction:
            return ""
        sanitized = " ".join(convention.instruction.split())
        if len(sanitized) > self._MAX_INSTRUCTION_CHARS:
            sanitized = sanitized[: self._MAX_INSTRUCTION_CHARS] + "…"
        return f"- {sanitized}"

    def normalize(self, envelope: IngressEnvelope, event_type: str) -> PartialEvent:
        # Envelope-level invariants. Pydantic enforces session_id/event_id
        # min_length=1 at the API boundary, but we re-check here so the
        # adapter is safe for callers that bypass FastAPI validation
        # (e.g. unit tests using HookEnvelope.model_construct, or future
        # internal callers). Defence-in-depth — see task-1 scar carry-forward
        # SF-3 (envelope schema relaxation must not silently produce empty
        # required fields).
        if not envelope.event_id:
            raise ValueError("ClaudeCodeAdapter: envelope missing required field 'event_id'")

        try:
            et = EventType(event_type)
        except ValueError as exc:
            raise ValueError(
                f"ClaudeCodeAdapter: unsupported event_type {event_type!r} — "
                f"supported: {sorted(self.supported_event_types())}"
            ) from exc

        builder = _DATA_BUILDERS.get(et)
        if builder is None:
            raise ValueError(
                f"ClaudeCodeAdapter: no data builder for event_type "
                f"{event_type!r} (out of P1 floor — plan §8)"
            )

        payload: Mapping[str, Any] = envelope.payload or {}
        # hook_event_name cross-check: the Claude Code hook stdin field that
        # names which hook fired. Caller-supplied event_type drives dispatch
        # (it is the route-param canonical), but a hook_event_name mismatch
        # means the route and body disagree — refuse to silently normalise.
        hook_event_name = payload.get("hook_event_name")
        if not hook_event_name:
            raise ValueError("ClaudeCodeAdapter: payload missing required field 'hook_event_name'")
        expected_hook = _EVENT_TYPE_TO_HOOK[et]
        if hook_event_name != expected_hook:
            raise ValueError(
                f"ClaudeCodeAdapter: hook_event_name {hook_event_name!r} does "
                f"not match dispatched event_type {event_type!r} (expected hook "
                f"{expected_hook!r}). Route/payload mismatch — refusing to "
                f"silently normalise across event types."
            )

        data = builder(payload)
        session_id = envelope.session_id or payload.get("session_id")
        if not session_id:
            raise ValueError(
                "ClaudeCodeAdapter: payload missing required field 'session_id'"
            )

        project_id = envelope.project_id
        if not project_id:
            cwd = payload.get("cwd")
            if not cwd:
                raise ValueError(
                    "ClaudeCodeAdapter: payload missing required field 'cwd'"
                )
            project_id = project_id_from_cwd(str(cwd))
        return PartialEvent(
            id=envelope.event_id,
            session_id=str(session_id),
            project_id=project_id,
            event_type=et,
            timestamp=envelope.timestamp,
            sequence_number=envelope.sequence_number,
            data=data,
        )


__all__ = ["DROP_LIST", "ClaudeCodeAdapter"]
