# 2-plan: GUR-100 Phase 2 Analysis Core

> Prerequisites: `1-kickoff.md`, `problem-autopsy.md`, `2-pre-thinking.md`.
> Decisions locked in pre-thinking: **D1** (enum validation = repo Python),
> **D2** (one PR, 5 tasks), **D3** (same-PR SD update encoded as ship gate).

## Goal

Ship the analysis-layer **data foundation**: enum + Pydantic contract,
two SQLAlchemy tables + repositories, segmenter, supplementary metrics.
Does NOT include LLM calls, HTTP/REST, CLI, or aggregation logic — those
ship in GUR-101 (analysis prompts) and GUR-104 (CLI + REST).

## Architecture

- New package `src/secondsight/analysis/` with five files:
  `__init__.py`, `schemas.py` (enum + Pydantic models),
  `segmenter.py` (read-side assembler), `metrics.py` (pure-function
  metrics), and tables/repositories under
  `src/secondsight/storage/` (NOT `analysis/`) to keep all DB schema
  in one place — `behavior_flags_table.py` + `behavior_flags_repository.py`,
  `directives_table.py` + `directives_repository.py`.
- **Why DB tables under `storage/` not `analysis/`:** mirrors the
  existing `events_table.py` + `events_repository.py` pattern.
  `analysis/` stays purely analytical (Pydantic contracts, segmenter,
  metrics). Mixing tables into `analysis/` would split the storage
  surface across two packages.
- **No production code changes outside the new files** plus
  `docs/system_design.md` patches per D3. The hot path
  (hooks → API → events table) stays untouched.

## Tech Stack

- SQLAlchemy Core (existing) — Table + Index declarations,
  `metadata.create_all(checkfirst=True)`.
- Pydantic v2 (existing dependency) — `BehaviorFlag`, `Directive`,
  `SegmentData`, `ToolUseSpan`, `SegmentMetrics` models.
- Python 3.13+ — `Enum`, `Literal`, `dataclass`, `datetime`.
- pytest (existing) — unit tests + adversarial fixtures.

## Key Decisions (from research + pre-thinking)

- **D1 — Enum validation = Pydantic + repository defensive guard.** No
  DB CHECK. Mirrors `events.event_type` convention.
- **D2 — One PR, 5 internal tasks.** task-1 = scaffold (analysis pkg
  + schemas + Pydantic models), task-2 = behavior_flags table+repo,
  task-3 = directives table+repo, task-4 = segmenter, task-5 = metrics.
  Tasks 2–5 depend only on task-1.
- **D3 — Same-PR SD update is a ship-manifest gate.** Reviewer cannot
  merge unless `docs/system_design.md` diff includes both §5.5.2
  (`confidence`) and §7.4 (`disabled_at`, `disabled_reason`) edits.
- **Wake-context P2-1..P2-4 → in-repo task-1..task-5.** `index.yaml`
  carries the cross-reference; the renumbering factors out
  `schemas.py` as a contract anchor every other task imports.
- **Single Pydantic model serves both DB and LLM-output validation**
  (assumption A5 from pre-thinking). One source of truth.
- **Segmenter is an ASSEMBLER, not a re-segmenter.** Events already
  have `segment_index` from `SessionTracker.bind()`. The segmenter
  groups events by `segment_index` and pairs `tool_use_start`/`end`
  into `ToolUseSpan` instances. Module docstring states this
  explicitly to prevent reimplementation drift.
- **Orphan tool-use is NEVER silently dropped.** `tool_use_start`
  with no matching `end` → `ToolUseSpan(success=None,
  duration_ms=None)`. Symmetric for orphan `end` (no preceding
  `start`).
- **Segment with no triggering `user_prompt` event** (the implicit
  `segment_index=0` pre-prompt segment) → `SegmentData(user_prompt=None,
  events=[...])`. The LLM prompt builder (GUR-101) handles
  `user_prompt is None` per its own logic; the segmenter does not
  silently merge or skip.

