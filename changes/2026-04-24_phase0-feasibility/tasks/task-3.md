# Task 3: Hook Mechanism Investigation — Codex

## Context

Read: overview.md

SecondSight needs to capture execution events from Codex CLI. This task investigates what events Codex exposes, through what mechanism, in what format, and with what limitations.

**Primary source:** https://developers.openai.com/codex
**Reference starting point:** `reference_opensoure/lazyagent/` — supports Codex session watching. Cross-validate against official docs.

**What SecondSight needs from events (for Phase 2 Analysis Layer):**
- Tool call type (read, write, execute, search, etc.)
- Tool call arguments and results
- Timestamps (start, end)
- Token usage per call (if available)
- Session lifecycle events (start, end, error)
- User prompt content
- Agent response content
- Sub-agent spawning events (if applicable)

## Files

- Create: `changes/2026-04-24_phase0-feasibility/investigations/codex-hooks.md`
- Create: `changes/2026-04-24_phase0-feasibility/investigations/codex-hooks.yaml`

## Death Test Requirements

- Test: Investigation reports "feasible" based on hook count alone, but event payloads lack tool call arguments/results
- Test: Codex CLI event format differs significantly from Codex API format, causing confusion about which is the target
- Test: Investigation assumes Codex exposes session-level hooks similar to Claude Code, but Codex may use a fundamentally different model (e.g., API-only, no local transcript)

## Investigation Steps

- [ ] Step 1: Read official Codex documentation on CLI events, hooks, and session management
- [ ] Step 2: Distinguish between Codex CLI (local tool) and Codex API (cloud service) — SecondSight targets the CLI
- [ ] Step 3: Catalog all available event types and their trigger conditions
- [ ] Step 4: For each event type, document the event payload schema
- [ ] Step 5: Cross-validate with lazyagent's Codex integration approach
- [ ] Step 6: Map each event type to SecondSight's Analysis Layer needs
- [ ] Step 7: Calculate hook coverage rate
- [ ] Step 8: Document known limitations and stability risks
- [ ] Step 9: Write feasibility verdict with evidence

## Expected Scar Report Items

- Potential shortcut: Conflating Codex CLI and Codex API capabilities
- Potential shortcut: Assuming Codex follows similar patterns to Claude Code hooks
- Assumption to verify: Codex CLI stores local transcripts or exposes hooks (may be API-only)
- Assumption to verify: OpenAI's developer documentation covers Codex CLI event format

## Acceptance Criteria

- Covers: "Silent failure - hook investigation reports feasible but event data too shallow for analysis"
- Covers: "Success - all three agents expose tool-call-level events" (Codex portion)
- Covers: "Degradation - one agent has no hook mechanism at all" (if applicable)

## Output Format: findings.yaml

Same structure as Task 1's findings.yaml with `agent: codex`.
