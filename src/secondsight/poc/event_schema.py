"""
Unified Event Schema v0.1 -- SecondSight POC

This module defines a unified event schema that can represent execution
events from Claude Code, OpenCode, and Codex in a single format.

Design principles:
1. TYPED FIELDS FIRST: Common fields across agents are typed at the top level.
   Agent-specific fields go in typed agent_metadata, not untyped dict[str, Any].
2. EXPLICIT UNIONS: Where agents use different structures for the same concept
   (e.g., tool arguments), the schema uses typed unions, not Any.
3. NORMALIZATION AT INGESTION: Each agent's raw event format is normalized
   to SecondSightEvent at the boundary. Internal code works with typed events.
4. SCHEMA VERSIONED: Every event carries the schema version for migration detection.

This implementation uses Python dataclasses (not Pydantic) per task spec.
Pydantic can be added as a dependency in Phase 1 if needed.

Assumptions:
- Event payloads from reference_opensoure are representative of current agent versions.
  If not: normalize_event will produce events with more None fields than expected,
  and typed_field_percentage will drop below 50% -- detectable via death tests.
- Schema versioning is needed this early (v0.1 may change significantly).
  If not: the version field costs nothing and provides migration detection.
- Token usage granularity differs across agents (per-message, per-turn cumulative,
  per-session cumulative). The schema models this with an is_cumulative flag.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, fields as dc_fields
from datetime import datetime, timezone
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


# Schema version -- embedded in every event for migration detection
SCHEMA_VERSION = "0.1.0"


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class AgentType(str, Enum):
    """Supported agent types."""

    CLAUDE_CODE = "claude_code"
    OPENCODE = "opencode"
    CODEX = "codex"


class EventType(str, Enum):
    """Unified event types across all agents.

    These are normalized from agent-specific event names:
    - tool_call_start: PreToolUse (CC), tool.execute.before (OC), function_call (Codex)
    - tool_call_end: PostToolUse (CC), tool.execute.after (OC), function_call_output (Codex)
    - session_start: Stop (first) (CC), session.created (OC), session_meta (Codex)
    - session_end: (inferred) (CC), session.idle (OC), (inferred from last event) (Codex)
    - turn_end: Stop (CC), (inferred) (OC), task_complete (Codex)
    - user_prompt: UserPromptSubmit (CC), chat.message (OC), response_item/message role=user (Codex)
    - agent_response: JSONL assistant message (CC), message.part.updated (OC), response_item/message role=assistant (Codex)
    - token_usage_report: JSONL usage (CC), DB message cost (OC), event_msg/token_count (Codex)
    - subagent_start: SubagentStart (CC), (session.parent_id) (OC), (session_meta source=subagent) (Codex)
    - subagent_end: SubagentStop (CC), (session ended + parent_id) (OC), (inferred) (Codex)
    - error: PostToolUseFailure (CC), event.session.error (OC), (inferred from output) (Codex)
    """

    TOOL_CALL_START = "tool_call_start"
    TOOL_CALL_END = "tool_call_end"
    SESSION_START = "session_start"
    SESSION_END = "session_end"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    USER_PROMPT = "user_prompt"
    AGENT_RESPONSE = "agent_response"
    TOKEN_USAGE_REPORT = "token_usage_report"
    SUBAGENT_START = "subagent_start"
    SUBAGENT_END = "subagent_end"
    ERROR = "error"


class ActionClassification(str, Enum):
    """Phase 2 action classification labels (PRD section 6.1)."""

    ALIGNED = "aligned"
    WASTEFUL = "wasteful"
    DIVERGENT = "divergent"
    EXPLORATORY = "exploratory"
    PREMATURE = "premature"
    OVER_VERIFIED = "over_verified"


# ---------------------------------------------------------------------------
# Typed sub-structures
# ---------------------------------------------------------------------------


@dataclass
class TokenUsage:
    """Token usage data -- handles all three granularity levels.

    Claude Code: per-message (via JSONL)
    OpenCode: per-message (via SQLite DB)
    Codex: per-session cumulative or per-turn (via JSONL token_count)

    is_cumulative distinguishes:
    - False: these counts are for this specific event/message
    - True: these counts are session-cumulative (Codex total_token_usage)
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_tokens: int = 0
    is_cumulative: bool = False
    cost: float | None = None  # Only available from OpenCode DB


