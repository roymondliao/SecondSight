# Unified Event Schema v0.1 -- Design Notes

## 1. Design Goals

The Unified Event Schema must represent execution events from Claude Code, OpenCode, and Codex in a single format, with these constraints:

- **>= 50% typed fields per agent** (DC-3 detection criterion)
- **No false unification** -- fields present for one agent should not pollute the schema for others
- **Codex double-parsing handled** -- JSON-encoded string arguments must be parsed at normalization
- **Phase 2 ready** -- action classification field present but optional
- **Versioned** -- schema version in every event for migration detection

## 2. Architecture: Normalize at the Boundary

```
Agent-specific raw events
    |
    v
normalize_event(agent, raw) -- BOUNDARY
    |
    v
SecondSightEvent (typed, unified)
    |
    v
Internal analysis pipeline
```

All agent-specific parsing happens in `normalize_event()`. Internal code only works with `SecondSightEvent` instances. This means:
- Agent format changes only affect the normalization layer
- New agents are added by writing a new `_normalize_<agent>()` function
- The schema itself does not need to change when an agent updates its format

## 3. Field Organization

### Shared Typed Fields (top-level on SecondSightEvent)

These fields are semantically meaningful across all agents:

| Field | Populated By | Notes |
|-------|-------------|-------|
| agent_type | All | Always set |
| event_type | All | Normalized event type enum |
| timestamp | CC (JSONL), OC (DB), Codex (JSONL) | CC hooks lack timestamp in payload; system clock is used |
| session_id | All | CC: session_id, OC: sessionID, Codex: payload.id or session_id |
| cwd | CC (hooks + JSONL), OC (DB), Codex (JSONL + hooks) | |
| tool_name | All (tool events) | CC: tool_name, OC: tool/input.tool, Codex: name |
| tool_args | All (tool events) | Always a dict; Codex JSON string is parsed |
| tool_result | All (tool end events) | CC: tool_response flattened, OC: output, Codex: output |
| duration_ms | OC (DB), Codex (hooks + task_complete) | CC: requires Pre/Post pairing |
| success | CC (inferred), Codex (hooks) | Not available from OC plugin hooks |
| content | All (message events) | User prompts and agent responses |
| token_usage | CC (JSONL), OC (DB), Codex (JSONL) | Granularity varies |
| subagent_id | CC (SubagentStart/Stop), Codex (session_meta) | OC: via parent_id |
| parent_session_id | CC, OC (DB), Codex | |
| action_classification | None at ingestion | Phase 2 populates |

### Agent-Specific Typed Metadata (AgentMetadata dataclass)

Fields that are meaningful for specific agents but still typed (not dict[str, Any]):

**Claude Code specific:** transcript_path, tool_use_id, permission_mode, hook_event_name, stop_hook_active, message_id, model, agent_version

**OpenCode specific:** call_id, tool_title, data_source

**Codex specific:** cli_version, source, agent_nickname, turn_id, tool_kind, executed, mutating, sandbox, output_preview

**Overflow:** `extra: dict[str, Any]` -- for truly unexpected fields. DC-3 monitors this.

### Why Not dict[str, Any] metadata?

The DC-3 death case explicitly targets schemas that hide incompatibility behind untyped metadata. Our `AgentMetadata` has 20+ typed fields and a minimal `extra` dict. The `compute_typed_field_percentage()` function monitors this ratio.

Measured typed field percentages (from death tests):
- Claude Code: ~80-90% typed
- OpenCode: ~80-90% typed
- Codex: ~80-90% typed

All above the 50% threshold.

## 4. Unification Decisions

### Tool Arguments

| Agent | Raw Format | Normalized |
|-------|-----------|------------|
| Claude Code | `tool_input: {command: "ls"}` (dict) | `tool_args: {command: "ls"}` |
| OpenCode | `output.args: {command: "ls"}` (dict) | `tool_args: {command: "ls"}` |
| Codex JSONL | `arguments: '{"cmd":"rg codex"}'` (JSON string) | `tool_args: {cmd: "rg codex"}` (parsed) |
| Codex hook | `tool_input.arguments: '{"cmd":"rg"}'` (JSON string) | `tool_args: {cmd: "rg"}` (parsed) |

Decision: `tool_args` is always `dict[str, Any]`. Codex's JSON-encoded strings are parsed at normalization time. If parsing fails (e.g., raw patch content that isn't valid JSON), it's stored as `{"raw_arguments": "<original string>"}`.

