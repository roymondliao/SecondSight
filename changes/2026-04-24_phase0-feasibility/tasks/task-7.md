# Task 7: Unified Event Schema POC

## Context

Read: overview.md

This task produces a working POC of the Unified Event Schema v0.1 — a schema that can represent execution events from all three agents (Claude Code, OpenCode, Codex) in a single format.

**Depends on:** Tasks 1-3 (hook investigation findings). The schema must be grounded in actual event data discovered during investigation, not hypothetical structures.

**Schema requirements (from PRD §6.1 and plan.md P0-12):**
- Must represent: tool calls, session lifecycle events, user prompts, agent responses
- Must include: timestamps, event types, payload data, agent-specific metadata
- Must support: Phase 2 action classification (Aligned/Wasteful/Divergent/Exploratory/Premature/Over-verified)
- Must minimize untyped fields: goal is >= 50% typed fields per agent

**POC scope:**
- Python dataclasses or Pydantic models
- JSON Schema export for validation
- Tests validating schema against sample events from each agent
- Design notes documenting unification decisions

## Files

- Create: `src/secondsight/__init__.py`
- Create: `src/secondsight/poc/__init__.py`
- Create: `src/secondsight/poc/event_schema.py` — Schema definitions
- Create: `src/secondsight/poc/event_schema.json` — JSON Schema export
- Create: `tests/__init__.py`
- Create: `tests/poc/__init__.py`
- Create: `tests/poc/test_event_schema.py` — Validation tests
- Create: `tests/poc/conftest.py` — Sample event fixtures
- Create: `changes/2026-04-24_phase0-feasibility/event-schema-design.md` — Design notes

## Death Test Requirements

- Test: Schema validates all sample events but > 50% of data per agent lands in untyped `metadata: dict` fields
- Test: Schema defines fields for Agent A that are always empty for Agent B and C (false unification)
- Test: Schema cannot represent a real event from reference_opensoure sample data
- Test: Schema version has no migration path — if fields change, old events become unreadable

## Implementation Steps

- [ ] Step 1: Write death tests — schema with > 50% untyped fields must fail validation, schema that can't parse real events must fail
- [ ] Step 2: Run death tests — verify they fail (no schema exists yet)
- [ ] Step 3: Write unit tests — schema validates sample events from all three agents, typed_field_percentage >= 50% per agent
- [ ] Step 4: Run unit tests — verify they fail
- [ ] Step 5: Implement event schema based on investigation findings from Tasks 1-3
- [ ] Step 6: Generate JSON Schema export
- [ ] Step 7: Run all tests — verify they pass
- [ ] Step 8: Write event-schema-design.md documenting decisions, tradeoffs, and untyped-field analysis
- [ ] Step 9: Write scar report
- [ ] Step 10: Commit

## Expected Scar Report Items

- Potential shortcut: Making all agent-specific fields optional to avoid validation failures
- Potential shortcut: Using `Any` type for fields that differ across agents instead of proper union types
- Assumption to verify: Event payloads from reference_opensoure are representative of current agent versions
- Assumption to verify: Schema versioning is needed this early (v0.1 may change significantly)

## Acceptance Criteria

- Covers: "Silent failure - event schema hides incompatibility behind untyped fields"
- Covers: "Success - unified event schema covers all three agents with >= 50% typed fields"
