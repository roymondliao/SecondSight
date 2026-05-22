# Task 2: Implement project-scoped identity resolution and stable lineage assignment

## Context

Read: `overview.md`, `2-plan.md` section "Component #1 — Identity resolution".

The core change here is semantic: canonical identity is no longer a hash of
session-derived inputs. It is a stable lineage id assigned once and reused
after matching.

## Files

- Create: `src/secondsight/feedback/directive_identity.py`
- Modify: `src/secondsight/analysis/aggregator.py`
- Modify: `src/secondsight/feedback/dedup.py`
- Modify: `src/secondsight/storage/directives_repository.py`
- Create: `tests/feedback/test_directive_identity.py`
- Modify: `tests/feedback/test_dedup.py`
- Modify: `tests/analysis/test_orchestrator.py`

## Death Test Requirements

- Test: two semantically same concepts with different representative session
  sets reuse one identity_key
- Test: rewrite/re-promote path preserves existing identity_key rather than
  minting a new one
- Test: match-to-obsolete directive reuses lineage id instead of creating a new
  directive identity

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

- Potential shortcut: keeping `compute_identity_key(...representative_sessions)`
  as canonical identity and just renaming it
- Potential shortcut: matching only ACTIVE directives and ignoring obsolete or
  stalled lineage reuse
- Assumption to verify: current Jaccard-based semantic dedup is sufficient for
  first-pass identity resolution

## Acceptance Criteria

- Covers: "same concept across different session snapshots gets a new identity"
- Covers: "new convention concept creates a stable lineage id"
- Covers: "recurring concept revives an obsolete directive by identity"