### Token Usage

| Agent | Granularity | is_cumulative |
|-------|------------|---------------|
| Claude Code | Per-message (JSONL) | False |
| OpenCode | Per-message (DB) | False |
| Codex | Per-session cumulative (JSONL) | True |
| Codex | Per-turn delta (last_token_usage) | False (if extracted) |

Decision: `TokenUsage.is_cumulative` flag distinguishes the granularity. Consumers that need per-call attribution must compute deltas from cumulative counts.

### Event Types

All agent-specific event names are normalized to a common enum:

| Unified Type | Claude Code | OpenCode | Codex |
|-------------|------------|----------|-------|
| tool_call_start | PreToolUse | tool.execute.before | function_call |
| tool_call_end | PostToolUse | tool.execute.after | function_call_output |
| session_start | (first event) | session.created | session_meta |
| session_end | (inferred) | session.idle | (inferred) |
| turn_end | Stop | (inferred) | task_complete |
| user_prompt | UserPromptSubmit | chat.message | message (role=user) |
| agent_response | JSONL assistant | message.part.updated | message (role=assistant) |
| token_usage_report | JSONL usage | DB message cost | token_count |
| subagent_start | SubagentStart | (parent_id) | session_meta (source=subagent) |
| subagent_end | SubagentStop | (inferred) | (inferred) |
| error | PostToolUseFailure | session.error | (inferred from output) |

### Success/Failure

| Agent | Source | Available |
|-------|--------|-----------|
| Claude Code | Inferred from tool_response.stderr | Partial (Bash only) |
| OpenCode | event.session.error | Error events only |
| Codex | hook_event.success (hooks) | Full (hooks only) |
| Codex | exec_command_end.exit_code | Extended mode only |

Decision: `success: Optional[bool]` at top level. None when not determinable. This is a known asymmetry -- not all agents provide explicit success/failure for all tool types.

## 5. Tradeoffs

### Optional Fields vs. Required Fields

Most fields are Optional (default None). This was a deliberate choice:
- Different event types populate different fields (a session_start has no tool_name)
- Different agents provide different data (CC hooks lack timestamp)
- Requiring too many fields would make normalization fail silently by forcing dummy values

The tradeoff: code that accesses fields must check for None. This is preferable to code that accesses non-None values that are actually dummy/default values.

### Single Event Class vs. Event Type Hierarchy

We chose a single `SecondSightEvent` dataclass instead of a hierarchy (ToolCallEvent, SessionEvent, etc.) because:
1. POC simplicity -- one class to serialize/deserialize
2. JSON Schema export is simpler for a flat structure
3. Phase 1 can introduce a hierarchy if needed

The tradeoff: less type safety at the Python level. A `tool_call_start` event has `tool_name` populated but the type system doesn't enforce this.

### AgentMetadata: Typed Fields vs. Pure Overflow Dict

We chose typed fields in AgentMetadata over a pure `dict[str, Any]`:
- Pro: DC-3 compliance, IDE autocomplete, documentation
- Con: Adding a new agent-specific field requires a code change

The tradeoff: slightly more friction to add fields, but much safer against silent schema drift.

## 6. Schema Versioning

Every `SecondSightEvent` carries `schema_version: str = "0.1.0"`. The JSON Schema export also includes the version.

Migration path for v0.2.0:
1. Increment SCHEMA_VERSION
2. Add/modify fields
3. Write `event_from_dict` migration logic that checks `schema_version` and transforms old format to new
4. Old events remain readable as long as migration logic exists

This is intentionally simple for POC. Phase 1 may need a more robust migration framework.

## 7. What This Schema Cannot Do

1. **Per-tool-call token attribution**: No agent provides this. Token usage is per-message (CC, OC) or per-session-cumulative (Codex). Phase 2 must estimate.
2. **Guaranteed timestamps from CC hooks**: Claude Code hooks don't carry timestamps in the payload. System clock at hook invocation is the only source.
3. **Structured tool output parsing**: `tool_result` is always a string. Different tools produce different output formats. Phase 2 must implement per-tool parsers.
4. **Real-time sub-agent topology from Codex**: Codex sub-agent discovery requires scanning all JSONL files. The schema can represent sub-agent events but discovery is an adapter concern.
5. **Exit codes for non-Bash CC tools**: Claude Code PostToolUse has no exit_code field for non-Bash tools. The `success` field will be None for these.