@dataclass
class AgentMetadata:
    """Agent-specific metadata in TYPED fields, not dict[str, Any].

    Each agent has known fields that do not map to the common schema
    but are still typed and documented. This avoids the DC-3 anti-pattern
    of hiding data in untyped metadata bags.
    """

    # Claude Code specific
    transcript_path: str | None = None
    tool_use_id: str | None = None
    permission_mode: str | None = None
    hook_event_name: str | None = None
    stop_hook_active: bool | None = None
    message_id: str | None = None  # JSONL message.id for dedup
    model: str | None = None
    agent_version: str | None = None

    # OpenCode specific
    call_id: str | None = None
    tool_title: str | None = None  # output.title from tool.execute.after
    data_source: str | None = None  # "plugin_hook" or "db_polling"

    # Codex specific
    cli_version: str | None = None
    source: str | None = None  # cli, vscode, exec, mcp, subagent, unknown
    agent_nickname: str | None = None
    turn_id: str | None = None
    tool_kind: str | None = None  # function, custom, local_shell, mcp
    executed: bool | None = None  # Whether tool was actually executed (Codex hook)
    mutating: bool | None = None  # Whether tool mutated filesystem (Codex hook)
    sandbox: str | None = None  # Sandbox enforcement context (Codex hook)
    output_preview: str | None = None  # Truncated output (Codex hook)

    # Overflow for truly unexpected fields (should be minimal)
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Core event schema
# ---------------------------------------------------------------------------


@dataclass
class SecondSightEvent:
    """Unified event representation for all three agents.

    Fields are organized by universality:
    1. Identity fields (required for all events)
    2. Common fields (populated for most event types across agents)
    3. Phase 2 classification (populated during analysis, not ingestion)
    4. Agent-specific metadata (typed, not dict[str, Any])
    """

    # --- Identity (required) ---
    schema_version: str = SCHEMA_VERSION
    agent_type: str = ""  # AgentType value
    event_type: str = ""  # EventType value
    timestamp: str | None = None  # ISO 8601

    # --- Session identity ---
    session_id: str | None = None
    cwd: str | None = None  # Working directory

    # --- Tool call fields (populated for tool_call_start, tool_call_end) ---
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None  # Parsed tool arguments (always dict, never raw string)
    tool_result: str | None = None  # Tool output text
    duration_ms: int | None = None  # Tool call duration in milliseconds
    success: bool | None = None  # Explicit success/failure (Codex hook, OpenCode error events)

    # --- Content fields (populated for user_prompt, agent_response) ---
    content: str | None = None  # Message text content

    # --- Token usage (populated for agent_response, token_usage_report) ---
    token_usage: TokenUsage | None = None

    # --- Sub-agent fields ---
    subagent_id: str | None = None  # For subagent_start/subagent_end events
    parent_session_id: str | None = None  # Parent session for subagent events

    # --- Phase 2 action classification (None at ingestion, populated during analysis) ---
    action_classification: str | None = None  # ActionClassification value

    # --- Agent-specific typed metadata ---
    agent_metadata: AgentMetadata = field(default_factory=AgentMetadata)


# ---------------------------------------------------------------------------
# Public API: field introspection
# ---------------------------------------------------------------------------


def get_schema_field_names() -> list[str]:
    """Return the list of typed field names on SecondSightEvent.

    Excludes agent_metadata (which is a separate typed structure)
    and schema_version (which is infrastructure, not data).
    """
    excluded = {"schema_version", "agent_metadata"}
    return [f.name for f in dc_fields(SecondSightEvent) if f.name not in excluded]


