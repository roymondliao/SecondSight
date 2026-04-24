# Task 9: Fallback Design

## Context

Read: overview.md

This task designs the fallback strategy for when primary injection paths are infeasible. It answers: "If SecondSight can't inject directives at runtime, what does the product become? If session-start injection is the only option, what features are lost and what remains viable?"

**Depends on:** Task 4 (injection feasibility) and Task 6 (directive comprehension). These provide the evidence for what works and what doesn't.

**Fallback hierarchy (from plan.md P0-8):**
1. Runtime injection → full Feedback Layer capability
2. Session-start injection only → post-run directives for next session, no runtime feedback
3. No injection → observation + analysis only, feedback via external channel (human review)

## Files

- Create: `changes/2026-04-24_phase0-feasibility/investigations/fallback-design.md`

## Death Test Requirements

- Test: Fallback design claims "session-start-only mode" is viable but doesn't quantify what's lost (which Phase 3 features are dropped?)
- Test: Fallback design assumes "observation + analysis only" is still a viable product without re-evaluating the market positioning
- Test: Fallback design proposes workarounds that haven't been validated against agent behavior

## Design Steps

- [ ] Step 1: Catalog all injection paths and their verdicts from Task 4
- [ ] Step 2: For each fallback level, list: what SecondSight CAN do, what it CANNOT do, which PRD features are affected
- [ ] Step 3: For session-start-only mode: define the directive lifecycle (generate after session N → inject before session N+1)
- [ ] Step 4: For no-injection mode: define alternative delivery mechanisms (dashboard, CLI report, file output for human review)
- [ ] Step 5: For each fallback level, assess product viability: is SecondSight still differentiated from observation-only tools?
- [ ] Step 6: Define the decision criteria: when does the team choose each fallback level?
- [ ] Step 7: Document phase-by-phase impact: how does each fallback level affect Phase 1, 2, 3A, 3B scope?
- [ ] Step 8: Write recommendations and present to stakeholder

## Expected Scar Report Items

- Potential shortcut: Designing fallback as "we'll figure it out later" instead of concrete feature-by-feature impact analysis
- Potential shortcut: Assuming session-start-only is "almost as good" without measuring the gap
- Assumption to verify: Post-run directive generation is valuable enough on its own to justify the full Analysis Layer investment
- Assumption to verify: Human-review-based feedback delivery is viable for the target user (agents and framework maintainers, not end users)

## Acceptance Criteria

- Covers: "Degradation - runtime injection infeasible, session-start only"
- Covers: "Degradation - directive comprehension below 50% but above 30%"
