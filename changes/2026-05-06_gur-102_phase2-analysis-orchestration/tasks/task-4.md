# Task 4 — `analysis/aggregator.py` — Cross-session aggregator

**Depends on:** task-1 (DirectivesRepository, BehaviorFlagsRepository),
task-2 (AnalysisAgent). **Blocks:** task-5.

## Goal

Build the cross-session aggregator implementing SD §5.5.3:

- **Step 1 (automated):** Group all flags for a project by `flag_type`.
- **Step 2 (LLM, one call per non-empty group):** Get `AggregateOutput`.
- **Step 3 (automated):** Merge all `AggregatePattern` instances across
  all flag_types, sort by `occurrence_count DESC` with deterministic
  tie-break, take top `DEFAULT_CONVENTION_TOP_N`, UPSERT to `directives`
  via stable `identity_key`.

## Files to create

- `src/secondsight/analysis/aggregator.py`
- `tests/analysis/test_aggregator.py`

## Files to modify

- `src/secondsight/analysis/__init__.py` — re-export
  `aggregate_project_flags`, `compute_identity_key`,
  `DEFAULT_CONVENTION_TOP_N`.

## Module surface

```python
DEFAULT_CONVENTION_TOP_N: Final[int] = 15
# TODO(future): make configurable via analysis_config.toml
# convention_top_n key (SD §11 line 1392). Hard-coded for v1 — see
# pre-thinking G2 for the rationale on deferring config plumbing.


def compute_identity_key(
    flag_type: BehaviorFlagType,
    representative_sessions: Sequence[str],
) -> str:
    """Stable hash for directives UPSERT.

    Input: emerged AggregatePattern's (flag_type, representative_sessions)
    — NOT the input flags. This is critical: two patterns emerging from
    the same flag_type with overlapping but distinct session-sets must
    produce distinct identity_keys (DC-6).

    Returns: hex sha256 of `flag_type.value + "|" +
    sorted(representative_sessions).join(",")`.
    """


@dataclass(frozen=True)
class AggregateProjectResult:
    project_id: str
    calls_made: int          # number of per-flag-type LLM calls
    flags_read: int          # total flags read in Step 1 (DC-5 disclosure)
    patterns_emerged: int    # before top-N truncation
    directives_upserted: int # at most DEFAULT_CONVENTION_TOP_N
    aggregated_at: datetime


async def aggregate_project_flags(
    project_id: str,
    *,
    behavior_flags_repo: BehaviorFlagsRepository,
    directives_repo: DirectivesRepository,
    agent: AnalysisAgent,
    top_n: int = DEFAULT_CONVENTION_TOP_N,
) -> AggregateProjectResult:
    """Run Step 1 → Step 2 → Step 3 for one project.

    All-or-nothing: if any Step-2 LLM call raises, no directives are
    upserted in this run. Caller can retry.
    """
```

## Step-by-step semantics

### Step 1 — Group

```python
flags_by_type: dict[BehaviorFlagType, list[FlagSummary]] = defaultdict(list)
for flag_type in BehaviorFlagType:
    flags = behavior_flags_repo.get_project_flags_by_type(project_id, flag_type)
    if flags:
        flags_by_type[flag_type] = [
            FlagSummary(
                session_id=f.session_id,
                segment_summary=f.segment_summary,  # NOTE: see scar item
                reason=f.reason,
            )
            for f in flags
        ]
flags_read = sum(len(v) for v in flags_by_type.values())
```

### Step 2 — Per-flag-type LLM calls

```python
all_patterns: list[tuple[BehaviorFlagType, AggregatePattern]] = []
for flag_type, summaries in flags_by_type.items():
    prompt = build_aggregate_prompt(flag_type, summaries)
    output = await agent.aggregate_flag_type(prompt)  # may raise
    for pattern in output.patterns:
        all_patterns.append((flag_type, pattern))
```

### Step 3 — Merge top-N + UPSERT

