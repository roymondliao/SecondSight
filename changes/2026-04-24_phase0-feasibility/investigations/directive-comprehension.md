# Directive Comprehension Experiment

**Date:** 2026-04-24
**Task:** P0-6 — Directive Comprehension Experiment
**Phase:** Phase 0 — Protocol Design (live testing deferred to Phase 1)
**Depends on:** Task 4 (injection feasibility findings)

---

## Purpose

Even if SecondSight can inject directives into agent sessions, those directives are worthless if agents cannot understand and follow them. This investigation tests whether agents can comprehend and comply with SecondSight-style directives — specifically whether they can:

1. Recognize a directive as an instruction to follow (not data to process)
2. Identify the trigger condition during execution (ongoing self-monitoring)
3. Change behavior when the trigger fires
4. Maintain the behavioral change throughout the session

**Critical framing:** This experiment must distinguish between verbal acknowledgment ("I'll follow this directive") and behavioral compliance (actually doing something different). Verbal acknowledgment without behavioral change is the most common and most dangerous silent failure mode for directive injection.

---

## What Was Done

**Phase 0 outcome:** Live agent testing was not feasible in Phase 0. This document contains:

- A complete experiment protocol designed for Phase 1 execution
- Three test directives with clear, measurable trigger conditions
- Three phrasing variants per directive (formal JSON, natural language, concise imperative)
- Measurement criteria that require behavioral observation, not acknowledgment
- Failure mode analysis based on agent architecture knowledge from Tasks 1-4
- Pre-populated result matrix with `not_tested` entries covering all 27 combinations

Phase 1 can execute the experiment without additional design work.

---

## Injection Paths Used (from Task 4)

| Agent | Injection Path | Confidence |
|-------|---------------|-----------|
| Claude Code | CLAUDE.md file (session-start) | High — officially supported |
| OpenCode | Config system prompt field (session-start) | Low — INFERRED, unverified |
| Codex | AGENTS.md in project directory (session-start) | High — officially supported |

---

## Test Directives Designed

### d_test_1: Repeat File Read Reduction

- **Scope:** file_reading
- **Trigger:** `repeat_read_same_file > 2`
- **Expected change:** Fewer repeat reads of same file path in session
- **Complexity:** Medium — requires counting reads of individual files

This maps directly to the PRD §10.2 canonical directive example. The agent must track which files have been read and how many times, then consult context before re-reading.

### d_test_2: Redundant Verification Reduction

- **Scope:** verification
- **Trigger:** `verification_after_verified_fact`
- **Expected change:** Fewer redundant verification commands (re-running already-passed tests, re-reading already-confirmed values)
- **Complexity:** Medium-high — requires tracking session state about confirmed facts

Tests the "Over-verified" waste pattern from PRD §6.2. More complex than d_test_1 because the trigger requires remembering what has already been confirmed, not just counting.

### d_test_3: Task Scope Enforcement

- **Scope:** task_scoping
- **Trigger:** `out_of_scope_action_attempt`
- **Expected change:** Agent asks before making unrequested adjacent changes, or makes none
- **Complexity:** High — requires recognizing intent before acting on it

Tests the "Divergent" action classification from PRD §6.2. The hardest directive to follow because the trigger fires on intent detection before the action, requiring the agent to interrupt itself.

---

## Phrasing Variants

Each directive has three phrasing styles:

| Style | Description | Hypothesis |
|-------|-------------|-----------|
| `formal_json` | Structured JSON matching PRD §10.2 directive contract format | Explicit schema may help or hinder; may be parsed as data |
| `natural_language` | Conversational explanation with rationale | Likely strongest for instruction-following |
| `concise_imperative` | Direct one-sentence rule, no rationale | May lose intent; risk of over-literal interpretation |

---

## Measurement Criteria

Compliance is always determined by **behavioral observation**, never by what the agent says about its behavior.

| Category | Definition |
|----------|-----------|
| `complied` | Target metric changed measurably in expected direction AND task completed successfully |
| `ignored` | Agent may have acknowledged directive; target metric unchanged vs baseline |
| `misinterpreted` | Behavior changed in wrong direction or too broadly (overcorrection) |
| `not_tested` | Phase 0 placeholder — experiment not yet executed |

---

## Death Cases Addressed

