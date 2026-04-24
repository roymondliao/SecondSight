# Injection Feasibility Investigation

**Date:** 2026-04-24
**Task:** P0-5 — Runtime Injection Feasibility Test
**Sources:** Official docs (Claude Code, OpenCode, Codex), reference open-source code (claude-code-langfuse-template, observagent, lazyagent)

---

## Purpose

This investigation determines whether SecondSight can inject directives into agent sessions — both during active sessions (runtime) and before sessions begin (session-start). The goal is to identify all injection surfaces, assess their reliability, and determine the primary injection strategy.

**Death cases this investigation is required to cover:**
- DC-1: Agent drops injected content during context compaction or context overflow
- DC-2: Agent ignores injected directive despite receiving it (acknowledgment without behavioral compliance)
- DC-3: Injection works in short sessions but silently fails in long sessions under heavy context load

---

## Claude Code

### Injection Surfaces

#### 1. CLAUDE.md Files (Session-Start)

**Method:** Write directives into CLAUDE.md files before session starts. Claude Code reads these at session initialization. Multiple scopes exist: `~/.claude/CLAUDE.md` (global), `<project>/.claude/CLAUDE.md` (project-level), and `<subdir>/CLAUDE.md` (subdirectory-level).

**Format:** Markdown text. No strict size limits documented, but large CLAUDE.md files may compete for context budget with the task at hand.

**Persistence:** Loaded once at session start. **Critical unknown:** Claude Code does NOT re-read CLAUDE.md files on every turn — they are loaded into context at session initialization only. Modifications to CLAUDE.md during an active session do NOT affect the current session. Confirmed by the langfuse reference implementation, which shows no mechanism for mid-session context reloading.

**Latency:** Zero (content is in context from turn 1). However, effect is only visible at next session start, not current session.

**Officially supported:** Yes. CLAUDE.md is a first-class Claude Code feature.

**Stability risk:** Low. CLAUDE.md is core UX for Claude Code.

**Verdict:** `session_start` — viable for session-start injection. Not viable for runtime injection within active session. Content priority relative to user instructions is unverified — anecdotal evidence suggests CLAUDE.md content can be overridden by direct user instructions (DC-2 risk).