## Death Cases (top 5 silent-failure paths this PR closes)

1. **Free-text `flag_type` drift.** LLM hallucinates a 7th flag type
   ("over_thinking"); without enum validation, the repository writes
   it; SD §5.5.1's "single source of truth" silently dies. Closed by
   Pydantic + repository defensive guard.
2. **Orphan `tool_use_start` silent drop.** Server crashed mid-tool;
   start row exists, end never arrived. Naive segmenter would skip
   the orphan. LLM analyzes a segment missing a real action. Closed
   by emitting `ToolUseSpan(success=None)`.
3. **Soft-disable forgotten.** Directive disabled by GUR-104's PATCH;
   `disabled_at` never written because column doesn't exist. Audit
   trail dies. Closed by shipping `disabled_at` + `disabled_reason`
   at table-creation time + repository test for the side effect.
4. **Status enum drift.** Future analyzer writes `status="expired_old"`;
   without repository validation, write succeeds; reader logic that
   checks `status == "expired"` silently misses the row. Closed by
   `DirectivesRepository.update_status()` enum guard.
5. **Null `token_count` distorts metrics.** Some events legitimately
   have null `token_count` (e.g. tool_use_start). Naive `sum(...)`
   raises `TypeError` mid-aggregation, OR (worse) silently coerces
   to 0 without telling anyone. Closed by explicit null-handling +
   WARNING log; metric returns 0 for that contribution but emits a
   structured log line so a future debugger can correlate.

## File Map

### New files (production)

- `src/secondsight/analysis/__init__.py` — package marker; re-exports
  `BehaviorFlag`, `BehaviorFlagType`, `Directive`, `DirectiveStatus`,
  `SegmentData`, `ToolUseSpan`, `SegmentMetrics`, `Segmenter`,
  `compute_segment_metrics` for ergonomic imports.
- `src/secondsight/analysis/schemas.py` — `BehaviorFlagType` enum
  (6 values per SD §5.5.1), `BehaviorFlag` Pydantic model,
  `DirectiveStatus` enum (5 values per SD §7.4),
  `DirectiveType` enum (`convention`, `hint`),
  `Directive` Pydantic model, `ToolUseSpan` Pydantic model,
  `SegmentData` Pydantic model, `SegmentMetrics` TypedDict / Pydantic.
- `src/secondsight/storage/behavior_flags_table.py` — SQLAlchemy table
  per SD §7.3 + `confidence` column.
- `src/secondsight/storage/behavior_flags_repository.py` — repository
  with `insert`, `insert_many`, `get_session_flags`,
  `get_project_flags_by_type`, `count_by_type`.
- `src/secondsight/storage/directives_table.py` — SQLAlchemy table per
  SD §7.4 + `disabled_at` + `disabled_reason` columns.
- `src/secondsight/storage/directives_repository.py` — repository with
  `insert`, `get_active_conventions`, `get_by_id`, `update_status`.
- `src/secondsight/analysis/segmenter.py` — `Segmenter` class +
  `SegmentData` assembly logic.
- `src/secondsight/analysis/metrics.py` — `compute_segment_metrics(segment)
  -> SegmentMetrics` pure function.

### New files (tests)

- `tests/analysis/__init__.py` — empty.
- `tests/analysis/test_schemas.py` — Pydantic validation tests
  (enum coverage, `confidence` enum, `DirectiveStatus` enum,
  `BehaviorFlag` round-trip, model-construct bypass behavior).
- `tests/analysis/test_segmenter.py` — pure-function tests with
  fixture event streams; adversarial: orphan start, orphan end,
  empty segment, sub-agent nesting in same segment, out-of-order
  sequence_number.
- `tests/analysis/test_metrics.py` — fixture-based tests; adversarial:
  null token_count, no file-touching tools, single-event segment
  (`duration=0` not `None`), sub-agent error propagation.