def compute_typed_field_percentage(event: SecondSightEvent) -> float:
    """Compute what percentage of non-None fields are typed (not in extra dict).

    This is the DC-3 detection metric. For a given event:
    - Count total non-None fields across SecondSightEvent + AgentMetadata
    - Count non-None fields that are in agent_metadata.extra
    - typed_percentage = (total_non_none - extra_count) / total_non_none * 100

    If all data is in extra: percentage = 0%
    If no data is in extra: percentage = 100%
    """
    non_none_typed = 0
    total_non_none = 0

    # Count top-level SecondSightEvent fields
    for f in dc_fields(SecondSightEvent):
        if f.name in ("schema_version", "agent_metadata", "action_classification"):
            # schema_version: infrastructure, not data
            # agent_metadata: counted separately via its typed fields below
            # action_classification: Phase 2 write-target, always None at ingestion
            continue
        val = getattr(event, f.name)
        if val is not None:
            total_non_none += 1
            non_none_typed += 1

    # Count AgentMetadata typed fields (non-None, non-extra)
    meta = event.agent_metadata
    for f in dc_fields(AgentMetadata):
        if f.name == "extra":
            continue
        val = getattr(meta, f.name)
        if val is not None:
            total_non_none += 1
            non_none_typed += 1

    # Count extra dict entries
    extra_count = len(meta.extra)
    total_non_none += extra_count

    if total_non_none == 0:
        return 100.0  # No data means no untyped data

    return (non_none_typed / total_non_none) * 100.0


# ---------------------------------------------------------------------------
# Normalization: agent-specific raw events -> SecondSightEvent
# ---------------------------------------------------------------------------


def normalize_event(agent: str, raw: dict[str, Any]) -> SecondSightEvent:
    """Normalize a raw agent event into a SecondSightEvent.

    This is the ingestion boundary. All agent-specific parsing happens here.
    Internal code should only work with SecondSightEvent instances.

    Args:
        agent: Agent type string ("claude_code", "opencode", "codex")
        raw: Raw event data as a dict (agent-specific format)

    Returns:
        SecondSightEvent with typed fields populated from the raw data.
    """
    if agent == "claude_code":
        return _normalize_claude_code(raw)
    elif agent == "opencode":
        return _normalize_opencode(raw)
    elif agent == "codex":
        return _normalize_codex(raw)
    else:
        raise ValueError(f"Unknown agent type: {agent}")


