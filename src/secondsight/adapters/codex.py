"""CodexAdapter — Codex CLI hook payload → SecondSight PartialEvent.

Translates Codex CLI hook stdin payloads (wrapped in HookEnvelope by the
shared bash hook scripts) into PartialEvents that SessionTracker.bind()
finalises into Events.

Codex CLI hook surface (checked against codex-cli 0.130.0 and upstream
`openai/codex` hook event source):
    PreToolUse, PostToolUse, SessionStart, UserPromptSubmit, Stop

JSONL rollout file parsing is out of scope here — that would be a
separate file-watcher path, not an adapter for the hook API.

Privacy contract:
    Raw tool_input content and raw tool_response content are NEVER stored
    in Event.data. Only derived metadata (for example command_length) is
    preserved. User prompt text is intentionally stored completely at
    `data.action_metadata.prompt_text` because the hook payload is the
    observation source of truth for intent analysis.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Callable

from secondsight.adapters.base import AgentAdapter
from secondsight.api.ingress import project_id_from_cwd
from secondsight.api.schemas import IngressEnvelope
from secondsight.event import EventType
from secondsight.feedback.convention import Convention
from secondsight.feedback.hint import Hint
from secondsight.observation.tracker import PartialEvent

_AGENT_NAME = "codex"

_HOOK_TO_EVENT_TYPE: dict[str, EventType] = {
    "PreToolUse": EventType.TOOL_USE_START,
    "PostToolUse": EventType.TOOL_USE_END,
    "SessionStart": EventType.SESSION_START,
    "UserPromptSubmit": EventType.USER_PROMPT,
    "Stop": EventType.SESSION_END,
}

_EVENT_TYPE_TO_HOOK: dict[EventType, str] = {et: hook for hook, et in _HOOK_TO_EVENT_TYPE.items()}

DROP_LIST: frozenset[str] = frozenset(
    {
        "tool_input.command",
        "tool_response",
        "last_assistant_message",
    }
)


def _action_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("cwd", "transcript_path"):
        if key in payload:
            out[key] = payload[key]
    return out


def _tool_input_metadata(tool_input: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "command" in tool_input:
        out["command_length"] = len(str(tool_input["command"]))
    return out


def _normalize_pre_tool_use(payload: Mapping[str, Any]) -> dict[str, Any]:
    """PreToolUse → tool_use_start. Raw tool_input stays dropped."""
    tool_name = payload.get("tool_name")
    if not tool_name:
        raise ValueError("CodexAdapter: PreToolUse payload missing required field 'tool_name'")

    metadata = _action_metadata(payload)
    tool_input = payload.get("tool_input") or {}
    if isinstance(tool_input, Mapping):
        metadata.update(_tool_input_metadata(tool_input))

    data: dict[str, Any] = {"tool_name": tool_name}
    if "turn_id" in payload:
        data["turn_id"] = payload["turn_id"]
    if "tool_use_id" in payload:
        data["tool_use_id"] = payload["tool_use_id"]

    if metadata:
        data["action_metadata"] = metadata
    return data


def _normalize_post_tool_use(payload: Mapping[str, Any]) -> dict[str, Any]:
    """PostToolUse → tool_use_end. Raw tool_input/tool_response stay dropped."""
    tool_name = payload.get("tool_name")
    if not tool_name:
        raise ValueError("CodexAdapter: PostToolUse payload missing required field 'tool_name'")

    metadata = _action_metadata(payload)
    tool_input = payload.get("tool_input") or {}
    if isinstance(tool_input, Mapping):
        metadata.update(_tool_input_metadata(tool_input))

    data: dict[str, Any] = {"tool_name": tool_name}
    if "turn_id" in payload:
        data["turn_id"] = payload["turn_id"]
    if "tool_use_id" in payload:
        data["tool_use_id"] = payload["tool_use_id"]

    if metadata:
        data["action_metadata"] = metadata
    return data


def _normalize_session_start(payload: Mapping[str, Any]) -> dict[str, Any]:
    """SessionStart → session_start."""
    metadata = _action_metadata(payload)
    if "source" in payload:
        metadata["source"] = payload["source"]
    return {"action_metadata": metadata} if metadata else {}


def _normalize_user_prompt_submit(payload: Mapping[str, Any]) -> dict[str, Any]:
    """UserPromptSubmit → user_prompt. Prompt text is preserved completely."""
    metadata = _action_metadata(payload)
    if "prompt" in payload:
        metadata["prompt_text"] = str(payload["prompt"])
    return {"action_metadata": metadata} if metadata else {}


def _normalize_stop(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Stop → session_end."""
    metadata = _action_metadata(payload)
    return {"action_metadata": metadata} if metadata else {}


