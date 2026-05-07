# Plan: GUR-102 Phase 2 Analysis Orchestration

**Inputs:** `1-kickoff.md`, `problem-autopsy.md`, `2-pre-thinking.md`,
`peer-review-sebastian.md`, `peer-review-followup-sebastian.md`.
**Status of pre-thinking gate:** `accepted` (confirmation `a86a5fc5`).
**Authorization to proceed to plan:** comment `c3fc0f3a` from
`local-board` (2026-05-06T13:32:32).

## 1. Feature description

Wire Phase 2's pure functions and persistence into a running pipeline,
gated behind a typed `AnalysisAgent` Protocol that GUR-103 will
implement. Three new analysis modules + two new storage tables + one
schema migration on `directives`. Two callable entrypoints (session-
scope, project-scope) plus a thin chained convenience wrapper.

## 2. Ratified decisions (D-numbered, pinned from pre-thinking + reviews)

- **D1.** `AnalysisAgent` Protocol is the seam between GUR-102 (freeze)
  and GUR-103 (implement). Async-first, batched-segments,
  raises-on-irrecoverable-failure. Body verbatim in §3.4.
- **D2.** Two new tables: `analysis_runs` (pipeline-progress audit) and
  `session_reports` (artifact persistence). Distinct identities, distinct
  damage recipients (Sebastian round-2 G1).
- **D3.** `directives.identity_key TEXT` column added with
  `UNIQUE(project_id, identity_key)`. Hash =
  `sha256(flag_type + "|" + sorted(repr_session_ids).join(","))`.
  Migration is additive; table is empty pre-Phase 3, no backfill.
- **D4.** Orchestrator has three callables: `analyze_session(session_id,
  *, force=False)`, `aggregate_project(project_id)`,
  `analyze_and_aggregate(session_id, *, force=False)`. The chained
  wrapper short-circuits if zero new flags landed (Sebastian round-2 U2).
- **D5.** `convention_top_n=15` hard-coded as
  `DEFAULT_CONVENTION_TOP_N` constant in `analysis/aggregator.py`. No
  AnalysisConfig plumbing in v1 (premature abstraction; one knob).
  TODO comment cites SD §11 line 1392.
- **D6.** Re-running `analyze_session` on a completed session
  (`stage='summary_written'`) raises `SessionAlreadyAnalyzedError`
  unless `force=True`. Silent skip would hide data-loss bugs.
- **D7.** Backfill stage is a **verifier**, not a recoverer
  ("consumer-not-recoverer principle", Sebastian round-2 U1). Verifies
  upstream contract from GUR-99; raises `SessionIncompleteError` on
  violation; does not re-execute upstream work.
- **D8.** Failure budget — disable-on-arrival rate ≤ 30% across first
  20 sessions is a deferred Phase 2 ship gate, blocking GUR-104
  directive lifecycle from starting until verified. Not blocking
  GUR-102 ship.

## 3. Tech Spec — I/O with `unknown` outputs

### 3.1 `analysis/orchestrator.py` — Orchestrator

```python
class Orchestrator:
    def __init__(
        self,
        events_repo: EventsRepository,
        behavior_flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
        analysis_runs_repo: AnalysisRunsRepository,
        session_reports_repo: SessionReportsRepository,
        agent: AnalysisAgent,
    ) -> None: ...

    async def analyze_session(
        self, session_id: str, *, force: bool = False
    ) -> AnalyzeSessionResult: ...

    async def aggregate_project(
        self, project_id: str
    ) -> AggregateProjectResult: ...

    async def analyze_and_aggregate(
        self, session_id: str, *, force: bool = False
    ) -> AnalyzeAndAggregateResult: ...
```

#### 3.1.1 `analyze_session` — I/O