def _normalize_claude_code(raw: dict[str, Any]) -> SecondSightEvent:
    """Normalize Claude Code events (hooks + JSONL transcript)."""
    event = SecondSightEvent(agent_type="claude_code")
    meta = event.agent_metadata

    # Detect event source: hook payload vs JSONL transcript record
    hook_event_name = raw.get("hook_event_name")
    record_type = raw.get("type")  # JSONL transcript records have a 'type' field

    if hook_event_name:
        # --- Hook-based events ---
        event.session_id = raw.get("session_id")
        event.cwd = raw.get("cwd")
        meta.hook_event_name = hook_event_name
        meta.transcript_path = raw.get("transcript_path")
        meta.permission_mode = raw.get("permission_mode")
        meta.tool_use_id = raw.get("tool_use_id")

        if hook_event_name == "PreToolUse":
            event.event_type = EventType.TOOL_CALL_START.value
            event.tool_name = raw.get("tool_name")
            tool_input = raw.get("tool_input")
            if isinstance(tool_input, dict):
                event.tool_args = tool_input

        elif hook_event_name == "PostToolUse":
            event.event_type = EventType.TOOL_CALL_END.value
            event.tool_name = raw.get("tool_name")
            tool_input = raw.get("tool_input")
            if isinstance(tool_input, dict):
                event.tool_args = tool_input
            tool_response = raw.get("tool_response")
            if isinstance(tool_response, dict):
                # Flatten tool_response to a text representation
                stdout = tool_response.get("stdout", "")
                stderr = tool_response.get("stderr", "")
                event.tool_result = stdout if stdout else stderr
                # Infer success ONLY for Bash tool where stderr is a meaningful
                # failure signal. For non-Bash tools, leave success=None because
                # PostToolUse has no explicit exit_status field (confirmed in
                # Task 1 investigation). Non-Bash failure detection requires
                # JSONL transcript is_error field (not available here).
                tool_name = raw.get("tool_name", "")
                if tool_name == "Bash":
                    if stderr:
                        event.success = False
                    else:
                        event.success = True
                # For non-Bash tools: success remains None (unknown)

        elif hook_event_name == "PostToolUseFailure":
            event.event_type = EventType.ERROR.value
            event.tool_name = raw.get("tool_name")
            event.success = False
            tool_input = raw.get("tool_input")
            if isinstance(tool_input, dict):
                event.tool_args = tool_input

        elif hook_event_name == "Stop":
            event.event_type = EventType.TURN_END.value
            meta.stop_hook_active = raw.get("stop_hook_active")

        elif hook_event_name == "UserPromptSubmit":
            event.event_type = EventType.USER_PROMPT.value
            event.content = raw.get("user_prompt")

        elif hook_event_name == "SubagentStart":
            event.event_type = EventType.SUBAGENT_START.value
            event.subagent_id = raw.get("agent_id")
            event.parent_session_id = raw.get("session_id")
            meta.agent_version = raw.get("agent_type")

        elif hook_event_name == "SubagentStop":
            event.event_type = EventType.SUBAGENT_END.value
            event.subagent_id = raw.get("agent_id")
            event.parent_session_id = raw.get("session_id")

    elif record_type in ("user", "assistant"):
        # --- JSONL transcript records ---
        event.session_id = raw.get("sessionId")
        event.cwd = raw.get("cwd")
        event.timestamp = raw.get("timestamp")
        meta.agent_version = raw.get("version")

        message = raw.get("message", {})
        role = message.get("role")
        meta.message_id = message.get("id")
        meta.model = message.get("model")

        if role == "user":
            event.event_type = EventType.USER_PROMPT.value
            content_blocks = message.get("content", [])
            texts = []
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
            event.content = "\n".join(texts) if texts else None

        elif role == "assistant":
            event.event_type = EventType.AGENT_RESPONSE.value
            content_blocks = message.get("content", [])
            texts = []
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    texts.append(block.get("text", ""))
            event.content = "\n".join(texts) if texts else None

            # Extract token usage
            usage = message.get("usage")
            if usage:
                event.token_usage = TokenUsage(
                    input_tokens=usage.get("input_tokens", 0),
                    output_tokens=usage.get("output_tokens", 0),
                    cache_read_tokens=usage.get("cache_read_input_tokens", 0),
                    cache_write_tokens=usage.get("cache_creation_input_tokens", 0),
                    is_cumulative=False,
                )

    return event