_DATA_BUILDERS: dict[EventType, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    EventType.TOOL_USE_START: _normalize_pre_tool_use,
    EventType.TOOL_USE_END: _normalize_post_tool_use,
    EventType.SESSION_START: _normalize_session_start,
    EventType.USER_PROMPT: _normalize_user_prompt_submit,
    EventType.SESSION_END: _normalize_stop,
}

assert set(_HOOK_TO_EVENT_TYPE.values()) == set(_DATA_BUILDERS.keys()), (
    "_HOOK_TO_EVENT_TYPE / _DATA_BUILDERS divergence: "
    f"hook→event mapping publishes {set(_HOOK_TO_EVENT_TYPE.values()) - set(_DATA_BUILDERS.keys())!r} "
    f"without builders, AND/OR _DATA_BUILDERS has builders for "
    f"{set(_DATA_BUILDERS.keys()) - set(_HOOK_TO_EVENT_TYPE.values())!r} that no hook publishes."
)


class CodexAdapter(AgentAdapter):
    """Codex CLI hook payload → SecondSight PartialEvent.

    Supports the currently exposed Codex hook event surface:
    PreToolUse, PostToolUse, SessionStart, UserPromptSubmit, Stop.
    """

    def supports(self, agent: str, event_type: str) -> bool:
        if agent != _AGENT_NAME:
            return False
        return event_type in self.supported_event_types()

    def supported_event_types(self) -> set[str]:
        return {et.value for et in _HOOK_TO_EVENT_TYPE.values()}

    _MAX_INSTRUCTION_CHARS = 1000

    def inject_hint(self, hint: Hint) -> str:  # type: ignore[override]
        return ""

    def inject_convention(self, convention: Convention) -> str:  # type: ignore[override]
        if not convention.instruction:
            return ""
        sanitized = " ".join(convention.instruction.split())
        if len(sanitized) > self._MAX_INSTRUCTION_CHARS:
            sanitized = sanitized[: self._MAX_INSTRUCTION_CHARS] + "…"
        return f"- {sanitized}"

    def render_session_start_output(self, text: str) -> str:
        return json.dumps(
            {"systemMessage": text},
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def render_user_prompt_output(self, text: str) -> str:
        return json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": text,
                }
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )

    def normalize(self, envelope: IngressEnvelope, event_type: str) -> PartialEvent:
        if not envelope.event_id:
            raise ValueError("CodexAdapter: envelope missing required field 'event_id'")

        try:
            et = EventType(event_type)
        except ValueError as exc:
            raise ValueError(
                f"CodexAdapter: unsupported event_type {event_type!r} — "
                f"supported: {sorted(self.supported_event_types())}"
            ) from exc

        builder = _DATA_BUILDERS.get(et)
        if builder is None:
            raise ValueError(f"CodexAdapter: no data builder for event_type {event_type!r}")

        payload: Mapping[str, Any] = envelope.payload or {}
        hook_event_name = payload.get("hook_event_name")
        if not hook_event_name:
            raise ValueError("CodexAdapter: payload missing required field 'hook_event_name'")
        expected_hook = _EVENT_TYPE_TO_HOOK[et]
        if hook_event_name != expected_hook:
            raise ValueError(
                f"CodexAdapter: hook_event_name {hook_event_name!r} does not match "
                f"dispatched event_type {event_type!r} (expected hook {expected_hook!r}). "
                f"Route/payload mismatch."
            )

        data = builder(payload)
        session_id = envelope.session_id or payload.get("session_id")
        if not session_id:
            raise ValueError("CodexAdapter: payload missing required field 'session_id'")
        project_id = envelope.project_id
        if not project_id:
            cwd = payload.get("cwd")
            if not cwd:
                raise ValueError("CodexAdapter: payload missing required field 'cwd'")
            project_id = project_id_from_cwd(str(cwd))
        return PartialEvent(
            id=envelope.event_id,
            session_id=str(session_id),
            project_id=project_id,
            event_type=et,
            timestamp=envelope.timestamp,
            sequence_number=envelope.sequence_number,
            data=data,
            duration_ms=data.get("duration_ms"),
        )


__all__ = ["DROP_LIST", "CodexAdapter"]
