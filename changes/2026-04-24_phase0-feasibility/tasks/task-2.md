# Task 2: Hook Mechanism Investigation — OpenCode

## Context

Read: overview.md

SecondSight needs to capture execution events from OpenCode. This task investigates what events OpenCode exposes, through what mechanism, in what format, and with what limitations.

**Primary source:** https://opencode.ai/docs
**Reference starting point:** `reference_opensoure/lazyagent/` — supports OpenCode session watching. Cross-validate against official docs.

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

- Create: `changes/2026-04-24_phase0-feasibility/investigations/opencode-hooks.md`
- Create: `changes/2026-04-24_phase0-feasibility/investigations/opencode-hooks.yaml`

## Death Test Requirements

- Test: Investigation reports "feasible" based on hook count alone, but event payloads lack tool call arguments/results needed for action classification
- Test: Investigation assumes OpenCode's event format is stable without checking version/changelog
- Test: Investigation misses that OpenCode being open source means the hook API can be extended — but that extension effort is a hidden cost

## Investigation Steps

- [ ] Step 1: Read official OpenCode documentation on hooks, events, and session management
- [ ] Step 2: If documentation is sparse, read OpenCode source code to understand event emission
- [ ] Step 3: Catalog all available event types and their trigger conditions
- [ ] Step 4: For each event type, document the event payload schema (fields, types, optional/required)
- [ ] Step 5: Cross-validate with lazyagent's OpenCode integration approach
- [ ] Step 6: Map each event type to SecondSight's Analysis Layer needs
- [ ] Step 7: Calculate hook coverage rate
- [ ] Step 8: Document known limitations, extensibility options (since open source), and stability risks
- [ ] Step 9: Write feasibility verdict with evidence

## Expected Scar Report Items

- Potential shortcut: Assuming open source = full access, ignoring that OpenCode may not expose all internal events
- Potential shortcut: Relying on lazyagent's approach without verifying it against current OpenCode version
- Assumption to verify: OpenCode's session storage format is documented or at least stable across versions
- Assumption to verify: OpenCode supports external hooks (not just internal event bus)

## Acceptance Criteria

- Covers: "Silent failure - hook investigation reports feasible but event data too shallow for analysis"
- Covers: "Success - all three agents expose tool-call-level events" (OpenCode portion)
- Covers: "Degradation - one agent has no hook mechanism at all" (if applicable)

## Output Format: findings.yaml

Same structure as Task 1's findings.yaml with `agent: opencode`.