def _normalize_opencode(raw: dict[str, Any]) -> SecondSightEvent:
    """Normalize OpenCode events (plugin hooks + DB polling)."""
    event = SecondSightEvent(agent_type="opencode")
    meta = event.agent_metadata

    # Detect event source: plugin hook vs DB polling
    source = raw.get("source")

    if source == "db_polling":
        meta.data_source = "db_polling"
        event.session_id = raw.get("session_id")

        # DB message record
        msg_data = raw.get("message", {}).get("data", {})
        if msg_data:
            role = msg_data.get("role")
            if role == "assistant":
                event.event_type = EventType.AGENT_RESPONSE.value
            elif role == "user":
                event.event_type = EventType.USER_PROMPT.value
            else:
                event.event_type = EventType.AGENT_RESPONSE.value

            tokens = msg_data.get("tokens", {})
            cache = tokens.get("cache", {})
            event.token_usage = TokenUsage(
                input_tokens=tokens.get("input", 0),
                output_tokens=tokens.get("output", 0),
                cache_read_tokens=cache.get("read", 0),
                cache_write_tokens=cache.get("write", 0),
                is_cumulative=False,
                cost=msg_data.get("cost"),
            )

            time_data = msg_data.get("time", {})
            if time_data.get("created"):
                # Convert epoch ms to ISO 8601
                ts_ms = time_data["created"]
                dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                event.timestamp = dt.isoformat()

        # DB part (tool) record
        part_data = raw.get("part", {}).get("data", {})
        if part_data:
            part_type = part_data.get("type")
            if part_type == "tool":
                event.event_type = EventType.TOOL_CALL_END.value
                event.tool_name = part_data.get("tool")
                meta.call_id = part_data.get("callID")

                state = part_data.get("state", {})
                tool_input = state.get("input")
                if isinstance(tool_input, dict):
                    event.tool_args = tool_input
                event.tool_result = state.get("output")

                time_state = state.get("time", {})
                start_ms = time_state.get("start")
                end_ms = time_state.get("end")
                if start_ms and end_ms:
                    event.duration_ms = int(end_ms - start_ms)
                    dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
                    event.timestamp = dt.isoformat()

    elif "input" in raw:
        # --- Plugin hook events ---
        meta.data_source = "plugin_hook"
        input_data = raw.get("input", {})
        output_data = raw.get("output", {})

        event.session_id = input_data.get("sessionID")
        meta.call_id = input_data.get("callID")

        tool_name = input_data.get("tool")
        if tool_name:
            event.tool_name = tool_name

        # Distinguish before vs after by hook output structure:
        # - tool.execute.before: output has {args} (modifiable tool arguments)
        # - tool.execute.after: output has {output, title, metadata} (tool result)
        # This is based on OpenCode plugin API contract (Task 2 investigation).
        # Assumption: OpenCode before/after hooks are distinguishable by
        # presence of "output" key in output_data. If OpenCode changes this
        # structure, events may be misclassified.
        if "output" in output_data:
            # tool.execute.after -- has result text
            event.event_type = EventType.TOOL_CALL_END.value
            event.tool_result = output_data.get("output")
            meta.tool_title = output_data.get("title")
            # Get args from input (mirrored in after hook)
            args = input_data.get("args")
            if isinstance(args, dict):
                event.tool_args = args
        elif "args" in output_data:
            # tool.execute.before -- has modifiable args
            event.event_type = EventType.TOOL_CALL_START.value
            args = output_data.get("args")
            if isinstance(args, dict):
                event.tool_args = args
        else:
            # Fallback: unknown hook structure. Log and default to tool_call_end.
            logger.warning(
                "OpenCode plugin hook has unknown output structure: %s. "
                "Defaulting to tool_call_end.",
                list(output_data.keys()),
            )
            event.event_type = EventType.TOOL_CALL_END.value

    elif "event" in raw:
        # --- Event bus events (session lifecycle) ---
        meta.data_source = "plugin_hook"
        props = raw.get("event", {}).get("properties", {})
        event.session_id = props.get("sessionID")

        # Detect event type from presence of fields
        event_type_str = raw.get("event", {}).get("type", "")
        if "error" in props:
            event.event_type = EventType.ERROR.value
        elif "idle" in event_type_str or "delete" in event_type_str:
            event.event_type = EventType.SESSION_END.value
        else:
            event.event_type = EventType.SESSION_START.value

    return event


