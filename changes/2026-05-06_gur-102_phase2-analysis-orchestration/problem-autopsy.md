# Problem Autopsy: gur-102-phase2-analysis-orchestration

## original_statement

> Build the orchestration layer that runs the full analysis pipeline.
>
> **Tasks (P2-8 to P2-10):**
> - P2-8: Analysis orchestrator — `analysis/orchestrator.py`: session
>   analysis full pipeline (backfill → segmenter → per-segment LLM →
>   session report → cross-session aggregation)
> - P2-9: Behavior flag detector — `analysis/behavior.py`: call LLM with
>   segment-level prompt, parse returned flags, write to DB
> - P2-10: Cross-session aggregator — `analysis/aggregator.py`: Step 1
>   auto-group → Step 2 per flag_type LLM classify → Step 3 merge top N
>   → write to directives table
>
> **Exit criteria:**
> - Full pipeline runs: session end → segments analyzed → flags stored
>   → conventions generated
> - Conventions written to directives table, queryable
>
> **Ref:** SD 5.5.2, 5.5.3, 5.6

## reframed_statement

GUR-102 is the integration seam that turns Phase 2's pure functions
(segmenter, metrics, prompt builders) and persistence (events,
behavior_flags, directives repositories) into an end-to-end pipeline,
gated behind a typed `AnalysisAgent` Protocol that GUR-103 will satisfy.
Three concerns split cleanly:

- **`behavior.py`** — single-segment unit: prompt → agent → validate
  → persist. Stateless beyond DB.
- **`aggregator.py`** — single project unit: read flag rows → group →
  per-group LLM → top-N merge → persist directives. Stateless beyond DB.
- **`orchestrator.py`** — composer: chains backfill / segmenter /
  per-segment behavior / summary / aggregate. Owns transactional
  discipline (what happens when stage 3 of 5 fails).

Phase 2's session-end auto-trigger and the model router live elsewhere
(GUR-103). This issue ships a **callable pipeline** plus the **Protocol
GUR-103 implements**, deliberately not the trigger.

## translation_delta

```yaml
translation_delta:
  - original: "P2-9: Behavior flag detector — call LLM with segment-level prompt"
    reframed: "Behavior detector calls AnalysisAgent.analyze_segment(prompt, SegmentAnalysis); the LLM is invisible to behavior.py"
    delta: |
      Original phrasing reads as if behavior.py owns LLM client wiring.
      That would couple this layer to a specific provider/SDK and
      foreclose GUR-103's router design. Reframing as a Protocol call
      preserves the abstraction barrier the SD already implies
      (summary.py docstring: "The orchestrator owns model invocation").
      The LLM exists; behavior.py just doesn't *see* it.

  - original: "P2-8: orchestrator: session analysis full pipeline (backfill → ...)"
    reframed: "Orchestrator exposes analyze_session(session_id) and aggregate_project(project_id) as two separate callables, both idempotent and partial-progress safe"
    delta: |
      The original phrasing implies one monolithic pipeline. Splitting
      session-scope and project-scope makes each independently testable,
      independently re-runnable on failure, and independently
      schedulable (cross-session aggregation may eventually run on a
      cadence rather than per-session-end). The SD §5.6 line about
      "第一層完成後立即執行第二層" describes a default chaining policy,
      not a coupling requirement.

  - original: "Step 3 merge top N"
    reframed: "Step 3 sorts ALL patterns across ALL flag_types by occurrence_count DESC, takes top `convention_top_n` (default 15 per SD §5.7.2 / §11.x config), writes those as directives"
    delta: |
      Original is silent on (a) the value of N and (b) whether top-N
      applies per-flag-type or globally. SD §5.5.3 Step 3 line 991 says
      "合併所有 flag_type 的 patterns，按 occurrence_count 排序，取
      top N (config: convention_top_n)" — global merge, single N. The
      config default is 15 (SD line 1392). Pinning these values now
      avoids a magic number landing in the orchestrator silently.

  - original: "Full pipeline runs: session end → segments analyzed → flags stored → conventions generated"
    reframed: "GUR-102 ships the callable pipeline; the 'session end →' arrow is a GUR-103 concern (P2-15: trigger mechanism)"
    delta: |
      The exit criterion as written would force GUR-102 to also wire
      session-end events to the orchestrator — but the trigger mechanism
      is explicitly listed as P2-15 inside GUR-103's task list. Treating
      the trigger as out-of-scope here is consistent with the issue
      blocker graph and the task numbering. GUR-102's exit is "given a
      session_id, the pipeline produces the rows"; GUR-103's exit is
      "session end produces a session_id call".
```

## kill_conditions