```python
all_patterns.sort(
    key=lambda fp: (
        -fp[1].occurrence_count,           # primary DESC
        fp[0].value,                        # tie-break 1: flag_type ASC (deterministic)
        fp[1].pattern_description,          # tie-break 2: description ASC
    ),
)
top = all_patterns[:top_n]

upserted = 0
for flag_type, pattern in top:
    identity_key = compute_identity_key(flag_type, pattern.representative_sessions)
    directive = Directive(
        id=str(uuid.uuid4()),
        project_id=project_id,
        type=DirectiveType.CONVENTION,
        status=DirectiveStatus.ACTIVE,
        instruction=pattern.convention,
        frequency=float(pattern.occurrence_count) / flags_read if flags_read else 0.0,
        source_flag_type=flag_type.value,
        source_sessions=list(pattern.representative_sessions),
        identity_key=identity_key,
        created_at=now,
        updated_at=now,
    )
    directives_repo.upsert_with_identity_key(directive)
    upserted += 1
```

## Death tests (write FIRST)

- **DT-4.1 (= DT-1.3) — Deterministic tie-break at top_n boundary
  (DC-3).** Construct 20 patterns where ranks 14, 15, 16 share
  `occurrence_count=5`. Run aggregator twice with the same inputs;
  assert both runs select the same row at rank 15. Verify by
  reading `directives` after each run and asserting the chosen
  pattern_description is identical.

- **DT-4.2 (= DT-1.5) — Two patterns, same flag_type, distinct
  identity (DC-6).** Construct two `AggregatePattern` instances
  for `BehaviorFlagType.UNNECESSARY_READ` with overlapping but
  distinct `representative_sessions`. After aggregation, both rows
  exist in `directives` with distinct `identity_key`. Re-run the
  aggregator: still 2 rows (UPSERT updates, doesn't duplicate).

- **DT-4.3 (= DG-2.1) — Partial step-2 failure writes nothing.**
  `FakeAnalysisAgent` configured to succeed on the first
  `aggregate_flag_type` call and raise `AnalysisAgentError` on the
  second. Run aggregator; assert it raises and that `directives`
  has zero rows added since the call started.

- **DT-4.4 (= DT-1.8) — `flags_read` disclosed in result (DC-5).**
  Project with 100 historical flags, 30 deleted by retention purge
  (test mocks the purge). Aggregator's result has
  `flags_read=70`. This is the disclosure surface for the
  retention-window dependency.

- **DT-4.5 — Empty project (zero flags).** `aggregate_project_flags`
  on a project with no flags: `calls_made=0`, `flags_read=0`,
  `patterns_emerged=0`, `directives_upserted=0`. No LLM calls made.

- **DT-4.6 — `compute_identity_key` stability.** Same inputs always
  produce same hash. `representative_sessions` order does not
  matter (sorted before hash). `flag_type` enum value is the input,
  not the enum object (resilience to enum re-ordering).

## Happy-path tests

- **HP-4.A (= HP-3.2) — Aggregate, then re-run idempotent via
  identity_key.** First call creates K directives; second call
  with same flag inputs UPSERTs (no row count change); convention
  text may differ on re-run (LLM nondeterminism), `identity_key`
  unchanged.

- **HP-4.B — top_n bound respected.** Construct 25 patterns;
  aggregator UPSERTs exactly 15 directives (the default top_n).
  The other 10 patterns are not persisted.

## Scar items to record

- **`segment_summary` field on flags:** the existing
  `BehaviorFlagsRepository.get_project_flags_by_type` returns
  `BehaviorFlag` rows. Need to confirm if `segment_summary` is a
  field on the row or must be re-derived. **Verify in task-1 review
  whether the `BehaviorFlag` schema carries `segment_summary`** or
  whether the aggregator needs to join against another source.
  If it's not on the row, this is a scope adjustment for task-1.
- All-or-nothing per project run is simpler than partial commit
  with reconciliation. Cost: one failure on flag_type N requires
  re-running flag_types 1..N-1. Acceptable because per-call cost
  is small (Haiku) and total fan-out is small (≤ 7 flag types).
- Top-N tie-break is `(flag_type ASC, pattern_description ASC)`.
  Deterministic across re-runs. Different from "sort stable" which
  depends on input order.
- `frequency` field semantics: `occurrence_count / flags_read` for
  this project. NOT global frequency. Document at the call site.
- `source_sessions` field on directives stores `representative_sessions`
  from the emerged `AggregatePattern`. NOT the full set of session_ids
  that contributed flags to the input — that distinction matters for
  the identity_key collision argument (DC-6).
