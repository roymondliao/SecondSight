# Plan: directive-lifecycle-hygiene

## Step 1.5 Pre-thinking Resolution

See [`2-pre-thinking.md`](./2-pre-thinking.md). The only accepted gaps carried
forward are:

- lifecycle policy constants ship as module constants in V1 (except global
  capacity ceiling, which is config-tunable)
- revision history is stored in-project, append-only, via a dedicated table

## Step 2: Technical Specification

### Goal

Make convention lifecycle autonomous end-to-end:

- conventions no longer rely on `expires_at` or manual PATCH for retirement
- system-owned states (`obsolete`, `stalled`) become first-class and visible
- identity is concept-stable across repeated aggregations and rewrites
- `weight` becomes the policy memory for whether a convention still deserves
  prompt budget

### Core decisions

- `identity_key` is redefined as a **project-scoped lineage id** for one
  convention concept. It is assigned once when a new concept is created and
  preserved across rewrite, dormant/active transitions, and future
  aggregations.
- identity matching is a separate policy concern from identity assignment.
  Aggregator output is resolved against existing directives before deciding
  whether to reuse an existing `identity_key` or mint a new one.
- `weight` is **not** used for prompt ordering. Injection order remains tied to
  the existing `frequency` sort. `weight` only drives decay, dormant
  transitions, and capacity shedding.
- `DISABLED` remains a user override state. Autonomous lifecycle logic must
  not move rows into or out of `DISABLED`.

### Data model changes

#### Directive schema additions

Extend `Directive` and `directives` storage with:

- `weight: float`
- `miss_streak: int`
- `last_promoted_at: datetime | None`
- `last_source_flag_seen_at: datetime | None`
- `revision_count: int`
- `last_revised_at: datetime | None`

Add a new lifecycle status:

- `DirectiveStatus.STALLED = "stalled"`

Semantic contract:

- `ACTIVE`: eligible for injection and policy updates
- `OBSOLETE`: system-dormant, not injected, auto-revivable
- `STALLED`: revision cap reached, not auto-rewritten further; still visible to
  operator
- `DISABLED`: human override, outside autonomous lifecycle

#### Revision ledger

Add a new append-only table `directive_revisions` with one row per rewrite
attempt or accepted rewrite:

- `id`
- `project_id`
- `directive_id`
- `identity_key`
- `revision_index`
- `old_instruction`
- `new_instruction`
- `reason`
- `accepted`
- `review_note`
- `created_at`

This table is not a generic audit log. It exists to preserve the version
history of one convention concept.

### Component #1 — Identity resolution

#### Ownership

Identity resolution belongs to the feedback/analysis seam, not to the storage
layer and not to the API layer.

Create a dedicated module:

```python
# src/secondsight/feedback/directive_identity.py

@dataclass(frozen=True)
class IdentityResolutionResult:
    identity_key: str
    matched_directive_id: str | None
    matched_status: DirectiveStatus | None
    is_new_identity: bool
    reason: str

def resolve_or_create_identity(...) -> IdentityResolutionResult:
    ...
```

#### Behavior

Inputs:

- `project_id`
- emerged `flag_type`
- emerged `pattern_description`
- emerged `convention` text
- existing directives in statuses `{active, obsolete, stalled}`

Resolution order:

1. project-scoped candidate set only
2. same `flag_type` filter first
3. semantic match against existing conventions
4. if matched, reuse existing `identity_key`
5. if unmatched, create a new opaque lineage id

Lineage id generation:

- system-assigned, opaque, durable (`uuid7`/`uuid4` acceptable)
- never derived from `representative_sessions`
- never operator-supplied

The old `compute_identity_key(project_id, flag_type, representative_sessions)`
contract is retired for lifecycle identity. If a derived fingerprint remains
useful for observability, it should be renamed and treated as a diagnostic
artifact, not as canonical identity.

#### Output states

| State | Condition | Behavior |
|---|---|---|
| `success` | existing concept matched | reuse matched `identity_key` |
| `success` | no match found | assign new lineage id |
| `unknown` | semantic matcher cannot determine due to malformed directive state | fail closed in aggregation step, log why, do not silently mint a new lineage id |

#### Death cases

1. **A rewritten convention receives a new `identity_key`**. The system appears
   to preserve history, but the lineage is broken and revision count resets.
   Detect with a death test: accepted rewrite must preserve `identity_key`.
