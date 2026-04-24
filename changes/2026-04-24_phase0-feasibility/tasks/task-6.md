# Task 6: Directive Comprehension Experiment

## Context

Read: overview.md

Even if SecondSight can inject directives into agent sessions, the directives are worthless if agents can't understand and follow them. This task tests whether agents can comprehend and comply with SecondSight-style directives.

**Depends on:** Task 4 (injection feasibility) — need to know which injection path to use for testing. If Task 4 is not yet complete, use the most likely injection path per agent (session-start via config files).

**What a SecondSight directive looks like (from PRD §10.2):**
```json
{
  "scope": "debugging",
  "trigger": "repeat_read_same_file > 2",
  "instruction": "Before rereading the same file, verify whether the needed evidence is already in context.",
  "priority": "high",
  "expected_effect": "reduce redundant file reads"
}
```

The experiment must test whether agents can:
1. Recognize a directive as an instruction to follow
2. Identify the trigger condition during execution
3. Change behavior when the trigger fires
4. Maintain the behavioral change throughout the session

## Files

- Create: `changes/2026-04-24_phase0-feasibility/investigations/directive-comprehension.md`
- Create: `changes/2026-04-24_phase0-feasibility/investigations/directive-comprehension.yaml`

## Death Test Requirements

- Test: Agent acknowledges directive verbally ("I'll follow this directive") but doesn't change behavior
- Test: Agent follows directive initially but drops it after context compaction in a long session
- Test: Agent interprets directive differently than intended (e.g., "avoid redundant reads" → stops reading files entirely)

## Experiment Steps

- [ ] Step 1: Design 3 test directives with clear, measurable trigger conditions and expected behavioral changes
- [ ] Step 2: Design 3 phrasing variants per directive (formal JSON, natural language, concise imperative)
- [ ] Step 3: For each agent + directive + phrasing combination, design a test scenario that would trigger the directive
- [ ] Step 4: Define measurement criteria: what counts as "complied" vs "ignored" vs "misinterpreted"
- [ ] Step 5: Run experiments (or document experiment protocol if live testing not feasible in this phase)
- [ ] Step 6: Calculate compliance rate per agent, per directive type, per phrasing style
- [ ] Step 7: Analyze failure modes: why did non-compliance happen?
- [ ] Step 8: Write feasibility verdict and phrasing recommendations for Phase 3A

## Expected Scar Report Items

- Potential shortcut: Testing only simple directives (like "add a comment") and extrapolating to complex behavioral directives
- Potential shortcut: Measuring compliance by agent self-report ("I followed the directive") instead of behavioral observation
- Assumption to verify: Directive compliance is stable across session length (not just first few turns)
- Assumption to verify: Multiple simultaneous directives don't interfere with each other

## Acceptance Criteria

- Covers: "Silent failure - injection verified but agent ignores directive content"
- Covers: "Degradation - directive comprehension below 50% but above 30%"
- Covers: "Success - at least one injection path verified per agent" (comprehension portion)

## Output Format: findings.yaml

```yaml
investigation_date: 2026-04-24
experiment_type: directive_comprehension

test_directives:
  - id: d_test_1
    scope: "<scope>"
    trigger: "<trigger>"
    instruction: "<instruction>"
    expected_behavior_change: "<what should change>"

results:
  - agent: claude_code
    directive: d_test_1
    phrasing: formal_json | natural_language | concise_imperative
    compliance: complied | ignored | misinterpreted
    evidence: "<what was observed>"

summary:
  overall_compliance_rate: <percentage>
  per_agent:
    claude_code: <percentage>
    opencode: <percentage>
    codex: <percentage>
  per_phrasing:
    formal_json: <percentage>
    natural_language: <percentage>
    concise_imperative: <percentage>
  recommended_phrasing: "<best performing>"
  verdict: feasible | partially_feasible | infeasible
```
