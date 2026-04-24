# Task 1: Hook Mechanism Investigation — Claude Code

## Context

Read: overview.md

SecondSight needs to capture execution events from Claude Code. This task investigates what events Claude Code exposes, through what mechanism, in what format, and with what limitations.

**Primary source:** https://code.claude.com/docs/en/overview
**Reference starting point:** `reference_opensoure/claude-code-langfuse-template/` and `reference_opensoure/observagent/` — both use Claude Code hooks for event capture. Cross-validate their approach against official docs.

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

- Create: `changes/2026-04-24_phase0-feasibility/investigations/claude-code-hooks.md`
- Create: `changes/2026-04-24_phase0-feasibility/investigations/claude-code-hooks.yaml`

## Death Test Requirements

- Test: Investigation reports "feasible" based on hook count alone, but event payloads lack tool call arguments/results needed for action classification
- Test: Investigation assumes JSONL transcript format is stable, but Claude Code docs indicate it's an internal format subject to change
- Test: Investigation misses a hook type that exists in official docs but not in reference projects

## Investigation Steps

- [ ] Step 1: Read official Claude Code documentation on hooks, events, and session management
- [ ] Step 2: Catalog all available hook types and their trigger conditions
- [ ] Step 3: For each hook type, document the event payload schema (fields, types, optional/required)
- [ ] Step 4: Cross-validate with reference_opensoure implementations (claude-code-langfuse-template, observagent)
- [ ] Step 5: Map each event type to SecondSight's Analysis Layer needs (see "What SecondSight needs" above)
- [ ] Step 6: Calculate hook coverage rate: (event types with sufficient fields) / (total needed event types)
- [ ] Step 7: Document known limitations, undocumented behaviors, and stability risks
- [ ] Step 8: Write feasibility verdict: feasible / partially_feasible / infeasible with evidence

## Expected Scar Report Items

- Potential shortcut: Treating JSONL transcript file watching as equivalent to a supported hook API
- Potential shortcut: Counting event types without verifying payload field completeness
- Assumption to verify: Claude Code hook mechanism is officially documented and stable (not experimental)
- Assumption to verify: Sub-agent events are visible through the same hook mechanism

## Acceptance Criteria

- Covers: "Silent failure - hook investigation reports feasible but event data too shallow for analysis"
- Covers: "Success - all three agents expose tool-call-level events" (Claude Code portion)

## Output Format: findings.yaml

```yaml
agent: claude_code
investigation_date: 2026-04-24
source: official_docs + reference_opensoure
verdict: feasible | partially_feasible | infeasible | inconclusive

hook_types:
  - name: "<hook type>"
    trigger: "<when it fires>"
    payload_fields:
      - name: "<field>"
        type: "<type>"
        required: true | false
        analysis_use: "<what Phase 2 uses this for>"
    limitations: "<known issues>"

coverage:
  needed_event_types: <N>
  available_event_types: <N>
  sufficient_field_event_types: <N>
  coverage_rate: <percentage>

risks:
  - "<risk description>"

missing:
  - "<what SecondSight needs but can't get>"
```
