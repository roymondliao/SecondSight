# Kickoff: gur-102-phase2-analysis-orchestration

## Problem Statement

Phase 2 has a complete data layer (GUR-100: schemas, repositories, segmenter,
metrics) and complete prompt builders (GUR-101: behavior, aggregate, summary).
None of it executes. There is no module that, given a finished session,
produces stored `BehaviorFlag` rows and a session report; and no module that,
given a project's accumulated flags, produces stored `Directive` rows.
GUR-102 is the wiring layer that turns the assembled parts into a running
analysis pipeline.

## Evidence

- `src/secondsight/analysis/` has `schemas.py`, `segmenter.py`, `metrics.py`,
  and `prompts/{behavior,aggregate,summary}.py` — but **no orchestrator,
  no behavior detector, no aggregator**. GUR-103 (PydanticAI agent + LLM
  router) is `blocked` on GUR-102, confirming this layer must define the
  agent invocation contract that GUR-103 implements.
- `BehaviorFlagsRepository.insert / insert_many / get_session_flags /
  get_project_flags_by_type / count_by_type` exist and are tested.
  `DirectivesRepository.insert / get_active_conventions / update_status`
  exist and enforce a soft-disable lifecycle. The persistence side is
  ready; the producer side is not.
- SD §5.5.3 Step 2 docstring and `summary.py:5` both state explicitly:
  "The orchestrator (GUR-102) owns model invocation, retries, and JSON
  parsing." This module is named in the design doc as the integration seam.
- Recent commits (`3330839`, `1be4a0d`) show GUR-101 finished prompt
  rendering and pinned UX defaults; the next concrete user-visible
  outcome ("session ended → I see flags + a report + conventions") cannot
  ship without GUR-102.

## Risk of Inaction

- **Phase 2 stalls.** GUR-103 blocked → GUR-104 (directive lifecycle) and
  GUR-106 (dashboard) have no flag/directive rows to consume. The
  Phase 1→2→3 chain (memory: `project_phase1_to_3_chain.md`) breaks at
  the joint between observation and feedback.
- **Silent contract drift.** Without GUR-102 freezing the
  `AnalysisAgent` Protocol shape, GUR-103 invents its own surface and
  the prompt builders end up coupled to a concrete client rather than an
  interface — making future SDK/CLI/local-model swaps harder than they
  should be.
- **No first end-to-end signal.** Until a session-end can produce
  observable rows, every Phase 2 unit test is local correctness with no
  proof that the pieces compose. Death tests at the orchestrator level
  are the first place silent integration failures surface.

## Scope

### Must-Have (with death conditions)

- **`AnalysisAgent` Protocol contract (in `analysis/orchestrator.py` or
  `analysis/agent.py`)** — A typed interface for "given prompt + output
  schema, return a validated Pydantic instance or raise". Death
  condition: if GUR-103 cannot satisfy this Protocol without > 2
  breaking changes within 30 days, the abstraction is wrong and must be
  re-shaped or dropped entirely (give up the Protocol and pass a callable).

- **`analysis/behavior.py` — segment-level flag detection (P2-9)** —
  Build segment prompt → call agent → validate `SegmentAnalysis` →
  persist `BehaviorFlag` rows. Death condition: if `SegmentAnalysis`
  validation fail rate exceeds 10% across the first 20 real sessions in
  Phase 2 dogfooding, the prompt or the parser is wrong; demote the
  detector to "log-only, do not insert" until fixed.

- **`analysis/aggregator.py` — cross-session aggregation (P2-10)** —
  Step 1 (group flags by `flag_type`, automated) → Step 2 (per-flag-type
  LLM call, returns `AggregateOutput`) → Step 3 (sort all patterns by
  `occurrence_count` descending, take top `convention_top_n`, write to
  `directives` table). Death condition: if the directives produced have
  < 50% human-review acceptance rate after Phase 3 dogfooding, this
  aggregator's flag-type partitioning is too coarse; revisit by allowing
  cross-flag-type clustering.

- **`analysis/orchestrator.py` — session-scope + cross-session
  pipeline (P2-8)** — `analyze_session(session_id)` runs:
  backfill check → segmenter → per-segment behavior detector → summary
  prompt → write report. Then `aggregate_project(project_id)` runs the
  cross-session pipeline. Both write to DB and return a structured
  result; failures are logged, partial progress is preserved, and the
  caller can re-run safely (idempotency rests on repo `INSERT ON CONFLICT
  DO NOTHING`). Death condition: if > 5% of triggered analyses leave the
  database in a partially-written state (flags inserted, no report
  written, or vice-versa), the orchestrator's transactional discipline
  is broken; introduce per-stage status tracking before shipping more
  features on top.