| State | Condition | Observable |
|---|---|---|
| `success` | All 5 stages completed; flags + report rows written | `AnalyzeSessionResult(stage='summary_written', flags_inserted=N, report_id=...)` |
| `failure` | `SessionIncompleteError`, `SessionAlreadyAnalyzedError`, `AnalysisAgentError`, validation error | Exception raised; `analysis_runs.stage='failed'` row written with `error_message` populated |
| `unknown` | Process killed mid-stage; DB write succeeded but stage transition was not committed | `analysis_runs.stage='segmented'` (or `'behavior_done'`) row exists with `completed_at IS NULL`; **detectable by:** `(updated_at < now - 5min AND stage NOT IN ('summary_written','failed'))` |

The `unknown` state is reachable on process kill mid-pipeline. Caller
on next session-end may either resume (if same session_id) or skip
(if different session). The `_recover_or_resume` entry helper inspects
the latest `analysis_runs` row before starting fresh.

#### 3.1.2 `aggregate_project` — I/O

| State | Condition | Observable |
|---|---|---|
| `success` | Step 1 grouped flags; Step 2 LLM call per non-empty group; Step 3 top-N upserted to directives | Returned `AggregateProjectResult(directives_upserted=K, calls_made=M)` |
| `failure` | LLM call raised on any flag_type; partial step-2 results discarded | Exception; **no partial directives written** (all-or-nothing per project run) |
| `unknown` | Step 2 succeeded for some groups; process killed before Step 3 | Step-2 results held in memory only — no leakage to DB; on retry, full re-run |

#### 3.1.3 `analyze_and_aggregate` — I/O

Composes `analyze_session` then optionally `aggregate_project`. Short-
circuit guard:

```python
session_result = await self.analyze_session(session_id, force=force)
if session_result.flags_inserted == 0:
    return AnalyzeAndAggregateResult(session=session_result, aggregate=None)
project_result = await self.aggregate_project(session_result.project_id)
return AnalyzeAndAggregateResult(session=session_result, aggregate=project_result)
```

### 3.2 `analysis/behavior.py` — Behavior detector

```python
async def detect_segment_flags(
    segment: SegmentData,
    metrics: SegmentMetrics,
    session_id: str,
    project_id: str,
    behavior_flags_repo: BehaviorFlagsRepository,
    agent: AnalysisAgent,
) -> int:
    """Build prompt → call agent → validate → persist. Returns flag count."""
```

| State | Condition | Observable |
|---|---|---|
| `success` | LLM returned valid `SegmentAnalysis`; M ≥ 0 flags persisted | Returns M |
| `failure` | `AnalysisAgentError` or `ValidationError` from agent | Exception propagates; zero flags written for this segment |
| `unknown` | Agent returned valid `SegmentAnalysis` but `BehaviorFlagsRepository._guard` rejected one flag mid-batch | First N-1 flags committed; flag N raises ValueError. **Detectable by:** count returned ≠ count in `flags` field of `SegmentAnalysis`. Resolution: `behavior.py` validates the entire `SegmentAnalysis.flags` against `_guard` rules **before** insert and raises early. |

### 3.3 `analysis/aggregator.py` — Cross-session aggregator

```python
DEFAULT_CONVENTION_TOP_N: Final[int] = 15

def compute_identity_key(
    flag_type: BehaviorFlagType,
    representative_sessions: Sequence[str],
) -> str:
    """sha256 of flag_type + "|" + sorted(repr_session_ids).join(",")."""

async def aggregate_project_flags(
    project_id: str,
    behavior_flags_repo: BehaviorFlagsRepository,
    directives_repo: DirectivesRepository,
    agent: AnalysisAgent,
    *,
    top_n: int = DEFAULT_CONVENTION_TOP_N,
) -> AggregateProjectResult:
    """Step 1 group → Step 2 per-flag-type LLM → Step 3 top-N UPSERT."""
```

| State | Condition | Observable |
|---|---|---|
| `success` | All non-empty groups returned valid `AggregateOutput`; ≤ top_n directives upserted | Result with counts |
| `failure` | Any group raises (timeout, validation) | Exception; **no directives upserted in this run** (all-or-nothing) |
| `unknown` | LLM returns same Step-1 input but variant Step-2 wording on re-run | Convention text drifts; **identity_key is stable** so rows converge via UPSERT, no duplicates |

