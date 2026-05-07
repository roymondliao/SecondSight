# Task 5 — `analysis/orchestrator.py` — Composer

**Depends on:** task-1, task-2, task-3, task-4. **Blocks:** GUR-103.

## Goal

Compose segmenter + behavior detector + summary + aggregator into a
class with three callable entrypoints, the consumer-not-recoverer
verifier, idempotent re-run support, and the zero-flag short-circuit
guard. This is the surface GUR-103 will call from the session-end
trigger.

## Files to create

- `src/secondsight/analysis/orchestrator.py`
- `tests/analysis/test_orchestrator.py`

## Files to modify

- `src/secondsight/analysis/__init__.py` — re-export `Orchestrator`
  and result dataclasses.

## Class surface

```python
class SessionIncompleteError(Exception):
    """Raised when verifier finds the session is missing event rows
    or end-event marker. Consumer-not-recoverer principle (D7)."""


class SessionAlreadyAnalyzedError(Exception):
    """Raised when analyze_session called on a session whose latest
    analysis_runs row is at stage='summary_written' or 'aggregated'.
    Pass force=True to re-analyze (D6)."""


@dataclass(frozen=True)
class AnalyzeSessionResult:
    run_id: str
    session_id: str
    project_id: str
    stage: AnalysisRunStage
    flags_inserted: int
    report_id: str | None  # set when stage reaches 'summary_written'


@dataclass(frozen=True)
class AggregateProjectResult:  # imported from aggregator.py
    ...


@dataclass(frozen=True)
class AnalyzeAndAggregateResult:
    session: AnalyzeSessionResult
    aggregate: AggregateProjectResult | None  # None when short-circuited


class Orchestrator:
    def __init__(
        self,
        events_repo: EventsRepository,
        behavior_flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
        analysis_runs_repo: AnalysisRunsRepository,
        session_reports_repo: SessionReportsRepository,
        agent: AnalysisAgent,
        *,
        segmenter: Segmenter | None = None,
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

## `analyze_session` pipeline

```text
1. _verify_session_complete(session_id)
     → raises SessionIncompleteError if events count == 0
       or end-event marker absent (consumer-not-recoverer, D7).

2. _check_already_analyzed(session_id, force=force)
     → reads latest analysis_runs row for session_id.
       If stage IN ('summary_written', 'aggregated') AND not force:
         raise SessionAlreadyAnalyzedError.

3. run_id = analysis_runs_repo.start_run(project_id, session_id)
     → row inserted at stage='pending' BEFORE any other side effect.

4. segments = segmenter.segment_session(session_id)  # already exists from GUR-100
   advance_stage(run_id, 'segmented')

5. for segment in segments:
       metrics = compute_segment_metrics(segment)
       inserted = await detect_segment_flags(
           segment, metrics,
           session_id=session_id, project_id=project_id,
           behavior_flags_repo=..., agent=...,
       )
       flags_inserted += inserted
   advance_stage(run_id, 'behavior_done', flags_inserted=flags_inserted)

6. segment_analyses = read SegmentAnalysis from collected detector outputs
   summary_prompt = build_summary_prompt(session_id, project_id, segment_analyses)
   summary_output = await agent.summarize_session(summary_prompt)

7. report = SessionReport(
       id=str(uuid.uuid4()),
       session_id=session_id,
       project_id=project_id,
       analysis_run_id=run_id,
       headline=summary_output.headline,
       key_findings=summary_output.key_findings,
       body=summary_output.body,
       created_at=now,
       updated_at=now,
   )
   session_reports_repo.upsert(report)

   ALSO: write filesystem JSON backup at
   {home}/projects/{project_id}/sessions/{session_id}/session_report.json
   per SD §7.2 line 209.

8. advance_stage(run_id, 'summary_written')
   completed_at populated; return AnalyzeSessionResult(...)
```

On any uncaught exception in steps 4-8, the orchestrator catches at
the outer pipeline boundary, calls `analysis_runs_repo.record_failure
(run_id, str(exc))`, and re-raises.

## `analyze_and_aggregate` short-circuit

```python
session_result = await self.analyze_session(session_id, force=force)
if session_result.flags_inserted == 0:
    logger.info(
        "aggregator skipped: zero flags inserted for session %s",
        session_id,
    )
    # DC-8 disclosure: log if directives.updated_at is older than the
    # most-recent retention purge marker (signals stale conventions).
    self._log_if_directives_stale(session_result.project_id)
    return AnalyzeAndAggregateResult(session=session_result, aggregate=None)