def _normalize_codex(raw: dict[str, Any]) -> SecondSightEvent:
    """Normalize Codex events (JSONL rollout + hook callbacks)."""
    event = SecondSightEvent(agent_type="codex")
    meta = event.agent_metadata

    # Detect event source: JSONL rollout vs hook callback
    if "hook_event" in raw:
        # --- Hook callback event ---
        event.session_id = raw.get("session_id")
        event.cwd = raw.get("cwd")
        event.timestamp = raw.get("triggered_at")

        hook = raw.get("hook_event", {})
        hook_type = hook.get("event_type")
        meta.turn_id = hook.get("turn_id")
        meta.call_id = hook.get("call_id")
        meta.tool_kind = hook.get("tool_kind")
        meta.executed = hook.get("executed")
        meta.mutating = hook.get("mutating")
        meta.sandbox = hook.get("sandbox")
        meta.output_preview = hook.get("output_preview")

        if hook_type == "after_tool_use":
            event.event_type = EventType.TOOL_CALL_END.value
            event.tool_name = hook.get("tool_name")
            event.duration_ms = hook.get("duration_ms")
            event.success = hook.get("success")

            tool_input = hook.get("tool_input", {})
            # Codex hook tool_input may contain arguments as JSON string
            args = tool_input.get("arguments")
            if isinstance(args, str):
                try:
                    event.tool_args = json.loads(args)
                except json.JSONDecodeError, TypeError:
                    logger.warning(
                        "Codex hook tool_input.arguments is not valid JSON: %r. "
                        "Storing as raw_arguments. Structured tool_args parsing degraded.",
                        args[:200] if len(args) > 200 else args,
                    )
                    event.tool_args = {"raw_arguments": args}
            elif isinstance(args, dict):
                event.tool_args = args
            elif isinstance(tool_input, dict) and tool_input:
                event.tool_args = tool_input

        elif hook_type == "before_tool_use":
            event.event_type = EventType.TOOL_CALL_START.value
            event.tool_name = hook.get("tool_name")

    else:
        # --- JSONL rollout event ---
        event.timestamp = raw.get("timestamp")
        envelope_type = raw.get("type")
        payload = raw.get("payload", {})

        if envelope_type == "session_meta":
            event.event_type = EventType.SESSION_START.value
            event.session_id = payload.get("id")
            event.cwd = payload.get("cwd")
            meta.cli_version = payload.get("cli_version")
            meta.agent_version = payload.get("cli_version")

            # Parse source field (can be string or object)
            source_val = payload.get("source")
            if isinstance(source_val, str):
                meta.source = source_val
            elif isinstance(source_val, dict):
                meta.source = str(source_val)

            meta.agent_nickname = payload.get("agent_nickname")
            if meta.source == "subagent" or meta.agent_nickname:
                event.event_type = EventType.SUBAGENT_START.value
                event.subagent_id = event.session_id

        elif envelope_type == "turn_context":
            event.event_type = EventType.TURN_START.value
            event.cwd = payload.get("cwd")
            meta.model = payload.get("model")
            git = payload.get("git", {})
            if git.get("branch"):
                meta.extra["git_branch"] = git["branch"]

        elif envelope_type == "response_item":
            item_type = payload.get("type")

            if item_type == "message":
                role = payload.get("role")
                content_blocks = payload.get("content", [])
                texts = []
                for block in content_blocks:
                    if isinstance(block, dict):
                        text = block.get("text", "")
                        if text:
                            texts.append(text)
                event.content = "\n".join(texts) if texts else None

                if role == "user":
                    event.event_type = EventType.USER_PROMPT.value
                elif role == "assistant":
                    event.event_type = EventType.AGENT_RESPONSE.value

            elif item_type == "function_call":
                event.event_type = EventType.TOOL_CALL_START.value
                event.tool_name = payload.get("name")
                meta.call_id = payload.get("call_id")

                # CRITICAL: Codex arguments is a JSON-encoded STRING
                # Must be double-parsed to produce a dict
                arguments = payload.get("arguments")
                if isinstance(arguments, str):
                    try:
                        event.tool_args = json.loads(arguments)
                    except json.JSONDecodeError, TypeError:
                        # If it's not valid JSON (e.g., raw patch content),
                        # store as a dict with the raw string.
                        # This is a degradation signal -- log it.
                        logger.warning(
                            "Codex JSONL function_call.arguments is not valid JSON: %r. "
                            "Storing as raw_arguments. Structured tool_args parsing degraded.",
                            arguments[:200] if len(arguments) > 200 else arguments,
                        )
                        event.tool_args = {"raw_arguments": arguments}
                elif isinstance(arguments, dict):
                    event.tool_args = arguments

            elif item_type == "function_call_output":
                event.event_type = EventType.TOOL_CALL_END.value
                meta.call_id = payload.get("call_id")
                event.tool_result = payload.get("output")

        elif envelope_type == "event_msg":
            msg_type = payload.get("type")

            if msg_type == "token_count":
                event.event_type = EventType.TOKEN_USAGE_REPORT.value
                info = payload.get("info", {})
                if info:
                    last = info.get("last_token_usage", {})
                    total = info.get("total_token_usage", {})
                    source = last if last else total
                    event.token_usage = TokenUsage(
                        input_tokens=source.get("input_tokens", 0),
                        output_tokens=source.get("output_tokens", 0),
                        cache_read_tokens=source.get("cached_input_tokens", 0),
                        reasoning_tokens=source.get("reasoning_output_tokens", 0),
                        is_cumulative=not bool(last),
                    )

            elif msg_type in ("task_complete", "turn_complete"):
                event.event_type = EventType.TURN_END.value
                meta.turn_id = payload.get("turn_id")
                event.duration_ms = payload.get("duration_ms")

            elif msg_type in ("task_started", "turn_started"):
                event.event_type = EventType.SESSION_START.value
                meta.turn_id = payload.get("turn_id")

            elif msg_type == "user_message":
                event.event_type = EventType.USER_PROMPT.value
                event.content = payload.get("message")

            elif msg_type == "agent_message":
                event.event_type = EventType.AGENT_RESPONSE.value
                event.content = payload.get("message")

    return event