### 3.4 `analysis/agent.py` — `AnalysisAgent` Protocol

```python
from __future__ import annotations
from typing import Protocol, Sequence, TypeVar

from secondsight.analysis.prompts.aggregate import AggregateOutput
from secondsight.analysis.prompts.summary import SummaryOutput
from secondsight.analysis.schemas import SegmentAnalysis

T = TypeVar("T", SegmentAnalysis, AggregateOutput, SummaryOutput)


class AnalysisAgentError(Exception):
    """Irrecoverable agent-side failure. Caller decides skip vs. fail-loud."""


class AnalysisAgent(Protocol):
    """Contract GUR-102 freezes; GUR-103 implements on PydanticAI."""

    async def analyze_segments(
        self,
        prompts: Sequence[str],
    ) -> list[SegmentAnalysis]: ...
    """Batched form. len(out) == len(in). Single-segment uses len==1."""

    async def aggregate_flag_type(
        self,
        prompt: str,
    ) -> AggregateOutput: ...
    """Per-flag-type call. Aggregator does its own fan-out."""

    async def summarize_session(
        self,
        prompt: str,
    ) -> SummaryOutput: ...
    """One call per session."""
```

The Protocol intentionally uses `list[SegmentAnalysis]` rather than a
generic `list[T]` — the input is always a segment-level prompt and the
output is always `SegmentAnalysis`. Three explicit methods replace a
single generic one because it makes the GUR-103 implementation
straight-line and lets each method evolve independently.

### 3.5 `storage/analysis_runs_table.py` + `analysis_runs_repository.py`

Schema (matches B1 of pre-thinking):

```python
analysis_runs = sa.Table(
    "analysis_runs",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("stage", sa.Text, nullable=False),
    sa.Column("started_at", sa.DateTime, nullable=False),
    sa.Column("updated_at", sa.DateTime, nullable=False),
    sa.Column("completed_at", sa.DateTime, nullable=True),
    sa.Column("error_message", sa.Text, nullable=True),
    sa.Column("flags_inserted", sa.Integer, nullable=False, server_default="0"),
)
sa.Index("idx_ar_project_session", analysis_runs.c.project_id, analysis_runs.c.session_id)
sa.Index("idx_ar_project_stage", analysis_runs.c.project_id, analysis_runs.c.stage)
```

Stage enum (validated at repository layer per D1 of GUR-100 mirror):
`pending | segmented | behavior_done | summary_written | aggregated |
failed`. Repository methods: `start_run / advance_stage / record_failure
/ get_latest_for_session / count_recent_partial`.

### 3.6 `storage/session_reports_table.py` + `session_reports_repository.py`

Schema (matches B2 of pre-thinking):

```python
session_reports = sa.Table(
    "session_reports",
    metadata,
    sa.Column("id", sa.Text, primary_key=True),
    sa.Column("project_id", sa.Text, nullable=False),
    sa.Column("session_id", sa.Text, nullable=False),
    sa.Column("analysis_run_id", sa.Text, nullable=False),
    sa.Column("headline", sa.Text, nullable=False),
    sa.Column("key_findings", sa.Text, nullable=False),  # JSON array
    sa.Column("body", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime, nullable=False),
    sa.Column("updated_at", sa.DateTime, nullable=False),
    sa.UniqueConstraint("session_id", name="uq_session_reports_session_id"),
)
sa.Index("idx_sr_project_created", session_reports.c.project_id, session_reports.c.created_at.desc())
```

Repository: `upsert(report) / get_for_session(session_id) /
list_for_project(project_id, limit, offset)`. UPSERT keyed on
`session_id` UNIQUE — re-run UPDATEs `analysis_run_id`,
`headline`, `key_findings`, `body`, `updated_at`.

