# Codex CLI — Hook Mechanism Investigation

**Investigation Date:** 2026-04-24
**Target:** Codex CLI (openai/codex, codex-rs/ Rust implementation)
**Verdict:** PARTIALLY_FEASIBLE

---

## Target Clarification

This investigation targets **Codex CLI** — the open-source Rust-based local coding agent
distributed via `npm i -g @openai/codex` and `brew install --cask codex`. Source code:
https://github.com/openai/codex (specifically `codex-rs/`).

This is **not** the same as:

- **Codex Web** (chatgpt.com/codex) — a cloud-based coding service. No local observation possible.
- **Codex API** (OpenAI's legacy code completion API) — deprecated, unrelated.

The Rust implementation (`codex-rs/`) is the current maintained version and replaced the
legacy TypeScript CLI. All findings below apply to the Rust CLI.

---

## Evidence Sources

1. `codex-rs/protocol/src/protocol.rs` — Defines `EventMsg` enum (all event types), `Op` enum
   (all submission operations), `RolloutItem` enum (what gets written to JSONL files),
   `SessionMeta`, `TurnContextItem`, `ExecCommandBeginEvent`, `ExecCommandEndEvent`,
   `PatchApplyEndEvent`, `TokenCountEvent`, hook-related enums (`HookEventName`, `HookRunSummary`).
   **5,423 lines — primary source of truth for event schemas.**

2. `codex-rs/rollout/src/policy.rs` — Defines `EventPersistenceMode` (Limited vs Extended) and
   exactly which events are persisted in each mode. **Critical for understanding what SecondSight
   can read from default JSONL files.**

3. `codex-rs/rollout/src/recorder.rs` — `RolloutRecorder` implementation. Confirms JSONL file
   path pattern (`~/.codex/sessions/rollout-TIMESTAMP-UUID.jsonl`) and async write mechanics.

4. `codex-rs/hooks/src/types.rs` — `HookPayload`, `HookEvent`, `HookEventAfterToolUse`,
   `HookEventAfterAgent` types. **Confirmed post_tool_use hook payload fields via stable wire
   shape test in the same file (lines 160-292).**

5. `codex-rs/hooks/src/lib.rs` — Lists all public hook exports: pre_tool_use, post_tool_use,
   session_start, user_prompt_submit, stop, permission_request.

6. `reference_opensoure/lazyagent/internal/codex/process.go` — Third-party JSONL parser.
   Independently confirms wire format for: `session_meta`, `turn_context`, `response_item`
   (message/function_call/function_call_output), `event_msg` (token_count, task_complete,
   user_message, agent_message). **Cross-validates source code findings.**

7. `reference_opensoure/lazyagent/internal/codex/process_test.go` — Synthetic JSONL fixtures
   showing exact line structure. Confirms `rollout-YYYY-MM-DDT...UUID.jsonl` naming pattern
   and `~/.codex/sessions/` directory. Also confirms `~/.codex/session_index.jsonl` for
   thread-name mapping.

---

## Architecture: Two Observation Surfaces

Codex CLI provides two distinct ways for SecondSight to observe agent activity:

### Surface 1: JSONL Rollout Files (Primary)

**Location:** `~/.codex/sessions/rollout-TIMESTAMP-UUID.jsonl`

**Format:** One JSON object per line, envelope structure:
```json
{"timestamp": "2026-03-28T11:26:17.785Z", "type": "session_meta", "payload": {...}}
{"timestamp": "2026-03-28T11:26:17.900Z", "type": "turn_context", "payload": {...}}
{"timestamp": "2026-03-28T11:26:18.000Z", "type": "response_item", "payload": {"type": "message", "role": "user", "content": [...]}}
{"timestamp": "2026-03-28T11:26:19.000Z", "type": "response_item", "payload": {"type": "function_call", "name": "exec_command", "arguments": "..."}}
{"timestamp": "2026-03-28T11:26:20.000Z", "type": "response_item", "payload": {"type": "function_call_output", "call_id": "...", "output": "..."}}
{"timestamp": "2026-03-28T11:26:21.000Z", "type": "event_msg", "payload": {"type": "token_count", "info": {...}}}
```

**Access method:** File polling (watch `~/.codex/sessions/` for new `.jsonl` files, tail-follow
existing files). No authentication required. No API calls needed.

**Two persistence tiers:**
- **Limited** (default): session_meta, response_items (messages, function_calls, function_call_outputs),
  task_started, task_complete, token_count, user_message, agent_message, context_compacted, undo_completed,
  thread_name_updated, image_generation_end.
- **Extended** (configurable): Adds exec_command_end (with exit_code, stdout, stderr),
  patch_apply_end (with success flag), guardian_assessment, web_search_end, collab_agent events.

### Surface 2: Hook Callbacks (Secondary)

**Registration:** `~/.codex/config.toml` `[hooks]` section.

**Hook event names:** `pre_tool_use`, `post_tool_use`, `session_start`, `user_prompt_submit`,
`stop`, `permission_request`.

**Payload delivery:** JSON via stdin to a subprocess. Each tool call spawns the registered
hook process.

**Hook handler types:** `command` (shell subprocess), `prompt` (model prompt injection),
`agent` (sub-agent delegation). For SecondSight, `command` is the relevant type.

---

## Event Coverage Analysis

### Required by SecondSight (Phase 2)

| Requirement | Available? | Source | Notes |
|---|---|---|---|
| Tool call type | Yes | `response_item/function_call.name` | JSONL Limited mode |
| Tool call arguments | Yes | `response_item/function_call.arguments` | JSON string, requires parsing |
| Tool call result | Yes | `response_item/function_call_output.output` | Unstructured string |
| Turn start time | Yes | `event_msg/task_started.started_at` + envelope timestamp | JSONL Limited mode |
| Turn end time | Yes | `event_msg/task_complete.completed_at` + `duration_ms` | JSONL Limited mode |
| Token usage (session) | Yes (cumulative) | `event_msg/token_count.info.total_token_usage` | Per-turn via delta; per-call NOT available |
| Token usage (per call) | NO | — | Not available at any tier |
| Session start/lifecycle | Yes | `session_meta.id`, `session_meta.source` | JSONL Limited mode |
| User prompt content | Yes | `response_item/message` (role=user) | JSONL Limited mode |
| Agent response content | Yes | `response_item/message` (role=assistant) | JSONL Limited mode |
| Sub-agent spawning | Partial | `session_meta.source = subagent` | Scan-based, no spawning event |
| Exit codes | Extended only | `event_msg/exec_command_end.exit_code` | NOT in Limited mode |
| Explicit failure flags | Extended only | `event_msg/exec_command_end.status` | NOT in Limited mode |

**Coverage rate: 7/8 needed types (87%) in Limited mode.**

### Critical Gap: Extended Mode for Rich Tool Execution Data

The most valuable tool execution fields — explicit `exit_code`, structured `stdout`/`stderr`,
and `success` boolean — are in `event_msg/exec_command_end` and `event_msg/patch_apply_end`,
which are **Extended mode only**. The default Codex CLI installation uses Limited mode.

SecondSight has two options:

1. **Configure Extended mode**: Add `event_persistence_mode = "extended"` to `~/.codex/config.toml`.
   This makes the JSONL file more informative but requires user config change.

2. **Parse unstructured output**: Use `response_item/function_call_output.output` (Limited mode)
   and extract exit codes via heuristics (look for "exit code N" patterns, non-empty output = success, etc.).

The lazyagent reference implementation uses option 2 — it tracks `apply_patch` as `last_file_write`
but does NOT parse exit codes from the output string.

---

## Comparison with Claude Code Hooks

| Dimension | Claude Code | Codex CLI |
|---|---|---|
| Primary mechanism | Hook callbacks (settings.json) | JSONL rollout files |
| Secondary mechanism | JSONL transcript | Hook callbacks (config.toml) |
| Tool call arguments | PreToolUse.tool_input | response_item/function_call.arguments |
| Tool call results | PostToolUse.tool_response | function_call_output.output |
| Exit codes | Not in hooks (stderr heuristic) | exec_command_end.exit_code (Extended) |
| Token usage | JSONL only, per-message | JSONL token_count, per-turn delta |
| Session identity | session_id in hook payload | JSONL session_meta.id |
| Sub-agent spawning | SubagentStart hook | Scan sub-agent JSONL files |
| Per-call duration | No | post_tool_use hook: duration_ms |
| Config requirement | settings.json (user-level) | config.toml (user-level) |

**Key difference:** Claude Code's hook callbacks are richer for real-time observation, while
Codex CLI's JSONL files are the primary surface and more complete for post-hoc analysis.
For SecondSight, **Codex CLI is actually EASIER to observe** because JSONL files require
no configuration changes — just file watching.

---

## Sub-Agent Spawning: The Hard Gap

Codex CLI supports multi-agent collaboration via `ThreadSpawn` (source type in `session_meta`).
When a sub-agent is spawned:

1. The **parent session** does NOT get a spawning event in its JSONL file.
2. The **sub-agent** creates a NEW JSONL file in `~/.codex/sessions/` with:
   `session_meta.source = {"subagent": {"thread_spawn": {"parent_thread_id": "...", "depth": N, "agent_path": "..."}}}`

SecondSight can discover sub-agents only by:
- Scanning all JSONL files and matching `source.subagent.thread_spawn.parent_thread_id` to known sessions.
- This is polling-based, not event-based.

Real-time sub-agent topology is not observable. There is no spawning event in the parent session.

The collab events (`CollabAgentSpawnBegin`, `CollabAgentSpawnEnd`, `CollabAgentInteractionBegin`, etc.)
in the EventMsg enum are Extended-mode-only and are NOT persisted in Limited mode.

---

## Hook Callback Payload: Confirmed Fields

From `codex-rs/hooks/src/types.rs` (wire-shape tests confirm exact JSON structure):

### AfterToolUse (post_tool_use) hook — CONFIRMED
```json
{
  "session_id": "uuid",
  "cwd": "/path/to/project",
  "triggered_at": "2025-01-01T00:00:00Z",
  "hook_event": {
    "event_type": "after_tool_use",
    "turn_id": "turn-2",
    "call_id": "call-1",
    "tool_name": "local_shell",
    "tool_kind": "local_shell",
    "tool_input": {
      "input_type": "local_shell",
      "params": {
        "command": ["cargo", "fmt"],
        "workdir": "codex-rs",
        "timeout_ms": 60000,
        "sandbox_permissions": "use_default",
        "justification": null,
        "prefix_rule": null
      }
    },
    "executed": true,
    "success": true,
    "duration_ms": 42,
    "mutating": true,
    "sandbox": "none",
    "sandbox_policy": "danger-full-access",
    "output_preview": "ok"
  }
}
```

**Note:** `output_preview` is explicitly truncated. Full output requires JSONL.

### AfterAgent hook — CONFIRMED
```json
{
  "session_id": "uuid",
  "cwd": "/path",
  "triggered_at": "2025-01-01T00:00:00Z",
  "hook_event": {
    "event_type": "after_agent",
    "thread_id": "uuid",
    "turn_id": "turn-1",
    "input_messages": ["hello"],
    "last_assistant_message": "hi"
  }
}
```

### session_start, user_prompt_submit, stop, permission_request — UNVERIFIED
Payload structure inferred from hook callback pattern (session_id, cwd, triggered_at).
Source files for these events were not directly inspected.

---

## Feasibility Verdict: PARTIALLY_FEASIBLE

**Feasible (default JSONL, Limited mode):**
- Tool call type and arguments: `response_item/function_call`
- Tool call output (unstructured): `response_item/function_call_output.output`
- Turn lifecycle (start/end with timing): `event_msg/task_started` + `event_msg/task_complete`
- Token usage (per-turn delta, cumulative): `event_msg/token_count`
- Session identity and context: `session_meta` + `turn_context`
- User prompts and agent responses: `response_item/message`

**Requires configuration (Extended mode):**
- Explicit exit codes: `event_msg/exec_command_end.exit_code`
- Structured stdout/stderr per command: `event_msg/exec_command_end.stdout/stderr`
- Explicit success/failure flags: `event_msg/exec_command_end.status`
- File-level change tracking: `event_msg/patch_apply_end.changes`

**Not available:**
- Per-tool-call token usage (only session-cumulative)
- Sub-agent spawning events in parent session (scan-based discovery only)
- Explicit session termination event

---

## Recommended SecondSight Integration Strategy

### Tier 1 (Immediate, no config required): JSONL file watching
1. Watch `~/.codex/sessions/` for new `.jsonl` files
2. Tail-follow each file for new events
3. Parse `session_meta` → `turn_context` → `response_item` entries
4. Infer exit status from `function_call_output.output` content heuristics

### Tier 2 (Enhanced, requires config): Extended persistence mode
1. Guide users to add `event_persistence_mode = "extended"` to `~/.codex/config.toml`
2. Parse `event_msg/exec_command_end` for structured execution data
3. Use `exit_code` field directly (no heuristics needed)

### Tier 3 (Optional): Hook callbacks for real-time streaming
1. Register post_tool_use hook in `~/.codex/config.toml`
2. Receive per-call `duration_ms` and `success` boolean in real-time
3. Useful for live dashboards but redundant with JSONL for analysis

---

## Known Limitations

1. **Extended mode gap is the primary risk.** The most useful fields (exit_code, structured output)
   require a config change most users haven't made.

2. **JSONL function_call.arguments is a JSON-encoded string.** It must be double-parsed.
   Large patches in apply_patch arguments are truncated by the compact tool.

3. **Token usage is not per-call.** Phase 2 cost attribution per tool call requires
   estimation or distributional approaches.

4. **Sub-agent topology requires polling.** No streaming sub-agent graph available.

5. **JSONL format is internal and may change.** The Codex CLI team has not documented
   JSONL as a stable external API. Field names could change in future releases.
   The `cli_version` field in `session_meta` enables version-gated parsing.
