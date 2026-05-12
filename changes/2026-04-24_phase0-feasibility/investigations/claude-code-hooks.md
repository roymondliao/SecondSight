# Claude Code Hook Mechanism Investigation

**Date:** 2026-04-24
**Agent:** Claude Code
**Verdict:** PARTIALLY_FEASIBLE
**Investigator:** SecondSight Task 1

---

## Evidence Sources

This investigation used four evidence sources, ordered by reliability:

1. **Live settings.json** (`~/.claude/settings.json`) — Production hook configuration showing 6 hook types: `UserPromptSubmit`, `Stop`, `PostToolUse`, `PostToolUseFailure`, `PermissionRequest`, `PreToolUse`
2. **observagent relay.py** — Live payload inspection comments document confirmed PostToolUse payload fields: `session_id`, `transcript_path`, `cwd`, `permission_mode`, `hook_event_name`, `tool_name`, `tool_use_id`, `tool_input`, `tool_response`
3. **Live JSONL transcript** (`~/.claude/projects/.../cb27db96....jsonl`) — Confirmed actual transcript schema from Claude Code version 2.1.85
4. **Reference implementations** — `claude-code-langfuse-template` (Stop hook) and `observagent` (PreToolUse/PostToolUse/SubagentStart/SubagentStop)

Official documentation was not accessible via WebFetch (docs site not reachable from this environment). Investigation proceeded from evidence sources above, which are higher-confidence than documentation (they reflect actual runtime behavior).

---

## Hook Types Catalog