### 3.7 Migration on `directives_table.py`

Additive:

```python
sa.Column("identity_key", sa.Text, nullable=False, server_default=""),
sa.UniqueConstraint("project_id", "identity_key", name="uq_directives_project_identity"),
```

`server_default=""` is a transitional default — only valid because
the table is empty pre-Phase 3 (G4). The repository computes the real
`identity_key` on every insert and rejects empty values via `_guard`.

`DirectivesRepository.upsert_with_identity_key(directive)` replaces
the old `insert(directive)` call path for aggregator-generated rows.
On `(project_id, identity_key)` conflict, UPDATE
`instruction, frequency, source_sessions, updated_at`. The
`status='active'` value is preserved on UPDATE; lifecycle transitions
remain GUR-104's concern.

## 4. Death cases (silent failures the design closes)

For each, document trigger / lie / truth / detection:

- **DC-1 — Pipeline killed mid-stage; no DB row marks the failure.**
  Lie: pipeline appears to have never run (no `analysis_runs` row).
  Truth: PID died after segmenter wrote `behavior_flags` rows.
  Detection: `analysis_runs.start_run` writes the row at stage entry,
  not exit. Even a SIGKILL after row insertion leaves a `pending` or
  `segmented` row visible to retry logic.

- **DC-2 — Behavior detector batch partially commits.** Lie:
  segment-level metric "flag count" matches reality. Truth: of M
  flags returned by LLM, N committed before `_guard` rejected flag
  N+1; orphan flags persist. Detection: `behavior.py` validates the
  entire `flags` array against `_guard` rules **before** any insert,
  raising early; insertion is `insert_many` in one transaction.

- **DC-3 — Aggregator silently drops Step-3 budget.** Lie: `top_n=15`
  ratified, but slicing the merged list with `[:15]` after a sort
  silently truncates ties at position 15. Truth: rank-15 and rank-16
  may have identical `occurrence_count`; deterministic tie-break by
  `flag_type ASC, pattern_description ASC` is required to make
  re-runs converge. Detection: golden-file test asserts deterministic
  ordering on tied counts.

- **DC-4 — Re-run on completed session silently re-LLMs.** Lie:
  caller-side "re-analyzing" feedback. Truth: without `force=True`
  guard, repeated invocation hits the LLM N times, costing $N×
  segments. Detection: `analyze_session` early-returns with
  `SessionAlreadyAnalyzedError` unless `force=True`; test pins this
  exact behavior.

- **DC-5 — Aggregator reads stale flags after retention purge.**
  Lie: cross-session statistics include "all" project history.
  Truth: GUR-147 retention purger may have deleted flags older than
  365 days; aggregator silently operates on the surviving subset.
  Detection: aggregator logs the count of flags read per project
  in the result; ship-manifest constrains aggregation to the
  retention window with a documented assumption.

- **DC-6 — Identity-key collision across distinct semantic patterns.**
  Lie: two genuinely-different conventions get UPSERTed into one row
  because they happened to share a `(flag_type, repr_sessions)`
  signature. Truth: Step-1 grouping is by `flag_type` only; if Step-2
  emerges two patterns from the same flag_type with overlapping
  representative_sessions, the hash collides. Detection:
  `compute_identity_key` reads `representative_sessions` directly from
  the LLM-emitted `AggregatePattern` (not the input flags), so each
  emerged pattern has its own session-set. Test pins this with two
  patterns sharing one source flag.

- **DC-7 — Consumer-not-recoverer violation goes silent.** Lie:
  orchestrator runs against an incomplete session and produces a
  "successful" but wrong analysis. Truth: GUR-99 backfill has not
  finished; some events for `session_id` are not in DB yet.
  Detection: `_verify_session_complete` asserts events count > 0 AND
  the session has a recorded end-event marker; raises
  `SessionIncompleteError` rather than degrading to "best effort".