# ---------------------------------------------------------------------------
# Serialization / deserialization
# ---------------------------------------------------------------------------


def event_to_dict(event: SecondSightEvent) -> dict[str, Any]:
    """Serialize a SecondSightEvent to a JSON-compatible dict.

    Handles nested dataclasses (TokenUsage, AgentMetadata) and
    filters out None values for compact representation.
    """
    result: dict[str, Any] = {}

    for f in dc_fields(SecondSightEvent):
        val = getattr(event, f.name)
        if val is None:
            continue
        if f.name == "token_usage" and isinstance(val, TokenUsage):
            result["token_usage"] = _dataclass_to_dict(val)
        elif f.name == "agent_metadata" and isinstance(val, AgentMetadata):
            meta_dict = _dataclass_to_dict(val)
            if meta_dict:  # Only include if there are non-None fields
                result["agent_metadata"] = meta_dict
        else:
            result[f.name] = val

    return result


def _dataclass_to_dict(obj: Any) -> dict[str, Any]:
    """Convert a dataclass to a dict, filtering out None values and empty collections."""
    result = {}
    for f in dc_fields(obj):
        val = getattr(obj, f.name)
        if val is None:
            continue
        if isinstance(val, dict) and not val:
            continue
        if isinstance(val, bool):
            result[f.name] = val
        elif val == 0 and f.name not in (
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "reasoning_tokens",
            "duration_ms",
        ):
            continue
        else:
            result[f.name] = val
    return result


def event_from_dict(data: dict[str, Any]) -> SecondSightEvent:
    """Deserialize a dict back to a SecondSightEvent.

    This is the reverse of event_to_dict.
    """
    event = SecondSightEvent()

    for f in dc_fields(SecondSightEvent):
        if f.name not in data:
            continue
        val = data[f.name]

        if f.name == "token_usage" and isinstance(val, dict):
            event.token_usage = TokenUsage(**val)
        elif f.name == "agent_metadata" and isinstance(val, dict):
            val_copy = dict(val)
            extra = val_copy.pop("extra", {})
            filtered = {k: v for k, v in val_copy.items() if v is not None}
            event.agent_metadata = AgentMetadata(**filtered, extra=extra)
        else:
            setattr(event, f.name, val)

    return event


# ---------------------------------------------------------------------------
# JSON Schema export
# ---------------------------------------------------------------------------


