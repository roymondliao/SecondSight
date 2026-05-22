# Task 1: Extend directive schema/storage for lifecycle state and revision ledger

## Context

Read: `overview.md`, `2-plan.md` sections "Data model changes" and
"Component #4 — Operator visibility".

This task establishes persistence primitives only. Do not implement identity
matching, lifecycle policy, or revision LLM rewriting in this task.

## Files

- Create: `src/secondsight/storage/directive_revisions_table.py`
- Create: `src/secondsight/storage/directive_revisions_repository.py`
- Modify: `src/secondsight/analysis/schemas.py`
- Modify: `src/secondsight/storage/directives_table.py`
- Modify: `src/secondsight/storage/directives_repository.py`
- Modify: `src/secondsight/storage/__init__.py`
- Test: `tests/storage/test_directives_repository.py`
- Create: `tests/storage/test_directive_revisions_repository.py`
- Modify: `tests/analysis/test_schemas.py`
- Modify: `tests/storage/test_table_registration.py`

## Death Test Requirements

- Test: `DirectiveStatus.STALLED` is accepted by schema/storage validation but
  not accidentally treated as user-PATCHable
- Test: accepted rewrite history appends a ledger row without mutating prior
  rows
- Test: new lifecycle fields are persisted and round-trip through repository
  reads without silently defaulting to nonsense values

## Implementation Steps

- [ ] Step 1: Write death tests
- [ ] Step 2: Run death tests — verify they fail
- [ ] Step 3: Write unit tests
- [ ] Step 4: Run unit tests — verify they fail
- [ ] Step 5: Implement minimal code to pass all tests
- [ ] Step 6: Run all tests — verify they pass
- [ ] Step 7: Write scar report
- [ ] Step 8: Commit

## Expected Scar Report Items

- Potential shortcut: storing revision history as JSON on the directive row
  instead of append-only rows
- Potential shortcut: adding weight alone without the state fields needed to
  explain policy decisions later
- Assumption to verify: the shared project DB is the right home for revision
  ledger persistence

## Acceptance Criteria

- Covers: "rewrite preserves directive row but silently breaks lineage identity"
- Covers: "directive list API expands shape with lifecycle fields"