- **DC-8 — Short-circuit elides aggregation when stale flags exist.**
  Lie: "zero new flags this run, skip aggregation" matches D4.
  Truth: skipping aggregation also means no convention churn — but
  if a previous aggregation happened with N stale flags that the
  retention purger just removed, active conventions reference
  vanished evidence. Detection: aggregator writes a
  `last_aggregated_at` timestamp per project (column on `directives`
  is overkill; instead, the aggregator scans `directives.updated_at`
  on entry and logs aggregator-skipped-but-stale events for
  ship-manifest review).

## 5. File map

### 5.1 New files

| Path | Purpose |
|---|---|
| `src/secondsight/analysis/agent.py` | `AnalysisAgent` Protocol + `AnalysisAgentError` |
| `src/secondsight/analysis/behavior.py` | `detect_segment_flags()` |
| `src/secondsight/analysis/aggregator.py` | `aggregate_project_flags()`, `compute_identity_key()`, constants |
| `src/secondsight/analysis/orchestrator.py` | `Orchestrator` class + result dataclasses |
| `src/secondsight/storage/analysis_runs_table.py` | Table definition |
| `src/secondsight/storage/analysis_runs_repository.py` | Repository methods |
| `src/secondsight/storage/session_reports_table.py` | Table definition |
| `src/secondsight/storage/session_reports_repository.py` | Repository methods |
| `tests/analysis/test_agent_protocol.py` | Protocol contract tests + `FakeAnalysisAgent` |
| `tests/analysis/test_behavior.py` | Death + happy tests |
| `tests/analysis/test_aggregator.py` | Death + happy tests |
| `tests/analysis/test_orchestrator.py` | Death + happy tests |
| `tests/storage/test_analysis_runs_repository.py` | |
| `tests/storage/test_session_reports_repository.py` | |

### 5.2 Modified files

| Path | Change |
|---|---|
| `src/secondsight/storage/directives_table.py` | Add `identity_key` column + `UNIQUE(project_id, identity_key)` |
| `src/secondsight/storage/directives_repository.py` | Add `upsert_with_identity_key(directive)` |
| `src/secondsight/analysis/__init__.py` | Re-export new modules |
| `tests/storage/test_directives_repository.py` | Add tests for UPSERT path |

### 5.3 SD updates

| SD section | Change | Required for |
|---|---|---|
| §7 | Add `analysis_runs` and `session_reports` table definitions | Schema completeness; D2 ratification |
| §7.4 | Document `identity_key` column on directives | D3 |
| §5.6 | Add note about `analyze_and_aggregate` short-circuit | D4 |

## 6. Citation correction

**Note for §Evidence in 1-kickoff.md:** the line "The orchestrator
(GUR-102) owns model invocation, retries, and JSON parsing" is in
`src/secondsight/analysis/prompts/behavior.py:5`, **not**
`summary.py:5`. The substantive point stands; the citation is corrected
here for plan-document accuracy (Sebastian round-1 review).

## 7. Open questions

None. All 4 gaps + 2 uncertainties from pre-thinking were resolved
either by board acceptance of the disposition (G2/G3/G4) or by
Sebastian's gate-time follow-up review (G1, U1, U2).

## 8. Task decomposition preview

5 tasks (matching GUR-100 5-task pattern). Full task files in
`tasks/task-N.md`; index in `index.yaml`.

| Task | Title | Depends on |
|---|---|---|
| task-1 | Storage layer: `analysis_runs` + `session_reports` tables and repos; `directives.identity_key` migration | — |
| task-2 | `AnalysisAgent` Protocol + `AnalysisAgentError` + `FakeAnalysisAgent` test double | — |
| task-3 | `analysis/behavior.py` — segment-level detector | task-1, task-2 |
| task-4 | `analysis/aggregator.py` — cross-session with stable-identity UPSERT | task-1, task-2 |
| task-5 | `analysis/orchestrator.py` — composer with verify-not-recover + short-circuit | task-1, task-2, task-3, task-4 |
