# Task 4: Add revision pipeline, stalled handling, and revision history persistence

## Context

Read: `overview.md`, `2-plan.md` sections "Revision ledger" and
"Component #2 — Weight policy layer".

This task owns rewrite history and the `STALLED` escape hatch. It does not
change capacity ceiling behavior; that is task 5.

## Files

- Modify: `src/secondsight/analysis/aggregator.py`
- Modify: `src/secondsight/feedback/directive_policy.py`
- Modify: `src/secondsight/storage/directive_revisions_repository.py`
- Modify: `src/secondsight/storage/directives_repository.py`
- Modify: `src/secondsight/analysis/schemas.py`
- Modify: `tests/feedback/test_directive_policy.py`
- Modify: `tests/storage/test_directive_revisions_repository.py`
- Modify: `tests/storage/test_directives_repository.py`

## Death Test Requirements

- Test: accepted rewrite preserves identity_key and appends one revision row
- Test: revision cap exhaustion transitions directive to `stalled` and stops
  further rewrite attempts
- Test: rejected rewrite does not overwrite the existing directive instruction

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

- Potential shortcut: incrementing revision_count without preserving revision
  content history
- Potential shortcut: treating `stalled` as a hidden boolean instead of a real
  lifecycle status
- Assumption to verify: revision acceptance/rejection should be observable from
  the ledger itself, not just from logs

## Acceptance Criteria

- Covers: "rewrite preserves directive row but silently breaks lineage identity"
- Covers: "revision cap reached for an ineffective convention"
