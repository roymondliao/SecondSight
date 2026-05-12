"""CodexAdapter — Codex CLI hook payload → SecondSight PartialEvent.

Per SD §4.3 + Phase 0 investigation (P0-2). Translates Codex CLI hook
stdin payloads (wrapped in HookEnvelope by a config.toml hook script)
into PartialEvents that SessionTracker.bind() finalises into Events.

Codex CLI hook surface (codex-rs/hooks/src/types.rs):
    post_tool_use, session_start, user_prompt_submit, stop
    — registered via ~/.codex/config.toml [hooks] section.

The adapter normalises these four hook event types. JSONL rollout file
parsing (Tier 1 in Phase 0 report) is out of scope — that would be a
file-watcher, not an adapter for the hook API.

Privacy contract (mirrors ClaudeCodeAdapter, SD §3.7.4):
    Raw tool_input params, tool output content, and user prompt text are
    NEVER stored in Event.data. Only metadata (sizes, types, names) flows
    through. The DROP_LIST is declarative.

Assumptions:
    - Codex CLI hook payloads arrive wrapped in HookEnvelope by a bash
      hook script (same pattern as Claude Code).
    - The hook_event_name field in payload maps to the Codex hook event
      type: "post_tool_use", "session_start", "user_prompt_submit", "stop".
    - Codex CLI agent name is "codex" (snake_case per plan §7 G3).
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

_AGENT_NAME = "codex"

_HOOK_TO_EVENT_TYPE: dict[str, EventType] = {
    "post_tool_use": EventType.TOOL_USE_END,
    "session_start": EventType.SESSION_START,
    "user_prompt_submit": EventType.USER_PROMPT,
    "stop": EventType.SESSION_END,
}

_EVENT_TYPE_TO_HOOK: dict[EventType, str] = {
    et: hook for hook, et in _HOOK_TO_EVENT_TYPE.items()
}

DROP_LIST: frozenset[str] = frozenset(
    {
        "hook_event.tool_input",
        "hook_event.output_preview",
    }
)


def _action_metadata(payload: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if "cwd" in payload:
        out["cwd"] = payload["cwd"]
    return out


def _normalize_post_tool_use(payload: Mapping[str, Any]) -> dict[str, Any]:
    """post_tool_use → tool_use_end.

    Codex post_tool_use hook payload (confirmed in codex-rs/hooks/src/types.rs):
        hook_event.tool_name, hook_event.tool_kind, hook_event.executed,
        hook_event.success, hook_event.duration_ms, hook_event.mutating,
        hook_event.output_preview (truncated — dropped).
    """
    hook_event = payload.get("hook_event") or {}
    if not isinstance(hook_event, Mapping):
        raise ValueError("CodexAdapter: post_tool_use payload missing 'hook_event' object")

    tool_name = hook_event.get("tool_name")
    if not tool_name:
        raise ValueError(
            "CodexAdapter: post_tool_use hook_event missing required field 'tool_name'"
        )

    metadata = _action_metadata(payload)
    data: dict[str, Any] = {"tool_name": tool_name}

    if "tool_kind" in hook_event:
        data["tool_kind"] = hook_event["tool_kind"]

    if "executed" in hook_event:
        data["executed"] = bool(hook_event["executed"])

    if "success" in hook_event:
        data["success"] = bool(hook_event["success"])

    if "duration_ms" in hook_event:
        data["duration_ms"] = hook_event["duration_ms"]

    if "mutating" in hook_event:
        data["mutating"] = bool(hook_event["mutating"])

    if "turn_id" in hook_event:
        data["turn_id"] = hook_event["turn_id"]

    if "call_id" in hook_event:
        data["call_id"] = hook_event["call_id"]

    if metadata:
        data["action_metadata"] = metadata
    return data


def _normalize_session_start(payload: Mapping[str, Any]) -> dict[str, Any]:
    """session_start → session_start. Carries cwd only (hook payload is minimal)."""
    metadata = _action_metadata(payload)
    return {"action_metadata": metadata} if metadata else {}


def _normalize_user_prompt_submit(payload: Mapping[str, Any]) -> dict[str, Any]:
    """user_prompt_submit → user_prompt. No prompt text in hook payload (per P0-2)."""
    metadata = _action_metadata(payload)
    return {"action_metadata": metadata} if metadata else {}


def _normalize_stop(payload: Mapping[str, Any]) -> dict[str, Any]:
    """stop → session_end. Carries cwd and triggered_at only."""
    metadata = _action_metadata(payload)
    return {"action_metadata": metadata} if metadata else {}


_DATA_BUILDERS: dict[EventType, Callable[[Mapping[str, Any]], dict[str, Any]]] = {
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

    Supports 4 event types from the Codex CLI hook callback surface:
    post_tool_use, session_start, user_prompt_submit, stop.

    Note: Codex CLI hooks do NOT provide a pre_tool_use equivalent with
    enough data for tool_use_start — the pre_tool_use hook is for blocking/
    approval, not observation. Tool use start events require JSONL parsing
    (out of adapter scope).
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
            raise ValueError(
                f"CodexAdapter: no data builder for event_type {event_type!r}"
            )

        payload: Mapping[str, Any] = envelope.payload or {}
        hook_event_name = payload.get("hook_event_name")
        if not hook_event_name:
            raise ValueError(
                "CodexAdapter: payload missing required field 'hook_event_name'"
            )
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
