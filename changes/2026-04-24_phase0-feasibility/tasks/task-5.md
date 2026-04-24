# Task 5: Session Identity Linking Design

## Context

Read: overview.md

SecondSight needs to track directive lineage across sessions: "directive D was generated from analysis of session S1, applied in session S2, and its outcome observed in session S3." This requires a session identity model that can link sessions to agents, projects, frameworks, and directive lineage.

This task is a design task, not a code task. The output is a design document with an identity model, linking strategy, and example data.

**Constraints from PRD:**
- Sessions may come from different agents (Claude Code, OpenCode, Codex)
- Sessions may target the same or different projects
- Sessions may or may not have explicit identifiers (depends on agent platform)
- Identity model must support Phase 1's Session Schema (P1-2) and Phase 3's directive outcome tracking

## Files

- Create: `changes/2026-04-24_phase0-feasibility/investigations/session-identity-design.md`

## Death Test Requirements

- Test: Identity model works for single-agent consecutive sessions but breaks when different agents work on the same project
- Test: Identity model assumes agent platforms provide stable session IDs, but some agents generate ephemeral IDs that don't persist
- Test: Identity linking requires information that's only available after session ends, making real-time linking impossible

## Design Steps

- [ ] Step 1: Catalog what identity attributes each agent provides (from Task 1-3 hook investigation findings or official docs)
- [ ] Step 2: Define identity dimensions: agent, project, user, session, directive lineage
- [ ] Step 3: Design linking strategy: how sessions are linked across dimensions
- [ ] Step 4: Assess cross-agent linking feasibility (same project, different agents)
- [ ] Step 5: Define degradation levels: full linking → single-agent linking → session-isolated
- [ ] Step 6: Create example data showing 5+ sessions with linking applied
- [ ] Step 7: Document what Phase 1 needs to implement from this design
- [ ] Step 8: Document limitations and unsupported scenarios explicitly

## Expected Scar Report Items

- Potential shortcut: Designing only for the easy case (same agent, consecutive sessions) and hand-waving cross-agent linking
- Potential shortcut: Assuming project identity is always available (some agents may not expose working directory)
- Assumption to verify: Agent session IDs are persistent and retrievable after session ends
- Assumption to verify: Directive lineage can be traced without requiring agent cooperation

## Acceptance Criteria

- Covers: "Silent failure - session identity works single-agent but breaks cross-agent"
- Covers: "Success - session identity model supports cross-session linking"