aggregate_result = await self.aggregate_project(session_result.project_id)
return AnalyzeAndAggregateResult(session=session_result, aggregate=aggregate_result)
```

## Death tests (write FIRST)

- **DT-5.1 (= DT-1.1) — KILL between stage transitions leaves audit
  row (DC-1).** Mock `detect_segment_flags` to raise immediately
  AFTER the segmenter writes its rows (simulating a SIGKILL).
  Assert the `analysis_runs` row exists at stage='segmented' with
  `completed_at IS NULL`. The next caller can detect the orphan via
  `count_recent_partial`.

- **DT-5.2 (= DT-1.4) — Completed session requires force=True
  (DC-4).** Run `analyze_session` once to completion. Call again
  without `force=True`; assert `SessionAlreadyAnalyzedError` raised
  and zero LLM calls made on the agent fake. Call with
  `force=True`; pipeline runs to completion again, agent fake
  invoked.

- **DT-5.3 (= DT-1.6) — Zero events raises consumer-not-recoverer
  (DC-7).** Empty `events` table for `session_id`. `analyze_session`
  raises `SessionIncompleteError`. The `analysis_runs` row is at
  stage='failed' with `error_message` containing 'consumer-not-
  recoverer' or 'session_incomplete' or similar. The orchestrator
  did NOT attempt to call GUR-99's backfill.

- **DT-5.4 (= DT-1.7) — Summary failure leaves resumable state.**
  Mock `agent.summarize_session` to raise `AnalysisAgentError` after
  behavior flags are committed. Assert: `analysis_runs` row at
  stage='behavior_done' with `error_message` populated AND
  `completed_at` is not null (failed terminal state).
  `behavior_flags` rows persist (idempotent on re-run via ON
  CONFLICT DO NOTHING).

- **DT-5.5 (= DT-1.9) — Short-circuit logs when directives stale
  (DC-8).** Project has active conventions older than a synthetic
  retention purge marker. Run `analyze_and_aggregate` on a session
  producing zero flags. Assert: aggregate is None, log includes
  'aggregator skipped' AND a stale-conventions warning.

- **DT-5.6 (= DG-2.2) — Segment failure halts session.** Mock
  `detect_segment_flags` to raise on segment #2 of a 3-segment
  session. Segment #1's flags persist (atomic per-segment).
  Segment #3 is NEVER processed. `analysis_runs` row at
  stage='failed' with `error_message` naming segment #2.

## Happy-path tests

- **HP-5.A (= HP-3.1) — Full pipeline evidence chain.** Run
  `analyze_session` on a 2-segment session producing 3 flags.
  Verify: events table has the input rows; behavior_flags has 3
  rows; session_reports has 1 row with session_id matching;
  analysis_runs has 1 row at stage='summary_written' with
  completed_at populated; filesystem JSON backup written at
  the SD §7.2 path.

- **HP-5.B (= HP-3.3) — Zero-flag short-circuit (D4 + DC-8).**
  Session produces zero flags; `analyze_and_aggregate` returns
  with `aggregate=None`. Verify aggregator was never invoked
  (no calls on agent.aggregate_flag_type).

- **HP-5.C (= HP-3.4) — Force re-run idempotent.** Analyze a
  session twice, second with `force=True`. Verify: 2 analysis_runs
  rows for that session_id, 1 session_reports row (UPSERT by
  session_id UNIQUE), behavior_flags count unchanged across re-run
  (ID determinism per task-3 design).

- **HP-5.D — `analyze_and_aggregate` chain end-to-end.** Session
  produces 3 flags; chained call invokes `analyze_session` then
  `aggregate_project`; result has both populated; directives table
  has K ≤ 15 rows.

## Scar items to record

- `_verify_session_complete` is verifier-only; **does NOT** recover
  GUR-99 backfill failures. The error message names the upstream
  contract violation explicitly so the operator can fix GUR-99
  rather than mask it here.
- Sequential per-segment detector calls. No concurrent batching in
  v1; GUR-103 may add (the agent Protocol's
  `analyze_segments` batched form is the seam where concurrency
  would land).
- The "behavior_done → summary_written" boundary is the highest-cost
  failure path (LLM call after expensive prior work). Resumability
  via `analysis_runs` stage tracking is essential — without it, a
  retry duplicates segment-level LLM calls.
- Filesystem JSON backup path collision: if `{home}/projects/.../
  session_report.json` already exists from a prior run, overwrite
  unconditionally (the DB UPSERT keys on session_id; filesystem
  follows DB).
- DC-8 stale-conventions logging is informational only — does not
  block the short-circuit. Validation of the warning happens at
  ship-manifest review.
- Outer `try/except` at the pipeline boundary catches `Exception`
  to record `failed` stage. Re-raises immediately. This is the ONE
  place catch-all is acceptable; document why (audit trail
  completion before propagation).