2. **An `obsolete` convention that semantically matches a new emerged pattern is
   treated as a new identity**. The system appears to learn, but revival never
   happens and capacity grows incorrectly. Detect with a death test covering
   match-to-obsolete resolution.
3. **`representative_sessions` still influence canonical identity**. The key
   changes across aggregations even when concept match succeeds. Detect by
   constructing two emerged patterns with different session sets but the same
   concept and asserting one identity.

### Component #2 — Weight policy layer

Create a dedicated deterministic policy module:

```python
# src/secondsight/feedback/directive_policy.py

@dataclass(frozen=True)
class DirectiveLifecycleSignal:
    directive: Directive
    now: datetime
    same_identity_repromoted: bool
    source_flag_seen: bool
    revision_cap_reached: bool

@dataclass(frozen=True)
class DirectivePolicyDecision:
    new_weight: float
    new_status: DirectiveStatus
    new_miss_streak: int
    should_revise: bool
    should_append_revision_candidate: bool
    reason: str

def evaluate_directive_policy(signal: DirectiveLifecycleSignal) -> DirectivePolicyDecision:
    ...
```

#### Meaning

`weight` means:

> the system's current belief that this convention still deserves to occupy
> prompt budget

It does **not** mean:

- causal effectiveness score
- direct frequency score
- display priority
- prompt order

#### V1 signal sources

- same `identity_key` re-promoted in current aggregation
- same `source_flag_type` seen in current aggregation
- directive's stored policy state (`miss_streak`, `revision_count`, etc.)

#### V1 policy rules

Exact numeric constants are config-tunable via `[directive_lifecycle]` in V1,
but the transition shape is fixed:

- same identity re-promoted:
  - increase `weight`
  - reset `miss_streak`
  - set `last_promoted_at`
  - set status to `ACTIVE`
- same identity not re-promoted but source flag type still seen:
  - do not decay `weight`
  - mark for revision
  - if revision cap reached, transition to `STALLED`
- source flag type absent:
  - increment `miss_streak`
  - once miss threshold is crossed, decay `weight`
  - when weight crosses obsolete threshold, transition to `OBSOLETE`

#### Output states

| State | Condition | Behavior |
|---|---|---|
| `success` | deterministic inputs valid | produce new weight/state decision |
| `failure` | impossible status or malformed policy state | raise loud error; no silent fallback |
| `unknown` | none in policy function itself | n/a |

#### Death cases

4. **Weight is accidentally used for prompt ordering**. The system appears to
   follow policy, but convention injection behavior silently changes. Detect
   with a test that prompt selection order remains frequency-based after weight
   data exists.
5. **A recurring but still-failing convention decays instead of holding for
   revision**. The system appears to forget learned rules, but actually drops
   rules that were merely ineffective. Detect with a death test:
   `source_flag_seen=True`, `same_identity_repromoted=False` must not decay.
6. **`DISABLED` enters autonomous transitions**. The system appears to respect
   human override, but policy resurrects or rewrites disabled rows. Detect with
   lifecycle tests that `DISABLED` rows are excluded from policy inputs.

### Component #3 — Aggregator / lifecycle integration

Aggregator remains the owner of emerged patterns. Lifecycle automation becomes a
consumer of aggregation signals rather than a separate heuristic engine.

Required changes:

- `aggregate_project_flags()` must resolve identity before UPSERT
- on match to existing `obsolete` directive, reuse identity and issue an
  explicit `OBSOLETE -> ACTIVE` transition
- on match to existing `active`/`stalled` directive, update fields in-place
- the old `flag_type + lookback` reactivation path in
  `feedback/lifecycle_automation.py` is removed or rewritten out of existence

Lifecycle automation after this change owns:

- autonomous decay / obsolete transitions
- explicit resurrection based on identity re-promotion
- capacity shedding

It no longer owns:

- rebound detection by `source_flag_type` lookback window

#### Death cases

7. **Aggregator re-promotes an obsolete convention, but repository UPSERT
   preserves status=obsolete**. The system appears to match identity, but the
   directive never becomes injectable again. Detect with an integration test
   covering `obsolete -> active`.
8. **Both old reactivation and new identity-driven revival run**. The system
   appears resilient, but status changes are now double-owned and non-
   deterministic. Detect with grep/test evidence that no `source_flag_type`
   lookback revival remains.