- **Empty-input handling (death cases co-located)** — A session with
  zero events, zero segments, or all-low-confidence flags must produce
  observable empty output (zero rows + an explicit "no segments" report)
  rather than silently no-op. Death condition: if any of these inputs
  causes the pipeline to silently exit early without a row in either
  the success log or an error log, the silent-failure surface has
  reopened.

### Nice-to-Have

- Concurrent per-segment LLM calls (sequential is fine for v1; the LLM
  router itself does no parallelism per SD §5.7.4).
- Prometheus-style structured metrics on segment count, flag count,
  convention count per run (just `logging.info` is enough for v1).
- Retry/backoff inside `behavior.py` (the agent layer in GUR-103 owns
  retries; orchestrator should treat agent calls as already-resilient).
- Pluggable Step 1 grouping strategy (one strategy — group by
  `flag_type` — is enough; SD §5.5.3 prescribes it).

### Explicitly Out of Scope

- LLM router / model selection / fallback model logic — GUR-103.
- PydanticAI agent loop / tool binding — GUR-103.
- Auto-trigger on session-end event (hook integration) — GUR-103 task
  P2-15. GUR-102 exposes `analyze_session(session_id)` as a callable;
  the trigger that calls it is GUR-103's concern.
- `secondsight analyze` CLI subcommand — GUR-103 task P2-15.
- Directive lifecycle transitions beyond initial insert (active →
  effective / obsolete / re-activated) — GUR-104.
- Dashboard rendering / API endpoints over flags + directives — GUR-106.

## North Star

```yaml
metric:
  name: "session_to_observable_outcome_completion_rate"
  definition: |
    For each session-end signal, the % that result in (a) ≥ 1
    BehaviorFlag row OR an explicit empty-segments report row AND a
    SummaryOutput report row, all within 60 s of trigger, with no
    half-written state (flags but no report, or report but no flags
    when flags should exist).
  current: 0  # nothing wired yet
  target: 0.95
  invalidation_condition: |
    The metric is wrong if GUR-103 lands and the LLM returns >50%
    low-confidence flags by default — completion rate stays high
    while output quality is noise. In that case switch to a
    confidence-weighted variant ("≥ 1 high-confidence flag OR an
    explicit empty report").
  corruption_signature: |
    Pipeline reports success but BehaviorFlag table grows by 0 rows
    across 10 consecutive non-empty sessions → detector is
    short-circuiting silently (e.g., catch-all except, JSON parse
    failure swallowed). Conversely, BehaviorFlag table grows by
    > 50/segment → detector is hallucinating; cap and alert.

sub_metrics:
  - name: "segment_analysis_validation_pass_rate"
    current: 0
    target: 0.90
    proxy_confidence: high
    decoupling_detection: |
      If validation passes but downstream `BehaviorFlagsRepository
      ._guard` rejection rate climbs above 1%, the prompt's enum
      coverage diverged from BehaviorFlagType — checked nightly.

  - name: "directive_acceptance_at_review"
    current: 0
    target: 0.50  # Phase 3 dogfooding gate, not Phase 2 ship gate
    proxy_confidence: low
    decoupling_detection: |
      Aggregator can produce many directives the user marks
      `disabled` immediately on the dashboard (GUR-106 telemetry).
      If disable-on-arrival > 30%, top-N is producing slop even
      though the metric says "directives generated".
```

## Stakeholders

- **Decision maker:** Project lead (board user) — locks Protocol shape
  and `convention_top_n` policy at the planning gate.
- **Impacted teams:**
  - GUR-103 (Cameron / agent integration) — must implement the
    Protocol GUR-102 freezes.
  - GUR-104 (directive lifecycle) — consumes the rows aggregator writes.
  - GUR-106 (dashboard) — consumes both flag rows and directive rows.
- **Damage recipients:**
  - Users whose first dogfooding session produces low-quality
    directives — those directives feed back into Phase 3's agent
    prompts, so an early bad call corrupts agent behavior at the
    consumption side until they prune via the dashboard.
  - LLM cost budget — segment-level detector + per-flag-type
    aggregator is the dominant cost path in Phase 2; a sloppy
    orchestrator (e.g., re-runs aggregation on every flag insert
    instead of post-session) would multiply spend.
  - SQLite write contention — orchestrator runs in background while
    Phase 1 observation may still be writing the next session's
    events; lock contention on `intelligence.db` is the first place
    that surfaces.