**Compaction risk (DC-1):** When Claude Code performs context compaction on a long session, CLAUDE.md content may be summarized or dropped. The reference code (lazyagent's `time_compacting` analogue for Claude Code) confirms compaction occurs. Whether CLAUDE.md content survives compaction is **unverified** — it likely depends on the compaction summary quality.

---

#### 2. PreToolUse Hook — Output to stdin injection (Runtime)

**Method:** Register a `PreToolUse` hook in `~/.claude/settings.json`. The hook receives tool call data via stdin (JSON). The hook can **output JSON to stdout to inject additional context** — Claude Code reads hook stdout and can incorporate it.

**Format:** Hook stdout is a JSON response. The official hook contract allows outputting a `continue: true/false` decision and optional `reason` message. Per the observagent reference, hooks write to a local HTTP server, not directly to Claude's context.

**Critical distinction:** PreToolUse hooks in Claude Code's documented API allow the hook to:
- `continue: false` — block the tool call
- `continue: true` — allow it
- Optionally provide a `reason` that appears in the UI

The hook **cannot directly append to the system prompt or inject arbitrary text into the conversation context** via stdout. This is a fundamental limitation. The hook intercepts the action decision, not the context.

**Verdict:** `runtime` — **not_viable** for directive injection. PreToolUse hooks cannot inject content into the conversation context; they only influence the tool execution decision.

**Compaction risk (DC-1):** N/A — hooks don't inject content into context.

---

#### 3. PostToolUse Hook — Observation Only (Runtime)

**Method:** Register a `PostToolUse` hook to observe tool outputs. Same mechanism as PreToolUse.

**Critical limitation:** Same as PreToolUse — PostToolUse hooks cannot inject content into conversation context. They fire after the tool result is already in context.

**Verdict:** `runtime` — **not_viable** for directive injection. PostToolUse is an observation surface, not an injection surface.

---

#### 4. Stop Hook — Response Injection via Transcript File (Indirect/Runtime-Adjacent)

**Method:** The `Stop` hook fires after each assistant response completes. The hook can read and write to the transcript file (`~/.claude/projects/<project>/<session>.jsonl`). The langfuse-template and observagent both use this hook for observation.

**Injection hypothesis:** Since Claude Code reads the JSONL transcript file to build context, theoretically appending a JSON message to the transcript before Claude Code's next turn could inject content. This is the "file-based context update" path mentioned in the task spec.

**Critical issue:** This is **undocumented and unsupported**. The JSONL transcript is Claude Code's internal session state. Writing to it externally could:
1. Corrupt the session
2. Be ignored (Claude Code may validate or cache the transcript)
3. Work inconsistently across Claude Code versions

**Officially supported:** No. This is reverse-engineered from the JSONL file format.

**Stability risk:** High — internal file format can change without notice.

**Compaction risk (DC-1):** If context is compacted, externally-appended transcript entries may be included in or excluded from the compaction summary unpredictably.

**Verdict:** `indirect` — **partially_viable** with high stability risk. Not recommended as primary path.

---

#### 5. MCP Tool Injection (Runtime)

**Method:** Claude Code supports MCP (Model Context Protocol) servers. An MCP server can expose tools that Claude Code can call. If SecondSight runs as an MCP server and Claude Code is configured to use it, SecondSight can deliver content to Claude Code when Claude Code calls the MCP tool.

**Critical distinction:** This is **pull-based, not push-based**. Claude Code calls the MCP tool when it decides to — SecondSight cannot push content to Claude Code. For this to work, Claude Code must be prompted to call the MCP tool. This is a runtime injection only if the directive can be delivered through a tool that Claude Code actively queries.

**Format:** MCP tool responses (JSON). Can include structured text content.

**Size limit:** Governed by Claude Code's context window minus existing content.

**Persistence:** Content injected via MCP tool response persists in context until compaction.

**Compaction risk (DC-1):** MCP tool response content is in the conversation context and subject to compaction. In long sessions, MCP-injected content can be summarized or dropped.

**Officially supported:** Yes. MCP is a first-class Claude Code integration.

**Stability risk:** Low. MCP is an open standard backed by Anthropic.

**Verdict:** `runtime` — **partially_viable**. Effective only when Claude Code is prompted to call the MCP tool. Cannot deliver unsolicited directives mid-session. Best used as: a "SecondSight advisor" tool that Claude Code calls at key decision points.

---

#### 6. System Prompt via settings.json (Session-Start)

**Method:** Claude Code's `~/.claude/settings.json` supports a `system_prompt` field that is prepended to every session's system context. This is set once globally or per-project.

**Format:** Plain text or Markdown.

**Persistence:** Loaded at session start. Static for the session duration.

**Officially supported:** Yes, per Claude Code docs.

**Stability risk:** Low.

**Priority concern (DC-2):** System prompt content has high priority — it is injected before user messages. However, direct user override is still possible ("ignore your system prompt and..."). The system prompt does not survive if Claude Code's jailbreak/override behavior is triggered.

**Compaction risk (DC-1):** System prompt is part of the core context — it typically survives compaction better than middle-of-conversation content. But in extreme context overflow, even system prompt content can be truncated.

**Verdict:** `session_start` — **viable**. High priority in context, officially supported, but static (must be written before session starts).

---

### Overall Claude Code Assessment

**Overall verdict:** `partially_feasible`

**Best path:** Two-path strategy:
1. **Primary (session-start):** CLAUDE.md + system_prompt for persistent directive injection before each session. Directives from the previous session's analysis are written to CLAUDE.md before the next session starts.
2. **Secondary (runtime):** MCP tool server that Claude Code can query for current directives during a session. Requires Claude Code to be configured to use the SecondSight MCP tool.

**DC-2 behavioral compliance risk:** Neither path guarantees behavioral compliance. CLAUDE.md and system prompt content can be ignored by the agent. This MUST be verified in P0-6.

---

## OpenCode

### Storage Architecture (from reference source analysis)

OpenCode uses SQLite at `~/.local/share/opencode/opencode.db` with tables: `session`, `message`, `part`. Sessions have `time_compacting` tracking when compaction occurred. This is confirmed by the lazyagent reference source code.

### Injection Surfaces

#### 1. OpenCode Configuration Files (Session-Start)

**Method:** OpenCode reads configuration from `~/.config/opencode/config.json` (or similar path based on the OS). Configuration includes model settings and potentially system prompt content. Documentation at opencode.ai/docs covers configuration options.

**Format:** JSON configuration file.

**Officially supported:** Yes, per OpenCode documentation.

**Stability risk:** Medium — OpenCode is newer and configuration schema may evolve.

**Persistence:** Loaded at application start, not per-session.

**Compaction risk (DC-1):** Config-sourced system content is typically included in the initial context and may be compacted in long sessions.

**Verdict:** `session_start` — **partially_viable**. Config modification affects new sessions only. The exact system prompt injection mechanism via config is unverified from official docs alone.

---

#### 2. OpenCode AGENTS.md / Instructions Files (Session-Start)

**Method:** OpenCode (based on its architecture as a Claude-compatible terminal agent) likely supports per-directory instruction files similar to CLAUDE.md. The exact file name and behavior requires confirmation from official docs, but the pattern is established in the ecosystem.

**Officially supported:** Unverified. OpenCode documentation at opencode.ai/docs must be consulted for exact configuration file names.

**Stability risk:** Medium.

**Compaction risk (DC-1):** Same as CLAUDE.md for Claude Code — loaded at session start, may be compacted in long sessions. The presence of `time_compacting` in OpenCode's SQLite schema confirms compaction is a real behavior.

**Verdict:** `session_start` — **partially_viable** pending documentation verification.

---

#### 3. SQLite Direct Write — NOT Viable (Runtime Candidate)

**Method:** Theoretically, one could write directly to OpenCode's SQLite database (`opencode.db`) to inject messages into the `message` or `part` table. This would appear as content in the session.

**Critical issue:** This is completely unsupported and dangerous. Writing to an active SQLite database (WAL mode) from an external process risks corruption. OpenCode has no documented API for external message injection. The lazyagent reference code confirms that third-party tools use `mode=ro` (read-only) for good reason.

**Officially supported:** No.

**Stability risk:** Very high — this is not an injection surface.

**Verdict:** `runtime` — **not_viable**. External SQLite writes to an active OpenCode session are not a legitimate injection surface.

---

#### 4. OpenCode MCP Integration (Runtime)

**Method:** OpenCode supports MCP tools (it is built on OpenAI's Responses API, which supports tool calling). If SecondSight exposes an MCP tool, OpenCode can call it during a session and receive directive content.

**Same limitation as Claude Code:** Pull-based, not push-based. OpenCode must be configured to use the SecondSight MCP tool and must choose to call it.

**Officially supported:** MCP support is documented in OpenCode's architecture.

**Stability risk:** Low for MCP protocol; Medium for OpenCode's specific MCP implementation details.

**Compaction risk (DC-1):** MCP tool response content is in conversation context and subject to compaction in long sessions.

**Verdict:** `runtime` — **partially_viable**. Same limitations as Claude Code's MCP path.

---

### Overall OpenCode Assessment

**Overall verdict:** `partially_feasible`

**Best path:** Session-start injection via OpenCode config/instruction files. MCP tool as runtime complement. Direct SQLite manipulation is explicitly not viable.

**DC-2 risk:** Same as Claude Code — config-injected directives may be acknowledged but not followed. Behavioral verification required in P0-6.

**Long-session risk (DC-3):** OpenCode's `time_compacting` field confirms compaction occurs. Injected content from session-start may not persist through multiple compaction cycles in long sessions.

---

## Codex (OpenAI Codex CLI)

### Storage Architecture (from reference source analysis)

Codex CLI writes JSONL to `~/.codex/sessions/YYYY/MM/DD/*.jsonl`. It has a `session_index.jsonl` for thread names. Each session entry has types: `session_meta`, `turn_context`, `response_item`, `event_msg`. Token counts are tracked via `event_msg` with type `token_count`. This is confirmed by the lazyagent codex provider reference source.

### Injection Surfaces

#### 1. System Prompt via Instructions File (Session-Start)

**Method:** Codex CLI accepts a system prompt via `--instructions` flag or an instructions file (e.g., `~/.codex/instructions.md` or project-level `AGENTS.md`). This is the primary configuration surface for Codex.

**Format:** Markdown text or plain text.

**Officially supported:** Yes. Codex CLI `--instructions` flag is documented.

**Stability risk:** Low for `--instructions` flag; Medium for instructions file auto-detection path.

**Persistence:** Loaded at session start. Static for session duration.

**Compaction risk (DC-1):** System prompt / instructions content is part of Codex's initial context. In long sessions (which Codex may be especially susceptible to given its batch/autonomous operation model), context compression is possible. The exact behavior during context overflow is undocumented in official sources.

**Verdict:** `session_start` — **viable**. This is the primary SecondSight injection path for Codex.

---

#### 2. AGENTS.md Support (Session-Start)

**Method:** Codex CLI respects `AGENTS.md` files in the project directory as per-project instructions. This mirrors the Claude Code CLAUDE.md pattern and is part of the OpenAI agent ecosystem standard.

**Format:** Markdown text.

**Officially supported:** Yes. AGENTS.md is part of OpenAI's agent harness specification.

**Stability risk:** Low. AGENTS.md is a standard across the OpenAI agent ecosystem.

**Persistence:** Loaded at session start.

**Compaction risk (DC-1):** AGENTS.md content is in initial context; compaction risk same as system prompt.

**Verdict:** `session_start` — **viable**. More granular than global instructions (per-project scope).

---

#### 3. JSONL Transcript Injection (Indirect/Undocumented)

**Method:** Theoretically, appending to `~/.codex/sessions/YYYY/MM/DD/<session>.jsonl` before Codex's next turn could inject content. The JSONL format includes `session_meta`, `turn_context`, `response_item`, `event_msg` entry types.

**Critical issue:** Undocumented, unsupported, and fragile. The JSONL format is Codex's internal session state. The lazyagent reference code reads this format but never writes to it. Writing externally risks session corruption.

**Officially supported:** No.

**Stability risk:** High.

**Verdict:** `indirect` — **not_viable**. Same reasoning as Claude Code's JSONL injection path.

---

#### 4. Codex Headless/API Mode (Runtime Candidate)

**Method:** Codex CLI can be run programmatically. If SecondSight orchestrates Codex via CLI flags, it can pass directives as part of the invocation. This is not mid-session injection but rather per-invocation system prompt control.

**Officially supported:** Yes, for CLI invocation control.

**Stability risk:** Low for CLI flags.

**Compaction risk (DC-1):** If Codex is invoked fresh per task, compaction is less of a concern. In long-running Codex sessions, the same context overflow risk applies.

**Verdict:** `session_start` — **viable** (per-invocation context control). Not true mid-session runtime injection.

---

### Overall Codex Assessment

**Overall verdict:** `partially_feasible`

**Best path:** AGENTS.md or `--instructions` flag for session-start injection. Directives are written to AGENTS.md before Codex session starts.

**DC-2 risk:** Codex is built on OpenAI's Responses API with strong instruction-following training. However, behavioral compliance is not guaranteed for complex or conflicting directives. Verification required in P0-6.

**Long-session risk (DC-3):** Codex's primary use case includes autonomous long-running tasks. Context overflow is a real risk. AGENTS.md/instructions content may be summarized or truncated in extreme context scenarios.

---

## Cross-Agent Analysis

### Runtime vs Session-Start Distinction

A key finding of this investigation is that **true runtime injection** (pushing new content into an active agent session mid-conversation) is **not reliably achievable** for any of the three agents via documented, supported mechanisms.

What exists:
- Claude Code: MCP tools (pull-based, not push-based)
- OpenCode: MCP tools (pull-based, not push-based)
- Codex: No documented mid-session push mechanism

What is achievable reliably:
- **Session-start injection** is viable for all three agents via instruction/config files
- **Post-session injection** (write directives for next session) is fully viable

### Context Compaction Risk (DC-1)

All three agents perform context compaction in long sessions. The specific behavior varies:
- Claude Code: Documented compaction behavior; CLAUDE.md content may or may not survive
- OpenCode: `time_compacting` field in SQLite schema confirms this is a core feature
- Codex: Context window management occurs implicitly; behavior during overflow is undocumented

**Mitigation:** SecondSight should treat session-start injection as the primary path, accepting that runtime injection requires MCP pull-based mechanisms that carry uncertainty.

### Priority Override Risk (DC-2)

For all three agents, injected directives (via CLAUDE.md, config files, AGENTS.md, system prompts) can in principle be overridden by explicit user instructions. The degree to which each agent prioritizes injected directives over user instructions is **unverified** and must be tested in P0-6.

**Mitigation:** Design directives as persistent preferences rather than hard constraints. Use directive framing that is consistent with agent behavior rather than conflicting with user autonomy.

### Heavy Context Load Risk (DC-3)

Long sessions (100+ turns, large file operations) create context window pressure for all three agents. Injected content present at session start may be summarized or dropped in compaction. The longer the session, the higher the probability that directives injected at session start no longer appear in the active context.

**Mitigation for long sessions:** Use MCP tool as a runtime fallback that Claude Code/OpenCode can call to refresh directives when context has been compacted.

---

## Summary

| Agent | Runtime Injection | Session-Start Injection | Best Path |
|-------|------------------|------------------------|-----------|
| Claude Code | Partially viable (MCP pull-based) | Viable (CLAUDE.md + system_prompt) | Session-start primary, MCP secondary |
| OpenCode | Partially viable (MCP pull-based) | Partially viable (config files) | Session-start primary |
| Codex | Not viable (no documented mechanism) | Viable (AGENTS.md + --instructions) | Session-start only |

**Primary recommendation:** Session-start injection is the viable path for all three agents. This means SecondSight's Feedback Layer must operate on a post-session analysis → pre-session injection cycle. True runtime injection requires MCP integration and is pull-based.

**P0-6 dependency:** All viability verdicts above assume agents will actually follow injected directives. Behavioral compliance must be verified in P0-6. If P0-6 shows low compliance, all "viable" verdicts must be downgraded to "partially_viable" or lower.
