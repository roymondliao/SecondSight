# OpenCode Hook Mechanism Investigation

**Date:** 2026-04-24
**Source:** Official docs (https://opencode.ai/docs/plugins) + OpenCode source (sst/opencode@dev) + lazyagent reference (reference_opensoure/lazyagent/)
**Investigator note:** The original `opencode-ai/opencode` repo has been archived. The active project is `sst/opencode` (published as `opencode-ai` on npm), which opencode.ai docs now points to.

---

## Summary

OpenCode exposes two complementary access mechanisms for SecondSight:

1. **Plugin Hook API (official)** — JavaScript/TypeScript plugins loaded from `.opencode/plugins/` that receive typed hook callbacks including `tool.execute.before` and `tool.execute.after`. These run inside the Bun runtime, in-process.

2. **SQLite Direct Polling (unofficial, lazyagent approach)** — Read-only polling of `~/.local/share/opencode/opencode.db` (or `$OPENCODE_DATA_DIR/opencode.db`). Provides richer data including token counts and full message content, but is not a supported API.

**Verdict: partially_feasible.** The plugin hook API covers tool call type and arguments (tool.execute.before/after), session lifecycle, and text content. However, per-call timestamps are only reliable from DB polling (hooks don't emit start/end times for individual tool executions). Token usage per call is available via DB polling only (StepFinishPart in part table), not from plugin hooks. Additionally, SecondSight is Python-based while plugins require JS/TS running in Bun — a cross-language integration cost that must be factored in.

---

## Repository Identity

The task spec references `anomalyco/opencode`, but the build badge in `sst/opencode`'s README points to `anomalyco/opencode`. The active npm package is `opencode-ai`, hosted under `sst/opencode` on GitHub. The archived `opencode-ai/opencode` is a defunct Go-based prototype that moved to `charmbracelet/crush`.

**For this investigation, all source references are to `sst/opencode` (TypeScript/Bun implementation).**

---

## Investigation Steps

### Step 1: Official Documentation

`opencode.ai/docs/plugins` documents the plugin system. Key findings:

- Plugins are JS/TS files in `.opencode/plugins/` or `~/.config/opencode/plugins/`
- Plugins require Bun runtime (installed by OpenCode at startup)
- A plugin is a function that receives `{ project, client, $, directory, worktree }` and returns a `Hooks` object
- The `Hooks` object can define event subscribers and hook handlers

**Complete event list from official docs:**
- Command events: `command.executed`
- File events: `file.edited`, `file.watcher.updated`
- Installation events: `installation.updated`
- LSP events: `lsp.client.diagnostics`, `lsp.updated`
- Message events: `message.part.removed`, `message.part.updated`, `message.removed`, `message.updated`
- Permission events: `permission.asked`, `permission.replied`
- Server events: `server.connected`
- Session events: `session.created`, `session.compacted`, `session.deleted`, `session.diff`, `session.error`, `session.idle`, `session.status`, `session.updated`
- Todo events: `todo.updated`
- Shell events: `shell.env`
- Tool events: `tool.execute.after`, `tool.execute.before`
- TUI events: `tui.prompt.append`, `tui.command.execute`, `tui.toast.show`
- Experimental: `experimental.session.compacting`, `experimental.compaction.autocontinue`, `experimental.text.complete`, `experimental.chat.messages.transform`, `experimental.chat.system.transform`

### Step 2: Source Code Investigation

From `packages/plugin/src/index.ts` (the `@opencode-ai/plugin` package):

```typescript
export interface Hooks {
  event?: (input: { event: Event }) => Promise<void>

  "tool.execute.before"?: (
    input: { tool: string; sessionID: string; callID: string },
    output: { args: any },
  ) => Promise<void>

  "tool.execute.after"?: (
    input: { tool: string; sessionID: string; callID: string; args: any },
    output: {
      title: string
      output: string
      metadata: any
    },
  ) => Promise<void>

  "chat.message"?: (
    input: {
      sessionID: string
      agent?: string
      model?: { providerID: string; modelID: string }
      messageID?: string
      variant?: string
    },
    output: { message: UserMessage; parts: Part[] },
  ) => Promise<void>

  "permission.ask"?: (input: Permission, output: { status: "ask" | "deny" | "allow" }) => Promise<void>
  "shell.env"?: (input: { cwd: string; sessionID?: string; callID?: string }, output: { env: Record<string, string> }) => Promise<void>
  "experimental.session.compacting"?: (input: { sessionID: string }, output: { context: string[]; prompt?: string }) => Promise<void>
}
```

Key observations:
- `tool.execute.before` gives: tool name, sessionID, callID — plus `output.args` (modifiable) which carries the tool arguments
- `tool.execute.after` gives: tool name, sessionID, callID, args — plus `output.output` (string result), `output.title`, `output.metadata`
- **No timestamp fields** in either hook. Time must be derived from DB write timestamps
- **No token usage** in either hook. Only available from DB polling via `StepFinishPart`

### Step 3: Event Catalog

The generic `event` hook fires for all event types via the `EventEmitter` bus in `src/bus/index.ts`. All events are published via `Bus.publish()` which calls `GlobalBus.emit("event", { directory, project, workspace, payload })`.

Key session events and their payloads (from `src/session/session.ts`):
- `session.created`: `{ sessionID }`
- `session.updated`: `{ sessionID }` (plus busSchema carries full session info)
- `session.deleted`: `{ sessionID }`
- `session.diff`: `{ sessionID, diff: FileDiff[] }`
- `session.error`: `{ sessionID?, error }` — error is discriminated union

Key message events (from `src/session/message-v2.ts`):
- `message.updated`: `{ sessionID, info: Message }` — Message has role, cost, tokens, agent, model
- `message.part.updated`: `{ sessionID, part: Part, time: number }` — Part can be ToolPart (has tool name, callID, state with input/output)
- `message.part.delta`: `{ sessionID, messageID, partID, field, delta }` — streaming delta

### Step 4: ToolPart Schema (from DB and part.updated event)

The `ToolPart` schema (from `message-v2.ts`):
```typescript
ToolPart = {
  id, sessionID, messageID,
  type: "tool",
  callID: string,
  tool: string,        // tool name
  state: ToolState,    // discriminated union
  metadata?: Record<string, any>
}

ToolStateCompleted = {
  status: "completed",
  input: Record<string, any>,  // tool arguments
  output: string,              // tool result
  title: string,
  metadata: Record<string, any>,
  time: { start: number, end: number, compacted?: number },
  attachments?: FilePart[]
}
```

This is the richest tool call data available — full input, output, and timestamps. Available via DB polling and via the `message.part.updated` plugin event.

### Step 5: Cross-validation with lazyagent

Lazyagent's opencode integration (`reference_opensoure/lazyagent/internal/opencode/`):
- Reads `~/.local/share/opencode/opencode.db` (SQLite, read-only)
- Queries `session`, `message`, `part` tables
- `part.data` column contains JSON-serialized PartData: `{ type, text, tool, callID, state }`
- `state` contains `{ status, input, output }` — which matches `ToolStateCompleted`

**Cross-validation result:** lazyagent's schema matches `sst/opencode`'s current source at the time of investigation. The `part.data` column structure is consistent. However, lazyagent was written against a specific version — the schema has no explicit versioning guarantee.

**Discrepancy found:** lazyagent's `partData` uses `text`, `tool`, `callID`, `state` (flat JSON). The current source uses full typed `PartData = Omit<MessageV2.Part, "id" | "sessionID" | "messageID">` which is a discriminated union across multiple part types. The lazyagent flat structure is a subset — it only reads `type`, `text`, `tool`, `callID`, `state`, consistent with the older simpler schema.

### Step 6: SecondSight Requirements Mapping

| SecondSight Need | Available via Plugin API | Available via DB Polling |
|---|---|---|
| Tool call type | `tool.execute.before` (input.tool) | `part.data.tool` |
| Tool call arguments | `tool.execute.before` (output.args) | `part.data.state.input` |
| Tool call results | `tool.execute.after` (output.output) | `part.data.state.output` |
| Timestamp start | NOT AVAILABLE from hooks | `part.data.state.time.start` |
| Timestamp end | NOT AVAILABLE from hooks | `part.data.state.time.end` |
| Token usage per call | NOT AVAILABLE from hooks | `StepFinishPart.tokens` (message-level) |
| Session start | `session.created` event | `session.time_created` |
| Session end/idle | `session.idle` event | `session.time_updated` + status inference |
| Session error | `session.error` event | last message role + finish field |
| User prompt content | `chat.message` hook (output.message) | `message.data` where role=user |
| Agent response content | `message.part.updated` (TextPart) | `part.data.text` |
| Sub-agent spawning | `session.created` with parent_id | `session.parent_id != null` |

### Step 7: Coverage Rate

**SecondSight required data categories (8):**
1. Tool call type — available via hooks AND DB
2. Tool call arguments — available via hooks AND DB
3. Tool call results — available via `tool.execute.after` AND DB
4. Timestamp start/end — DB ONLY
5. Token usage per call — DB ONLY (StepFinishPart, message-level granularity)
6. Session lifecycle (start/end/error) — available via hooks AND DB
7. User prompt content — available via `chat.message` hook AND DB
8. Sub-agent spawning — available via DB (parent_id); plugin events don't expose parent_id directly

**Plugin API alone:** covers 6/8 (missing per-call timestamps, token usage)
**DB polling alone:** covers 8/8 (all fields present in stored data)
**Plugin API + DB polling:** covers 8/8

Coverage rate (plugin API sufficient fields): 75% (6/8)
Coverage rate (DB polling sufficient fields): 100% (8/8)
Coverage rate (combined): 100% (8/8)

For this report's YAML, we document "available_event_types = 8" (two access mechanisms offering different event granularities), "sufficient_field_event_types = 6" (plugin API only — conservative approach since DB polling is unofficial).

### Step 8: Known Limitations, Extensibility, and Stability Risks

**Limitations:**

1. **Plugin runtime is Bun/JS/TS only.** SecondSight is Python. To use the plugin hook API, a JS/TS bridge plugin must be written that forwards events to SecondSight (e.g., via HTTP or IPC). This adds a runtime dependency (Bun) and maintenance surface.

2. **No per-call timestamps from hooks.** `tool.execute.before/after` don't emit start/end timestamps. These must be reconstructed from DB polling (`part.data.state.time`) or from wall-clock time at hook call.

3. **Token usage is message-level, not tool-call-level.** `StepFinishPart.tokens` gives token counts per LLM step (which may contain multiple tool calls), not per individual tool invocation.

4. **DB polling is unofficial.** The SQLite schema is internal. OpenCode does not guarantee schema stability across versions. lazyagent's cross-validation proves the schema at a specific git snapshot, not across future versions.

5. **event hook carries opaque `Event` type.** The generic `event?: (input: { event: Event }) => Promise<void>` hook fires for all events but the Event type comes from `@opencode-ai/sdk` — it's a union type. Without TypeScript type checking at runtime, discriminating event types requires runtime checks.

6. **Parent_id not exposed in plugin events.** Sub-agent detection requires either polling the `session` table for `parent_id != null` or hooking `session.created` and separately querying the DB to get parent_id.

**Extensibility (open source advantage):**

Since `sst/opencode` is MIT-licensed TypeScript, SecondSight could:
- Add new Bus events with richer payloads (e.g., add timestamp to tool events)
- Create a dedicated observer plugin that streams events to SecondSight over HTTP
- Modify the storage schema to add indexes for faster polling

Cost: maintaining a fork or contributing upstream. Upstream contribution may be rejected if tool event timestamps are considered internal.

**Stability risk:**

The plugin API (`@opencode-ai/plugin`) is versioned on npm. Breaking changes would require a version bump — this provides some stability signal. However, the `experimental.*` hooks are explicitly unstable.

The SQLite schema has no explicit version contract. Schema migrations in `packages/opencode/src/storage/storage.ts` use Effect framework migrations — these could add/remove/rename columns without notice.

### Step 9: Feasibility Verdict

**Verdict: partially_feasible**

OpenCode provides a formally supported plugin hook system (`tool.execute.before`, `tool.execute.after`, session events) that covers the core tool call classification needs. However:

- Plugin hooks require Bun/JS/TS — not directly callable from Python
- Per-call timestamps require DB polling to be precise
- Token usage per call requires DB polling (message-level only via hooks)
- Sub-agent spawning requires DB polling for parent_id

**Recommended approach:** Combine both mechanisms. Use DB polling as the primary data source (covers all 8 requirements) with plugin hooks as the injection/interception mechanism when directive injection into tool calls is needed. The Python↔Bun bridge is a non-trivial cost that should be assessed in Phase 1.

---

## DC-1 Cross-Validation Check

**DC-1: Hook investigation reports "feasible" but event payloads are too shallow.**

Evidence gathered at payload level:
- `tool.execute.before`: `input.tool` (string), `input.sessionID`, `input.callID`; `output.args` (any — modifiable)
- `tool.execute.after`: `input.tool`, `input.sessionID`, `input.callID`, `input.args` (any); `output.output` (string), `output.title` (string), `output.metadata` (any)

**Shallow payload assessment:** `output.output` is typed as `string` but no schema constraint enforces its content. Tool results from bash would be shell output; read would be file content. The `output.metadata` field is `any` — no documented subfields. This is a risk for Phase 2 analysis that expects structured results.

**Missing from hooks:** Start timestamp, end timestamp, token count. These require DB polling.

Verdict on DC-1: The plugin hook payloads are NOT shallow for tool classification purposes, but ARE shallow for timing and cost analysis. Investigation correctly reflects this as `partially_feasible`.
