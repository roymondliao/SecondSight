# Planning Pre-thinking: Information Assumptions

To write this plan, I am assuming:

- `Directive` remains the persistence unit for convention injection, while a
  new append-only revision ledger carries rewrite history for the same
  convention identity.
- `identity_key` must become a project-scoped, system-assigned lineage id for
  one convention concept. It is not operator-supplied and not derived from
  session snapshots.
- `weight` is policy-layer state meaning "how strongly the system believes this
  convention still deserves prompt budget". It is not an effectiveness score,
  not a direct projection of `frequency`, and not a prompt ordering input.
- `DISABLED` remains in the public lifecycle contract as an operator override,
  but autonomous lifecycle automation must not transition into or out of
  `DISABLED`.
- `OBSOLETE` becomes system-dormant and auto-revivable. `STALLED` is added as
  a system-owned state for revision-cap exhaustion.
- Existing semantic dedup (`src/secondsight/feedback/dedup.py`) is reusable as
  the first version of identity resolution, even though it was originally
  scoped to ACTIVE conventions only.

Gaps I cannot resolve from Research:

- Research did not lock exact numeric policy constants for boost/decay,
  thresholds, or revision windows.
- Research did not define whether revision history belongs in the directives
  DB or a separate store, only that history must be append-only and queryable.

Uncertainties (I cannot determine if more information is needed):

- Whether the existing directive list API should surface all system-owned
  statuses by default when `active=false`, or whether a narrower operator view
  is still desired.

## Human Gate Resolution

The operator clarified or accepted the following during planning review:

- `STALLED` should be a new lifecycle status.
- `DISABLED` is retained for now, but a future change may revisit whether it
  belongs in SecondSight's long-term philosophy.
- `OBSOLETE -> ACTIVE` should be driven by concept identity, not by
  `source_flag_type` rebound.
- `weight` should be designed as a policy layer concept, not a formula-driven
  metric.
- `identity_key` must be project-based and concept-stable, not session-based.

Accepted undocumented assumptions carried into the plan:

- V1 ships directive lifecycle policy constants as config-tunable project
  settings under `[directive_lifecycle]`.
- Revision history is implemented inside the same SQLite project database as
  directives, via a dedicated append-only table.
- `active=false` operator views should include system-owned dormant states
  (`obsolete`, `stalled`) because otherwise lifecycle automation becomes
  invisible to the operator.
