# Task 4: Runtime Injection Feasibility Test

## Context

Read: overview.md

SecondSight's Feedback Layer depends on being able to inject directives into agent sessions. This task tests whether runtime injection (during an active session) and session-start injection (before session begins) are feasible across all three agents.

**Primary sources:**
- Claude Code: https://code.claude.com/docs/en/overview
- OpenCode: https://opencode.ai/docs
- Codex: https://developers.openai.com/codex

**Injection paths to investigate per agent:**
1. **Runtime injection:** Modifying agent context during an active session (e.g., via hook that appends to system prompt, MCP tool injection, file-based context update)
2. **Session-start injection:** Writing directives into files that the agent reads at session start (e.g., CLAUDE.md, .opencode config, system prompt files)
3. **Indirect injection:** Using hooks to modify the agent's environment (e.g., creating/modifying files the agent will read during task execution)

## Files

- Create: `changes/2026-04-24_phase0-feasibility/investigations/injection-feasibility.md`
- Create: `changes/2026-04-24_phase0-feasibility/investigations/injection-feasibility.yaml`

## Death Test Requirements

- Test: Injection path exists technically but agent drops/ignores injected content during compaction or context overflow
- Test: Session-start injection works but content is treated as low-priority and overridden by user instructions
- Test: Injection succeeds in test conditions but fails when agent is under heavy context load (long sessions)

## Investigation Steps

- [ ] Step 1: For each agent, identify all possible injection surfaces from official documentation
- [ ] Step 2: Categorize each surface as runtime / session-start / indirect
- [ ] Step 3: For each surface, document: method, content format, size limits, persistence, latency
- [ ] Step 4: Identify which surfaces are officially supported vs undocumented/experimental
- [ ] Step 5: Assess risk of each surface being changed or removed by platform
- [ ] Step 6: For each viable path, document how SecondSight would use it (what gets injected, when, how)
- [ ] Step 7: Rate each path: viable / partially_viable / not_viable with evidence
- [ ] Step 8: Write overall injection feasibility verdict per agent

## Expected Scar Report Items

- Potential shortcut: Counting CLAUDE.md/config file modification as "injection" without testing if agent actually reads updated content mid-session
- Potential shortcut: Assuming all agents have equivalent injection surfaces
- Assumption to verify: Runtime injection is distinct from session-start injection in practice (some agents may re-read config on every turn)
- Assumption to verify: Injected content survives context compaction in long sessions

## Acceptance Criteria

- Covers: "Silent failure - injection verified but agent ignores directive content"
- Covers: "Degradation - runtime injection infeasible, session-start only"
- Covers: "Success - at least one injection path verified per agent"

## Output Format: findings.yaml

```yaml
investigation_date: 2026-04-24
source: official_docs + reference_opensoure

agents:
  - name: claude_code
    injection_paths:
      - type: runtime | session_start | indirect
        method: "<description>"
        format: "<content format>"
        size_limit: "<if known>"
        persistence: "<when/how long>"
        latency: "<injection to effect>"
        officially_supported: true | false
        stability_risk: low | medium | high
        verdict: viable | partially_viable | not_viable
        evidence: "<how tested or documented>"
    overall_verdict: feasible | partially_feasible | infeasible
    best_path: "<recommended injection method>"

  - name: opencode
    # same structure

  - name: codex
    # same structure

summary:
  agents_with_viable_injection: <N>
  runtime_injection_viable: true | false
  session_start_injection_viable: true | false
  recommendation: "<primary injection strategy>"
```