- `tests/storage/test_behavior_flags_repository.py` — insert /
  query / idempotency / enum-bypass guard.
- `tests/storage/test_directives_repository.py` — insert /
  get_active_conventions / update_status (transitions:
  active→disabled→active, disabled_at side effect, invalid status
  rejection, type filter).

### Modified files (production)

- `docs/system_design.md` — patch §5.5.2 to add `confidence` to the
  prompt output schema; patch §7.4 to add `disabled_at` and
  `disabled_reason` columns to the directive DDL block. **Required
  by D3 ship-manifest gate.**

## Tech Spec — I/O Contracts

### `BehaviorFlagType` enum (str, Enum)

```python
class BehaviorFlagType(str, Enum):
    UNNECESSARY_READ = "unnecessary_read"
    REDUNDANT_EXPLORATION = "redundant_exploration"
    MISSED_SHORTCUT = "missed_shortcut"
    REPEATED_OPERATION = "repeated_operation"
    WRONG_TOOL_CHOICE = "wrong_tool_choice"
    EXCESSIVE_CONTEXT_GATHERING = "excessive_context_gathering"
```

### `BehaviorFlag` (Pydantic v2 BaseModel)

```python
class BehaviorFlag(BaseModel):
    id: str                              # uuid4 hex; caller-supplied
    project_id: str
    session_id: str
    segment_index: int
    flag_type: BehaviorFlagType          # validated enum
    event_ids: list[str]                 # JSON-encoded by repository
    intent_summary: str
    reason: str
    confidence: Literal["high","medium","low"]
    created_at: datetime
```

- I/O states: `success` (validated and constructed) | `failure` (raises
  `ValidationError` with field-level message) | `unknown` — N/A for
  pure construction.

### `DirectiveStatus` enum (str, Enum)

```python
class DirectiveStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    OBSOLETE = "obsolete"
```

### `Directive` (Pydantic v2 BaseModel)

```python
class Directive(BaseModel):
    id: str
    project_id: str
    type: DirectiveType                          # convention | hint
    status: DirectiveStatus
    instruction: str
    frequency: float | None                      # convention only
    trigger_pattern: str | None                  # hint reserved
    confidence: float | None                     # hint reserved
    max_firing: int | None                       # hint reserved
    source_flag_type: str | None                 # use BehaviorFlagType.value
    source_sessions: list[str]                   # JSON-encoded by repository
    created_at: datetime
    expires_at: datetime | None
    updated_at: datetime
    disabled_at: datetime | None
    disabled_reason: str | None
```

### `ToolUseSpan` (Pydantic v2 BaseModel)

```python
class ToolUseSpan(BaseModel):
    tool_name: str
    target: str | None              # may be None for tools without target
    success: bool | None            # None = unknown (orphan start)
    duration_ms: int | None         # None = unknown
    start_seq: int                  # sequence_number of tool_use_start
    end_seq: int | None             # None if orphan start
    metadata: dict[str, object]     # passthrough from data field
```

- I/O states: `success` (paired) | `failure` (impossible at this layer
  — assembly never fails) | `unknown` (`success=None` when start has
  no matching end). The `unknown` state is **explicit and observable**,
  never silently coerced.

### `SegmentData` (Pydantic v2 BaseModel)

```python
class SegmentData(BaseModel):
    segment_index: int
    user_prompt: dict | None        # raw user_prompt event data; None for pre-prompt segment_index=0
    events: list[dict | ToolUseSpan]   # interleaved: thinking/response events as raw dicts, paired tool-uses as ToolUseSpan
    session_id: str
    project_id: str
```

- Note: `events` is intentionally heterogeneous — `ToolUseSpan` for
  paired tool-uses, raw dicts for thinking/response/sub_agent events.
  Trade-off: callers must dispatch on type. Alternative (tagged union)
  is more verbose without solving a real problem at v1.

### `SegmentMetrics` (TypedDict)