### Component #4 — Operator visibility

`DirectiveOut`, CLI directive listing, and any list/query surface used by the
operator must show system-owned states and policy metadata.

Required API changes:

- `active=false` listing includes `active`, `disabled`, `obsolete`, `stalled`
- response shape includes the new policy fields needed for inspection:
  `weight`, `miss_streak`, `last_promoted_at`, `last_source_flag_seen_at`,
  `revision_count`, `last_revised_at`

The public PATCH surface remains user-scoped:

- user may still toggle `active`/`disabled`
- user may not PATCH `obsolete`/`stalled`

#### Death cases

9. **Lifecycle automation runs but operator cannot see dormant states**. The
   system appears healthy in automation logs, but the API hides the outcome.
   Detect with API/CLI tests that `active=false` includes `obsolete` and
   `stalled`.

### Component #5 — Capacity ceiling

Per-project active convention count is bounded by a config-tunable ceiling.

Config surface:

```toml
[directive_lifecycle]
capacity_ceiling = 15
```

Policy:

- if active convention count exceeds the ceiling after aggregation/revision
  processing, transition the lowest-weight active convention(s) to `OBSOLETE`
- tie-break is deterministic and documented (`updated_at ASC`, then `id ASC`)

#### Death cases

10. **Capacity shedding removes the most frequent convention instead of the
    least justified one**. The system appears bounded, but loses the wrong
    convention. Detect with a deterministic fixture where weight and frequency
    intentionally disagree.
11. **Ceiling is enforced before resurrection/revision updates settle**. The
    system appears bounded, but decisions are made on stale weight/state.
    Detect with an integration test that shedding runs after policy updates.

## Step 2.5 Acceptance Criteria

See [`acceptance.yaml`](./acceptance.yaml). Order is death-first:

1. identity/lineage corruption paths
2. autonomous lifecycle corruption paths
3. operator visibility/capacity degradation paths
4. happy-path resurrection, revision, and bounded-count flows

## Step 3: Task Decomposition

Five self-contained tasks:

1. schema + storage foundation for lifecycle state and revision ledger
2. identity resolution and stable lineage assignment
3. weight policy layer and autonomous lifecycle transition rewrite
4. revision pipeline + stalled state + revision history persistence
5. capacity ceiling + operator visibility + end-to-end lifecycle invariants

## File Map

### New

- `src/secondsight/feedback/directive_identity.py` — project-scoped identity
  resolution and lineage assignment
- `src/secondsight/feedback/directive_policy.py` — deterministic lifecycle
  policy decisions
- `src/secondsight/storage/directive_revisions_table.py` — append-only revision
  ledger schema
- `src/secondsight/storage/directive_revisions_repository.py` — revision ledger
  writes/queries
- `tests/feedback/test_directive_identity.py`
- `tests/feedback/test_directive_policy.py`
- `tests/storage/test_directive_revisions_repository.py`

### Modified

- `src/secondsight/analysis/schemas.py`
- `src/secondsight/analysis/aggregator.py`
- `src/secondsight/feedback/lifecycle_automation.py`
- `src/secondsight/feedback/convention.py`
- `src/secondsight/storage/directives_table.py`
- `src/secondsight/storage/directives_repository.py`
- `src/secondsight/api/directives.py`
- `src/secondsight/cli/directive.py`
- `src/secondsight/config/schema.py`
- `src/secondsight/config/loader.py`
- `src/secondsight/config/template.py`
- `tests/feedback/test_lifecycle.py`
- `tests/feedback/test_dedup.py`
- `tests/storage/test_directives_repository.py`
- `tests/cli/test_directive.py`

### Possibly modified during implementation

- `src/secondsight/analysis/orchestrator.py` — if lifecycle sequencing must
  move to accommodate policy application order
- `tests/analysis/test_orchestrator.py`

## Risks Surfaced During Planning

- The current semantic dedup helper is instruction-text based and may need a
  modest expansion to operate against `obsolete` and `stalled` directives
  without confusing "revive" and "supersede".
- `identity_key` contract change is not backward-compatible with the current
  `compute_identity_key()` semantics. Existing code/tests that assume
  deterministic session-derived keys will need deliberate migration.
- Adding many lifecycle fields to `DirectiveOut` expands the agent/operator
  surface. Contract tests must freeze the new shape to avoid drift.
