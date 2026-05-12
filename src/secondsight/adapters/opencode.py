"""OpenCodeAdapter — OpenCode hook payload → SecondSight PartialEvent.

Per SD §4.3 + Phase 0 investigation (P0-3). Translates OpenCode plugin
hook events (forwarded to SecondSight via an HTTP bridge plugin) into
PartialEvents that SessionTracker.bind() finalises into Events.

OpenCode plugin hook surface (@opencode-ai/plugin):
    tool.execute.before, tool.execute.after — typed hooks with tool name,
    sessionID, callID, args, and output.
    session.created, session.idle, session.error — session lifecycle events.
    chat.message — user prompt hook.

The adapter normalises these event types. DB polling (unofficial SQLite
access) is out of scope — that would be a separate poller, not an adapter
for the hook API.

Integration pattern:
    OpenCode plugins run in Bun (JS/TS). A bridge plugin forwards events
    to SecondSight's /hook/{event_type} endpoint as HookEnvelope JSON.
    The bridge plugin wraps OpenCode's hook payload into the HookEnvelope
    format with hook_event_name set to the OpenCode event name.

Privacy contract (mirrors ClaudeCodeAdapter, SD §3.7.4):
    Raw tool arguments (output.args / input.args), tool output content
    (output.output), and user prompt text are NEVER stored in Event.data.
    Only metadata (sizes, types, tool names) flows through.

Assumptions:
    - OpenCode agent name is "opencode" (snake_case per plan §7 G3).
    - hook_event_name in the payload maps to the OpenCode plugin event:
      "tool.execute.before", "tool.execute.after", "session.created",
      "session.idle", "chat.message".
    - Plugin hooks lack per-call timestamps; wall-clock time from the
      bridge plugin's envelope.timestamp is used instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable

from secondsight.adapters.base import AgentAdapter
from secondsight.api.schemas import IngressEnvelope
from secondsight.event import EventType
from secondsight.feedback.convention import Convention
from secondsight.feedback.hint import Hint
from secondsight.observation.tracker import PartialEvent

_AGENT_NAME = "opencode"

_HOOK_TO_EVENT_TYPE: dict[str, EventType] = {
    "tool.execute.before": EventType.TOOL_USE_START,
    "tool.execute.after": EventType.TOOL_USE_END,
    "session.created": EventType.SESSION_START,
    "session.idle": EventType.SESSION_END,
    "chat.message": EventType.USER_PROMPT,
}

_EVENT_TYPE_TO_HOOK: dict[EventType, str] = {
    et: hook for hook, et in _HOOK_TO_EVENT_TYPE.items()
}

DROP_LIST: frozenset[str] = frozenset(
    {
        "output.args",
        "input.args",
        "output.output",
        "output.message",
        "output.parts",
    }
)


def _normalize_tool_execute_before(payload: Mapping[str, Any]) -> dict[str, Any]:
    """tool.execute.before → tool_use_start.

    OpenCode plugin hook provides:
        input.tool, input.sessionID, input.callID
        output.args (modifiable — dropped for privacy)
    """
    input_data = payload.get("input") or {}
    if not isinstance(input_data, Mapping):
        raise ValueError(
            "OpenCodeAdapter: tool.execute.before payload missing 'input' object"
        )

    tool_name = input_data.get("tool")
    if not tool_name:
        raise ValueError(
            "OpenCodeAdapter: tool.execute.before input missing required field 'tool'"
        )

    data: dict[str, Any] = {"tool_name": tool_name}

    if "callID" in input_data:
        data["call_id"] = input_data["callID"]

    output_data = payload.get("output") or {}
    if isinstance(output_data, Mapping) and "args" in output_data:
        args = output_data["args"]
        if isinstance(args, Mapping):
            if "file_path" in args:
                data["action_target"] = args["file_path"]
            if "command" in args:
                data["action_metadata"] = {"command_length": len(str(args["command"]))}

    return data


def _normalize_tool_execute_after(payload: Mapping[str, Any]) -> dict[str, Any]:
    """tool.execute.after → tool_use_end.

    OpenCode plugin hook provides:
        input.tool, input.sessionID, input.callID, input.args
        output.output (string — dropped), output.title, output.metadata
    """
    input_data = payload.get("input") or {}
    if not isinstance(input_data, Mapping):
        raise ValueError(
            "OpenCodeAdapter: tool.execute.after payload missing 'input' object"
        )

    tool_name = input_data.get("tool")
    if not tool_name:
        raise ValueError(
            "OpenCodeAdapter: tool.execute.after input missing required field 'tool'"
        )

    data: dict[str, Any] = {"tool_name": tool_name}

    if "callID" in input_data:
        data["call_id"] = input_data["callID"]

    output_data = payload.get("output") or {}
    if isinstance(output_data, Mapping):
        if "output" in output_data:
            data["output_size"] = len(str(output_data["output"]))
        if "title" in output_data:
            data["title"] = output_data["title"]
        data["success"] = True

    if isinstance(input_data.get("args"), Mapping):
        args = input_data["args"]
        if "file_path" in args:
            data["action_target"] = args["file_path"]

    return data


def _normalize_session_created(payload: Mapping[str, Any]) -> dict[str, Any]:
    """session.created → session_start."""
    data: dict[str, Any] = {}
    props = payload.get("properties") or payload
    if isinstance(props, Mapping) and "sessionID" in props:
        pass
    return data


def _normalize_session_idle(payload: Mapping[str, Any]) -> dict[str, Any]:
    """session.idle → session_end."""
    return {}


def _normalize_chat_message(payload: Mapping[str, Any]) -> dict[str, Any]:
    """chat.message → user_prompt. Prompt text dropped; only metadata kept."""
    data: dict[str, Any] = {}
    input_data = payload.get("input") or {}
    if isinstance(input_data, Mapping):
        if "agent" in input_data:
            data["agent"] = input_data["agent"]
        model = input_data.get("model")
        if isinstance(model, Mapping):
            data["model_id"] = model.get("modelID", "")

    output_data = payload.get("output") or {}
    if isinstance(output_data, Mapping):
        parts = output_data.get("parts")
        if isinstance(parts, list):
            data["part_count"] = len(parts)

    return data


def _session_id_from_payload(payload: Mapping[str, Any]) -> str | None:
    for candidate in (
        payload.get("session_id"),
        (payload.get("properties") or {}).get("sessionID")
        if isinstance(payload.get("properties"), Mapping)
        else None,
        (payload.get("input") or {}).get("sessionID")
        if isinstance(payload.get("input"), Mapping)
        else None,
    ):
        if candidate:
            return str(candidate)
    return None


_DATA_BUILDERS: dict[EventType, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
    EventType.TOOL_USE_START: _normalize_tool_execute_before,
    EventType.TOOL_USE_END: _normalize_tool_execute_after,
    EventType.SESSION_START: _normalize_session_created,
    EventType.SESSION_END: _normalize_session_idle,
    EventType.USER_PROMPT: _normalize_chat_message,
}

assert set(_HOOK_TO_EVENT_TYPE.values()) == set(_DATA_BUILDERS.keys()), (
    "_HOOK_TO_EVENT_TYPE / _DATA_BUILDERS divergence: "
    f"hook→event mapping publishes {set(_HOOK_TO_EVENT_TYPE.values()) - set(_DATA_BUILDERS.keys())!r} "
    f"without builders, AND/OR _DATA_BUILDERS has builders for "
    f"{set(_DATA_BUILDERS.keys()) - set(_HOOK_TO_EVENT_TYPE.values())!r} that no hook publishes."
)


class OpenCodeAdapter(AgentAdapter):
    """OpenCode plugin hook payload → SecondSight PartialEvent.

    Supports 5 event types from the OpenCode plugin hook surface:
    tool.execute.before, tool.execute.after, session.created,
    session.idle, chat.message.

    The plugin hooks run in Bun (JS/TS); a bridge plugin forwards events
    to SecondSight as HookEnvelope payloads over HTTP.
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

    def normalize(self, envelope: IngressEnvelope, event_type: str) -> PartialEvent:
        if not envelope.event_id:
            raise ValueError(
                "OpenCodeAdapter: envelope missing required field 'event_id'"
            )

        try:
            et = EventType(event_type)
        except ValueError as exc:
            raise ValueError(
                f"OpenCodeAdapter: unsupported event_type {event_type!r} — "
                f"supported: {sorted(self.supported_event_types())}"
            ) from exc

        builder = _DATA_BUILDERS.get(et)
        if builder is None:
            raise ValueError(
                f"OpenCodeAdapter: no data builder for event_type {event_type!r}"
            )

        payload: Mapping[str, Any] = envelope.payload or {}
        hook_event_name = payload.get("hook_event_name")
        if not hook_event_name:
            raise ValueError(
                "OpenCodeAdapter: payload missing required field 'hook_event_name'"
            )
        expected_hook = _EVENT_TYPE_TO_HOOK[et]
        if hook_event_name != expected_hook:
            raise ValueError(
                f"OpenCodeAdapter: hook_event_name {hook_event_name!r} does not match "
                f"dispatched event_type {event_type!r} (expected hook {expected_hook!r}). "
                f"Route/payload mismatch."
            )

        data = builder(payload)
        session_id = envelope.session_id or _session_id_from_payload(payload)
        if not session_id:
            raise ValueError(
                "OpenCodeAdapter: payload missing required field 'session_id'"
            )
        if not envelope.project_id:
            raise ValueError(
                "OpenCodeAdapter: envelope missing required field 'project_id'"
            )
        return PartialEvent(
            id=envelope.event_id,
            session_id=session_id,
            project_id=envelope.project_id,
            event_type=et,
            timestamp=envelope.timestamp,
            sequence_number=envelope.sequence_number,
            data=data,
        )


__all__ = ["DROP_LIST", "OpenCodeAdapter"]
