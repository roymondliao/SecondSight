# Overview: directive-lifecycle-hygiene

## Goal
Make directive lifecycle autonomous and concept-stable so conventions can fade,
revive, rewrite, and stay bounded without relying on manual expiry management.

## Architecture
Directive lifecycle is split into three layers: storage state on the directive
row, a deterministic policy layer for lifecycle decisions, and an identity
resolution layer that maps new aggregator output onto existing convention
lineage. Aggregator remains the source of emerged patterns; lifecycle policy
and revision history consume those signals and persist the results.

## Tech Stack
Python, Pydantic models, SQLAlchemy Core repositories/tables, existing
aggregator + lifecycle modules, FastAPI directive API, CLI directive listing,
pytest.

## Key Decisions
- `identity_key` is a project-scoped lineage id, assigned once and preserved
  across rewrite/revive cycles; it is no longer derived from session snapshots.
- `weight` is policy memory for "does this convention still deserve prompt
  budget", not a formula score and not an injection ordering input.
- `DISABLED` remains a human override state, but autonomous lifecycle logic
  excludes it entirely.
- `OBSOLETE` becomes system-dormant and auto-revivable; `STALLED` is added for
  revision-cap exhaustion.
- Prompt ordering remains frequency-based; capacity shedding and dormant
  transitions are weight-based.

## Death Cases Summary
1. Rewrite or repeated aggregation silently generates a new identity, breaking
   lineage and making revival/history impossible.
2. A still-problematic convention decays because source-flag recurrence is
   misread as success rather than "needs revision".
3. Lifecycle automation works internally but operator surfaces hide `obsolete`
   and `stalled`, making the system appear inert.

## File Map
- `src/secondsight/analysis/schemas.py` — lifecycle model/status extensions and
  new directive state fields.
- `src/secondsight/analysis/aggregator.py` — identity resolution integration
  and lifecycle signal emission.
- `src/secondsight/feedback/directive_identity.py` — concept identity matching
  and stable lineage assignment.
- `src/secondsight/feedback/directive_policy.py` — deterministic lifecycle
  decisions from aggregation signals.
- `src/secondsight/feedback/lifecycle_automation.py` — remove old
  source-flag-lookback revival and run the new policy flow.
- `src/secondsight/storage/directives_table.py` — new policy state columns.
- `src/secondsight/storage/directives_repository.py` — policy-state persistence
  and expanded listing/query support.
- `src/secondsight/storage/directive_revisions_table.py` — append-only revision
  ledger schema.
- `src/secondsight/storage/directive_revisions_repository.py` — revision ledger
  persistence.
- `src/secondsight/api/directives.py` — expose new states/fields to operator
  surfaces.
- `src/secondsight/cli/directive.py` — CLI visibility for dormant/system-owned
  states.
- `src/secondsight/config/schema.py`, `loader.py`, `template.py` — capacity
  ceiling config.