```python
class SegmentMetrics(TypedDict):
    total_tokens: int        # null token_count contributes 0 + WARNING log
    unique_files: int        # distinct `target` across file-touching tools
    duration: float          # seconds; first-event ts → last-event ts
    error_count: int         # ToolUseSpan with success=False (NOT None)
```

### `Segmenter` class

```python
class Segmenter:
    def __init__(self, events_repo: EventsRepository) -> None: ...

    def segment_session(self, session_id: str) -> list[SegmentData]:
        """Group session events by segment_index; pair tool-uses.

        Death cases:
        - orphan tool_use_start → ToolUseSpan(end_seq=None, success=None,
          duration_ms=None). NEVER dropped.
        - orphan tool_use_end → ToolUseSpan(start_seq=event.sequence_number,
          end_seq=event.sequence_number, success=event.data.get("success"),
          duration_ms=event.duration_ms). Synthesized as a 0-duration span
          with WARNING log; NEVER dropped.
        - segment_index=0 pre-prompt segment (events before any
          USER_PROMPT) → SegmentData(user_prompt=None, ...).
        - empty segment (USER_PROMPT with no following events) →
          SegmentData(events=[]).
        """
```

### `compute_segment_metrics(segment) -> SegmentMetrics`

```python
def compute_segment_metrics(segment: SegmentData) -> SegmentMetrics:
    """Pure function. No DB. No side effects (except WARNING logs)."""
```

## Tech Spec — Repository Contracts

### `BehaviorFlagsRepository`

| Method | Signature | Idempotency | Death case |
|---|---|---|---|
| `create_schema()` | `() -> None` | `metadata.create_all(checkfirst=True)` | — |
| `insert(flag)` | `(BehaviorFlag) -> None` | `INSERT … ON CONFLICT(id) DO NOTHING` | `model_construct` bypasses Pydantic → repository defensive guard re-validates `flag_type` against `BehaviorFlagType` enum, raises `ValueError` |
| `insert_many(flags)` | `(Sequence[BehaviorFlag]) -> int` | same | same |
| `get_session_flags(session_id)` | `(str) -> list[BehaviorFlag]` | — | — |
| `get_project_flags_by_type(project_id, flag_type)` | `(str, BehaviorFlagType) -> list[BehaviorFlag]` | — | — |
| `count_by_type(project_id)` | `(str) -> dict[BehaviorFlagType, int]` | — | — |

### `DirectivesRepository`

| Method | Signature | Idempotency | Death case |
|---|---|---|---|
| `create_schema()` | `() -> None` | `metadata.create_all(checkfirst=True)` | — |
| `insert(directive)` | `(Directive) -> None` | `INSERT … ON CONFLICT(id) DO NOTHING` | `model_construct` bypasses Pydantic → repository defensive guard re-validates `status`, `type` |
| `get_active_conventions(project_id)` | `(str) -> list[Directive]` | — | filter: `type='convention' AND status='active'`, ordered by `frequency DESC` |
| `get_by_id(id)` | `(str) -> Directive \| None` | — | — |
| `update_status(directive_id, new_status, reason=None)` | `(str, DirectiveStatus, str \| None) -> None` | UPDATE | invalid `new_status` → `ValueError`. `disabled` transition requires non-None `reason`; non-`disabled` transition with `reason` raises `ValueError`. `disabled_at` set on `→disabled`, cleared on `→active`. |

## Test Inventory (per-task)

### task-1 — `analysis/__init__.py` + `analysis/schemas.py`

- 6 enum-coverage tests (one per `BehaviorFlagType` value) ensuring `value`
  is the exact SD §5.5.1 string.
- 5 enum-coverage tests for `DirectiveStatus` (one per value).
- 2 enum-coverage tests for `DirectiveType`.
- 4 Pydantic validation tests: `BehaviorFlag` rejects unknown
  `flag_type`, rejects unknown `confidence`, accepts valid round-trip,
  preserves `event_ids` order.