```yaml
kill_conditions:
  - condition: "GUR-103 design fundamentally rejects a Protocol-shaped agent (e.g., requires async generators or streaming-only)"
    rationale: |
      The Protocol is the load-bearing abstraction here. If it cannot
      survive contact with GUR-103's chosen framework (PydanticAI),
      GUR-102's split between "compose pipeline" and "call agent" is
      wrong. Better to drop the Protocol, ship behavior.py and
      aggregator.py with `# TODO(GUR-103)` callable hooks, and let
      GUR-103 author the wiring directly.

  - condition: "SD §5.5.3 Step 3 top-N config not finalizable at planning gate"
    rationale: |
      An unspecified top-N silently encodes a magic number. If the
      project lead cannot confirm `convention_top_n=15` (or specify
      another value) at the planning gate, the aggregator either ships
      with a hard-coded guess that drifts from intent, or stalls
      waiting for product input. Better to abandon the aggregator from
      this issue and route P2-10 into a separate decision-needed issue.

  - condition: "Cross-session aggregation needs to read flags older than the retention window (365 days, SD §6.x)"
    rationale: |
      If aggregation requires data the retention purger has already
      deleted, the aggregator's correctness is at the mercy of GUR-147
      (retention API) timing. We cannot guarantee top-N stability in
      that case. The kill is to constrain aggregation to a documented
      lookback window aligned with retention, and surface that
      constraint in the directive metadata.

  - condition: "Phase 1 backfill (GUR-99) cannot guarantee event ordering at orchestrator entry"
    rationale: |
      Segmenter requires monotonic sequence_number. If backfill can
      land out-of-order events, the orchestrator's first stage fails
      non-deterministically. GUR-99 should already enforce this
      (verified during planning), but if it does not, GUR-102 must
      either re-sort defensively (silent compensation, anti-pattern)
      or fail loudly. Killing GUR-102 in favor of fixing GUR-99 is
      the correct response, not silent compensation.
```

## damage_recipients

```yaml
damage_recipients:
  - who: "Phase 3 dogfooding users"
    cost: |
      First-pass directives are aggregated from a small flag corpus
      and may be low-quality. They feed back into agent prompts via
      Phase 3 SessionStart injection. A bad early directive degrades
      agent behavior on the consumption side until the user disables
      it via the dashboard (GUR-106). Mitigated by GUR-104 disable
      lifecycle, but the damage window is "first session that picks
      up the new directive" → "user notices and disables" → could be
      hours or days.

  - who: "GUR-103 implementer (Cameron)"
    cost: |
      Whatever Protocol shape GUR-102 freezes constrains GUR-103's
      design space. If we pick wrong (e.g., synchronous-only when
      async is required for streaming, or single-call when batched
      calls are needed for cost), GUR-103 either inherits the
      constraint or has to lobby for a Protocol change — coordination
      cost across two issues.

  - who: "LLM API cost budget"
    cost: |
      Segment-level detector calls scale with session length;
      per-flag-type aggregator calls scale with active flag types
      (currently 7 in BehaviorFlagType). A sloppy orchestrator
      (e.g., re-runs aggregator on every flag insert instead of
      post-session) multiplies spend. Default model is Haiku per
      SD §5.7.1, so per-call cost is small, but unbounded
      session-length × call count = unbounded.

  - who: "SQLite intelligence.db"
    cost: |
      Orchestrator writes happen in background while Phase 1
      observation may be writing the next session's events. WAL
      mode mitigates but does not eliminate write contention.
      First place this surfaces is the GUR-99 e2e suite, which
      runs Phase 1 + Phase 2 in tandem.
```

## observable_done_state

Solved: A test calls `Orchestrator(...).analyze_session(session_id)`
on a stored session of N events; afterward the DB contains M ≥ 0
`BehaviorFlag` rows linked to that session and exactly one stored
session-summary record. A follow-up `aggregate_project(project_id)`
call leaves K ≤ `convention_top_n` `Directive` rows in `status='active'`.
Re-running either call is a no-op modulo idempotent inserts — no
duplicate flags, no duplicate directives.

Not solved: Pipeline raises mid-flight without leaving DB in a
recoverable state; flags written without a corresponding report;
report written referencing flags that were not inserted; aggregator
generates directives that mix patterns across `flag_type` boundaries
or silently drops the `convention_top_n` budget.

The observable difference is a SQL count: post-call,
`SELECT COUNT(*) FROM behavior_flags WHERE session_id=? > 0` (or an
explicit empty-report row), and `SELECT COUNT(*) FROM directives
WHERE project_id=? AND status='active'` ≤ `convention_top_n`.