### 1. PreToolUse
- **Trigger:** Before each tool call executes
- **Can block execution:** Yes (via exit code or stdout JSON)
- **Payload (confirmed):** `session_id`, `hook_event_name`, `tool_name`, `tool_use_id`, `tool_input`, `transcript_path`, `cwd`, `permission_mode`
- **SecondSight use:** Tool call type + arguments (primary action classification signal)
- **Key limitation:** No result data (tool hasn't run); `matcher` field can silently exclude tools

### 2. PostToolUse
- **Trigger:** After each tool call completes
- **Can block execution:** No
- **Payload (confirmed):** Same as PreToolUse + `tool_response` object
- **SecondSight use:** Tool call result for outcome tracking
- **Key limitation (confirmed by observagent):** NO explicit `exit_status` field. For Bash: `tool_response.stderr` signals failure. For Read/Write/Edit/Grep: no reliable failure signal in hook payload — requires JSONL transcript for `is_error` flag.

### 3. PostToolUseFailure
- **Trigger:** When a tool call fails
- **Payload (partially confirmed):** Hook existence confirmed from settings.json. Schema inferred from PostToolUse pattern. FIELD NAMES NOT VERIFIED.
- **SecondSight use:** Primary failure signal (complementing PostToolUse stderr heuristic)
- **Key limitation:** Neither reference project implements this hook. Version support unknown.

### 4. Stop
- **Trigger:** After each complete assistant response (turn end)
- **Payload (confirmed):** `session_id`, `hook_event_name`, `transcript_path`
- **SecondSight use:** Turn lifecycle + access to transcript for full-turn data
- **Key limitation:** Hook payload is minimal. Rich data (prompt, response, token usage) requires reading JSONL transcript via `transcript_path`.

### 5. UserPromptSubmit
- **Trigger:** When user submits a new prompt
- **Payload (unverified):** Hook existence confirmed from settings.json. Whether payload includes prompt content is UNKNOWN.
- **SecondSight use:** User prompt content (if payload includes it)
- **Key limitation:** Not used by any reference project. Payload schema completely unverified.

### 6. PermissionRequest
- **Trigger:** When Claude requests permission for a potentially dangerous action
- **Payload (unverified):** Hook existence confirmed from settings.json. Schema unknown.
- **SecondSight use:** Governance data for Phase 3B
- **Key limitation:** Langfuse-template reads a separate `~/.claude/logs/permission-events.jsonl` file for permission data, suggesting hooks may not be the primary channel for this.

### 7. SubagentStart
- **Trigger:** When a sub-agent (Task tool spawn) begins
- **Payload (confirmed):** `session_id`, `agent_id`, `agent_type`, `agent_transcript_path`
- **SecondSight use:** Multi-agent tree construction
- **Key limitation:** Task description not in this payload — must be captured at PreToolUse time from `tool_input.description`

### 8. SubagentStop
- **Trigger:** When a sub-agent completes
- **Payload (confirmed):** `session_id`, `agent_id`, `agent_type`
- **SecondSight use:** Mark subagent completion in agent tree
- **Key limitation:** NOT fired on abnormal termination (crash/kill). Requires stale detection timeout.

---

## JSONL Transcript Format

The JSONL transcript at `~/.claude/projects/<encoded-path>/<session-uuid>.jsonl` is the richest data source, confirmed from live file inspection (version 2.1.85).

**Confirmed fields:**
- `type`: `"user"` | `"assistant"` | `"file-history-snapshot"`
- `message.role`, `message.content` (array of text/tool_use/tool_result/thinking blocks)
- `message.id` — unique message ID; required for streaming deduplication
- `message.model` — e.g., `"claude-sonnet-4-6"`
- `message.usage` — `{input_tokens, output_tokens, cache_read_input_tokens, cache_creation_input_tokens, cache_creation.ephemeral_5m/1h_input_tokens}`
- `message.stop_reason` — `"tool_use"` | `"end_turn"` | `null` (streaming chunks)
- `sessionId`, `timestamp`, `cwd`, `version`, `uuid`, `parentUuid`
- `toolUseResult` — `{stdout, stderr, interrupted, isImage, noOutputExpected}` (on user-type records that are tool results)
- `message.content[tool_use].name`, `.input`, `.id`
- `message.content[tool_result].tool_use_id`, `.content`, `.is_error`

**CRITICAL DEDUPLICATION REQUIREMENT:** Claude Code emits multiple `assistant`-type records per message ID during streaming. All have `stop_reason: null`. Only the LAST record for a given `message.id` has the accurate final `output_tokens` count. Deduplication by `message.id` keeping last occurrence is mandatory.

**Stability risk:** This is an INTERNAL format. It has already evolved (cache token breakdown fields added). No official API stability guarantee.

---

## SecondSight Needs Coverage

| Need | Via Hooks | Via JSONL | Status |
|------|-----------|-----------|--------|
| Tool call type | PreToolUse.tool_name | transcript content[tool_use].name | Fully covered (hooks) |
| Tool call arguments | PreToolUse.tool_input | transcript content[tool_use].input | Fully covered (hooks) |
| Tool call results | PostToolUse.tool_response (partial) | transcript toolUseResult + content[tool_result] | Partially via hooks, fully via JSONL |
| Timestamps (start/end) | Hook invocation time + PreToolUse/PostToolUse pairing | transcript.timestamp | Fully covered (hooks) |
| Token usage per call | NOT available | transcript message.usage | JSONL only |
| Session lifecycle | Stop, SubagentStart/Stop | sessionId in all records | Fully covered (hooks) |
| User prompt content | UserPromptSubmit (UNVERIFIED) | transcript type:user records | JSONL confirmed; hook unverified |
| Agent response content | NOT available | transcript type:assistant records | JSONL only |
| Sub-agent spawning | SubagentStart/Stop | subagent JSONL files | Fully covered (hooks) |

**Coverage rate:** 71.4% via hooks alone (5 of 7 required types fully covered)
**Coverage with JSONL:** ~100% (all needs addressable, subject to format stability risk)

---

## Feasibility Verdict

**PARTIALLY_FEASIBLE** with a required two-tier data access pattern:

**Tier 1 — Hooks (real-time, stable API):**
- Captures: tool call type, tool arguments, timing, session lifecycle, sub-agent events
- Sufficient for: action classification, duration tracking, agent tree construction
- Hooks are the reliable, officially-supported mechanism

**Tier 2 — JSONL Transcript (post-turn, internal format):**
- Captures: token usage, full user prompt, full assistant response, tool results with is_error
- Required for: cost tracking, context monitoring, complete failure attribution
- Risk: internal format, no stability guarantee

**Why not infeasible:** Both reference projects have shipped production systems using this approach. The pattern is viable; the risk is managed by treating JSONL as supplemental with format monitoring.

**Why not feasible (without qualification):** Token usage is not in hooks. PostToolUse lacks explicit exit_status. UserPromptSubmit payload is unverified. JSONL format stability is not guaranteed.

---

## Cross-Validation: Reference Projects vs. Production Settings

| Hook Type | observagent | langfuse-template | Production settings.json |
|-----------|-------------|-------------------|--------------------------|
| PreToolUse | YES | NO | YES |
| PostToolUse | YES | NO | YES |
| Stop | NO | YES | YES |
| SubagentStart | YES | NO | not observed directly |
| SubagentStop | YES | NO | not observed directly |
| UserPromptSubmit | NO | NO | YES |
| PostToolUseFailure | NO | NO | YES |
| PermissionRequest | NO | NO | YES |

The reference projects together cover only 5 of 8 confirmed hook types. 3 hook types (UserPromptSubmit, PostToolUseFailure, PermissionRequest) exist in production but are not used by any reference implementation. This confirms DC-3: investigation relying only on reference projects would have missed 3 hook types.

---

## Known Limitations and Stability Risks

1. **JSONL transcript format is internal** — most critical risk for long-term stability
2. **PostToolUse lacks exit_status** — failure detection for non-Bash tools requires JSONL
3. **UserPromptSubmit payload schema unverified** — user prompt content source uncertain
4. **PostToolUseFailure schema unknown** — need live verification before relying on it
5. **SubagentStop unreliable on crash** — stale detection required
6. **Hook matcher can silently filter events** — must use `"*"` matcher or no matcher
7. **Concurrent session race conditions** — transcript reading needs session identification
8. **Streaming deduplication required** — JSONL emits multiple chunks per message ID