### DC-A: Acknowledgment Without Behavioral Change

**What it is:** Agent says "I'll follow this directive" but behavior is identical to baseline.

**How it's detected:** Behavioral metrics are compared between baseline (no directive) and test (directive injected) sessions. Agent's verbal statements are recorded but do not count toward compliance determination.

**Why it matters:** If this death case is not detected, SecondSight will report high directive "adoption" while producing zero behavioral improvement. The entire feedback loop produces false signal.

### DC-B: Directive Drop After Context Compaction

**What it is:** Agent follows directive in early turns, then silently reverts after context compaction in a long session.

**How it's detected:** Phase 1 protocol requires testing both short sessions (10 turns) and long sessions (50+ turns) for d_test_1 and d_test_2. If compliance rate drops in long sessions, compaction is the suspected cause.

**Why it matters:** SecondSight's most valuable use cases are long, complex tasks. If directives only survive 10 turns, the product has a fundamental session-length limitation.

### DC-C: Misinterpretation / Overcorrection

**What it is:** Agent follows directive but too broadly. "Avoid redundant reads" becomes "avoid reads entirely." "Ask before scope-expanding changes" becomes "ask before every action."

**How it's detected:** `misinterpreted` is a separate compliance category. Task completion quality is checked: if the agent achieved the behavioral target metric but the task completed with degraded quality, this is classified as misinterpreted.

**Why it matters:** A misinterpreted directive can be worse than no directive. An agent that stops reading files produces broken outputs.

---

## Failure Mode Analysis

### Failure Mode 1: Trigger Identification Failure
Agent remembers the directive but fails to recognize when the trigger condition fires. For threshold-based directives (d_test_1 with `> 2`), the agent must maintain a count across turns. If it loses track, the directive is unreachable even though the agent intends to follow it.

### Failure Mode 2: Multiple Directive Interference
When multiple directives are active simultaneously, they may conflict. d_test_2 (reduce redundant verification) may conflict with d_test_1 (check context before re-reading — which itself is a form of verification). An agent trying to follow both may resolve the conflict by dropping one.

### Failure Mode 3: The "Easy Compliance" Illusion
Testing only simple directives (e.g., "always add a comment to each function you write") would show high compliance but would not predict compliance with complex behavioral directives. This experiment avoids this by using trigger-condition directives.

---

## Current Assessment (Pre-Live-Testing)

**Verdict:** `partially_feasible` — low confidence

**Reasoning:**
- Session-start injection paths confirmed viable (Task 4)
- The content DOES reach the agent
- Whether agents follow trigger-condition behavioral directives consistently is unknown
- Compaction risk is the highest uncertainty for production use cases
- Natural language phrasing is the recommended default (hypothesis, not yet verified)

**Per-agent pre-assessment:**

| Agent | Assessment | Primary Risk |
|-------|-----------|-------------|
| Claude Code | Most likely to show reasonable compliance | DC-A (acknowledgment without behavior) |
| OpenCode | Lowest confidence — injection path unverified | Injection may fail before compliance can be measured |
| Codex | Unclear — task-execution agent may have different compliance profile | Trigger-condition tracking may be weaker |

---

## Experiment Matrix for Phase 1

27 primary combinations (3 agents × 3 directives × 3 phrasings), plus:
- 3 combined-directive sessions (all directives active simultaneously, 1 per agent)
- 6 long-session stability tests (d_test_1 and d_test_2, 2 agents × 3 phrasings compressed to 1 per directive)

**Estimated effort:** 2-3 days of structured testing in Phase 1.

---

## Known Limitations of This Investigation

1. **No live data.** All results are protocol-based. Actual compliance rates are unknown.
2. **OpenCode injection unverified.** If config injection fails, the OpenCode portion of the matrix cannot proceed until an alternative path is found.
3. **Baseline variation.** Agent behavior on the same task may vary between runs due to non-determinism. Protocol should use temperature=0 or average over multiple baseline runs where possible.
4. **Controlled tasks required.** The experiment protocol specifies task designs, but implementing those tasks requires choosing specific codebases and scenarios. Phase 1 must select representative tasks.

---

## Machine-Readable Companion

See `directive-comprehension.yaml` for the full experiment protocol, result matrix, measurement criteria, and failure mode analysis in machine-readable format.