def generate_json_schema() -> dict[str, Any]:
    """Generate a JSON Schema document describing the SecondSightEvent format.

    This is a POC-level export -- not a full JSON Schema with $refs and
    definitions, but sufficient for Phase 1 validation and documentation.
    """
    event_types = [e.value for e in EventType]
    agent_types = [a.value for a in AgentType]
    classification_types = [c.value for c in ActionClassification]

    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": f"SecondSight Unified Event Schema v{SCHEMA_VERSION}",
        "version": SCHEMA_VERSION,
        "description": (
            "Unified event schema for representing execution events from "
            "Claude Code, OpenCode, and Codex agents. "
            f"Schema version: {SCHEMA_VERSION}"
        ),
        "type": "object",
        "required": ["schema_version", "agent_type", "event_type"],
        "properties": {
            "schema_version": {
                "type": "string",
                "const": SCHEMA_VERSION,
                "description": "Schema version for migration detection",
            },
            "agent_type": {
                "type": "string",
                "enum": agent_types,
                "description": "Which agent produced this event",
            },
            "event_type": {
                "type": "string",
                "enum": event_types,
                "description": "Normalized event type",
            },
            "timestamp": {
                "type": ["string", "null"],
                "format": "date-time",
                "description": "Event timestamp in ISO 8601 format",
            },
            "session_id": {
                "type": ["string", "null"],
                "description": "Agent-native session identifier",
            },
            "cwd": {
                "type": ["string", "null"],
                "description": "Working directory at time of event",
            },
            "tool_name": {
                "type": ["string", "null"],
                "description": "Name of the tool invoked (normalized from agent-specific names)",
            },
            "tool_args": {
                "type": ["object", "null"],
                "description": (
                    "Tool arguments as a parsed dict. For Codex, JSON-encoded string "
                    "arguments are parsed to dict at normalization time."
                ),
            },
            "tool_result": {
                "type": ["string", "null"],
                "description": "Tool execution output text",
            },
            "duration_ms": {
                "type": ["integer", "null"],
                "description": "Tool call or turn duration in milliseconds",
            },
            "success": {
                "type": ["boolean", "null"],
                "description": (
                    "Explicit success/failure flag. Available from Codex hooks "
                    "(hook_event.success) and inferred from Claude Code stderr."
                ),
            },
            "content": {
                "type": ["string", "null"],
                "description": "Message text content (user prompts, agent responses)",
            },
            "token_usage": {
                "type": ["object", "null"],
                "description": "Token usage data (granularity varies by agent)",
                "properties": {
                    "input_tokens": {"type": "integer"},
                    "output_tokens": {"type": "integer"},
                    "cache_read_tokens": {"type": "integer"},
                    "cache_write_tokens": {"type": "integer"},
                    "reasoning_tokens": {"type": "integer"},
                    "is_cumulative": {
                        "type": "boolean",
                        "description": (
                            "True if counts are session-cumulative (Codex total_token_usage). "
                            "False if counts are per-message/per-event."
                        ),
                    },
                    "cost": {
                        "type": ["number", "null"],
                        "description": "Monetary cost (only from OpenCode DB)",
                    },
                },
            },
            "subagent_id": {
                "type": ["string", "null"],
                "description": "Sub-agent identifier for subagent lifecycle events",
            },
            "parent_session_id": {
                "type": ["string", "null"],
                "description": "Parent session ID for sub-agent events",
            },
            "action_classification": {
                "type": ["string", "null"],
                "enum": classification_types + [None],
                "description": (
                    "Phase 2 action classification. None at ingestion time, "
                    "populated during analysis."
                ),
            },
            "agent_metadata": {
                "type": "object",
                "description": (
                    "Agent-specific typed metadata. Fields are documented per-agent. "
                    "The 'extra' sub-field captures truly unexpected fields."
                ),
                "properties": {
                    "transcript_path": {"type": ["string", "null"]},
                    "tool_use_id": {"type": ["string", "null"]},
                    "permission_mode": {"type": ["string", "null"]},
                    "hook_event_name": {"type": ["string", "null"]},
                    "stop_hook_active": {"type": ["boolean", "null"]},
                    "message_id": {"type": ["string", "null"]},
                    "model": {"type": ["string", "null"]},
                    "agent_version": {"type": ["string", "null"]},
                    "call_id": {"type": ["string", "null"]},
                    "tool_title": {"type": ["string", "null"]},
                    "data_source": {"type": ["string", "null"]},
                    "cli_version": {"type": ["string", "null"]},
                    "source": {"type": ["string", "null"]},
                    "agent_nickname": {"type": ["string", "null"]},
                    "turn_id": {"type": ["string", "null"]},
                    "tool_kind": {"type": ["string", "null"]},
                    "executed": {"type": ["boolean", "null"]},
                    "mutating": {"type": ["boolean", "null"]},
                    "sandbox": {"type": ["string", "null"]},
                    "output_preview": {"type": ["string", "null"]},
                    "extra": {
                        "type": "object",
                        "description": "Overflow for truly unexpected fields",
                        "additionalProperties": True,
                    },
                },
            },
        },
        "additionalProperties": False,
    }

    return schema


def export_json_schema(path: str) -> None:
    """Export the JSON Schema to a file."""
    schema = generate_json_schema()
    with open(path, "w") as f:
        json.dump(schema, f, indent=2)
        f.write("\n")


# ---------------------------------------------------------------------------
# Module-level schema export on import (for POC convenience)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1:
        export_json_schema(sys.argv[1])
    else:
        print(json.dumps(generate_json_schema(), indent=2))