- 3 Pydantic validation tests: `Directive` rejects unknown `status`,
  validates `disabled_at` is None when `status=active`, accepts full
  field set with optional fields = None.
- 2 `ToolUseSpan` tests: `success=None` is allowed (orphan), `success=True`
  + `duration_ms=None` is rejected (incoherent — "succeeded with unknown
  duration" is a contract violation).
- 2 `SegmentData` tests: `user_prompt=None` is allowed (pre-prompt segment),
  `events=[]` is allowed (empty segment).

### task-2 — `behavior_flags` table + repository

- DT-2.1 (death): `model_construct(flag_type="bogus")` → `repo.insert()`
  raises `ValueError`. Without the defensive guard, this silently
  inserts a row with an invalid enum.
- DT-2.2 (death): two `insert()` calls with same `id` and different
  `flag_type` → only the first persists. Verify by `get_session_flags`.
- 1 happy path: `insert(flag)` then `get_session_flags(session_id)`
  round-trips, including JSON-encoded `event_ids` decoded back to
  `list[str]`.
- 1 happy path: `insert_many(N=50)` returns 50, `count_by_type`
  reflects distribution.
- 1 query test: `get_project_flags_by_type` filters correctly.

### task-3 — `directives` table + repository

- DT-3.1 (death): `update_status(..., DirectiveStatus.DISABLED, reason=None)`
  → `ValueError("disabled transitions require a reason")`. Death of
  the audit trail.
- DT-3.2 (death): `update_status(..., DirectiveStatus.ACTIVE, reason="late")`
  → `ValueError("non-disabled transitions cannot carry a reason")`.
- DT-3.3 (death): `model_construct(status="frozen")` → `insert` raises
  `ValueError`. Same enum-bypass guard as task-2.
- DT-3.4 (death): transition `active → disabled → active` clears
  `disabled_at` and `disabled_reason` to None. Without the clear,
  re-active rows still show stale "disabled at..." metadata.
- 1 happy path: `insert(directive)` then `get_active_conventions(project_id)`
  returns it sorted by `frequency DESC`.
- 1 happy path: `update_status(active → superseded)` works (analyzer
  path); `disabled_at` stays None.
- 1 happy path: `get_by_id` round-trips.

### task-4 — `analysis/segmenter.py`

- DT-4.1 (death): orphan `tool_use_start` (no matching end) → `ToolUseSpan`
  with `success=None`, `end_seq=None`. Failing assertion: span exists,
  not omitted.
- DT-4.2 (death): orphan `tool_use_end` (no preceding start) →
  synthesized `ToolUseSpan` with `start_seq == end_seq`,
  `duration_ms=event.duration_ms`. Span exists, WARNING log present.
- DT-4.3 (death): pre-prompt events (segment_index=0, no USER_PROMPT
  yet) → `SegmentData(user_prompt=None, events=[...])`. Segment
  exists; not silently merged into segment 1.
- DT-4.4 (death): empty segment (USER_PROMPT followed immediately by
  next USER_PROMPT) → `SegmentData(events=[])`. Segment exists with
  zero events; not silently dropped.
- DT-4.5 (death): out-of-order `sequence_number` (should never happen
  at ingest) → segmenter raises `ValueError`, does NOT silently sort
  and proceed.
- 1 happy path: 8-event session
  (start → user-prompt → pre/post-tool-use → user-prompt → pre/post →
  end) → 2 SegmentData (segment_index 1 and 2), each with 1 paired
  ToolUseSpan.
- 1 happy path: nested sub-agent in segment 1 → SegmentData.events
  contains `sub_agent_start`/`sub_agent_end` raw dicts AND the
  paired ToolUseSpan from inside the sub-agent.

### task-5 — `analysis/metrics.py`

- DT-5.1 (death): events with `token_count=None` → `total_tokens` = sum
  of non-None values; WARNING log emitted naming the event_id; result
  is NOT silently NaN or raise `TypeError`.
- DT-5.2 (death): single-event segment (only USER_PROMPT) → `duration=0.0`
  not None. None would imply "no duration measurable" which is a
  different state than "0.0 seconds elapsed".
- DT-5.3 (death): empty segment (`events=[]`) → all four metrics =
  0/0/0.0/0; not raise, not None.
- 1 happy path: fixture segment with 5 events, 3 distinct file targets,
  1 error → metrics match hand-computed values exactly.
- 1 invariant test: re-running on same segment returns identical
  metrics (purity).

## Step 0 Commitments (carried forward from kickoff)

1. **Most-wanted shortcut REJECTED:** "skip enum validation at
   repository layer; trust Pydantic." Rejected because
   `model_construct()` bypasses Pydantic and is the standard escape
   hatch for performance — the repository layer must defensively
   re-validate.
2. **This implementation MUST NOT ship when:**
   - SD §5.5.2 + §7.4 patches are absent from the PR diff (D3 gate).
   - The segmenter silently drops orphan tool_use events (any DT-4.x
     red).
   - `disabled_at` / `disabled_reason` are absent from
     `directives_table.py`.
3. **Silent failure surface this PR closes:** see Death Cases section
   above.
4. **What lives one year from now:** `BehaviorFlagType` enum (most
   load-bearing), the two tables, the repositories. Segmenter and
   metrics are the most replaceable; both could be absorbed by GUR-101
   if the boundary turns out to be wrong.

## Risks

- **Pydantic v2 + SQLAlchemy Core round-tripping** is well-trodden but
  the JSON-encoded TEXT columns require explicit `json.dumps`/`json.loads`
  in the repository. Existing `events_repository._row_to_event` is the
  reference pattern; mirror it carefully.
- **Test fixture explosion:** 5 task files × ~5 tests each = ~25 tests
  before adversarial coverage. Adversarial fixtures push it to ~40–50
  tests. Acceptable; comparable to GUR-99's ~25-test count.
- **`docs/system_design.md` patch correctness:** the SD edits land in
  the same PR as the code. If the SD patch is wrong (e.g. types a
  column name as `disabled_at` in code but `disabled_time` in SD),
  the ship-manifest grep may pass on string match while the actual
  contract is broken. Mitigate by reviewer reading both diffs.
- **Hidden coupling to GUR-101:** `BehaviorFlag` Pydantic model and
  `SegmentData` are consumed by Karpathy's prompt builder. If the
  shape needs adjusting after GUR-101 starts, it's a coordinated
  schema change. Acceptable — GUR-101 hasn't started; we own the
  contract first.

## Out of Scope (re-confirmed from kickoff and pre-thinking)

- HTTP/REST endpoints (GUR-104).
- CLI subcommands (GUR-104).
- LLM analysis prompts and rendering (GUR-101).
- Cross-session aggregation / convention generation (GUR-101).
- Alembic migrations (project decision: no Alembic in MVP).
- Dashboard UI (GUR-106).
- `read_project_file` analysis tool (GUR-101 or later).
- Span-splitting for long segments per SD §5.3.3 (GUR-101).
- Concurrent-writer / version-column mechanics (assumption U2).
- Expiry-checking logic on `expires_at` (assumption U4 — GUR-101).

## Success Criteria

- All death tests (DT-2.1..DT-5.3) green deterministically.
- All happy-path tests green deterministically.
- 30 consecutive runs of `pytest tests/analysis/ tests/storage/test_behavior_flags_repository.py tests/storage/test_directives_repository.py -v` are 30/30 green.
- `git diff main -- docs/system_design.md` includes both §5.5.2 and §7.4 edits.
- Total LoC (production + test): ≤ 1500 lines (alarm if exceeded).
- No production code changes outside the new files + the SD patches.
- Existing test suite continues to pass.
