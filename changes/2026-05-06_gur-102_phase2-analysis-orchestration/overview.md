# Overview — GUR-102 Phase 2 Analysis Orchestration

Shared context for all 5 tasks. Read this once before starting any task.

## What this feature is

The integration seam that turns Phase 2's pure functions and persistence
into a running analysis pipeline, gated behind a typed `AnalysisAgent`
Protocol that GUR-103 will implement.

## What already exists (do not rebuild)

| Layer | Module | Purpose |
|---|---|---|
| Schemas | `secondsight.analysis.schemas` | `BehaviorFlagType`, `BehaviorFlag`, `Directive`, `SegmentAnalysis`, `SegmentData`, `SegmentMetrics`, `ToolUseSpan`, `FLAG_DEFINITIONS` |
| Schemas | `secondsight.analysis.prompts.aggregate` | `FlagSummary`, `AggregatePattern`, `AggregateOutput` |
| Schemas | `secondsight.analysis.prompts.summary` | `SummaryOutput` |
| Pure | `secondsight.analysis.segmenter` | `Segmenter.segment_session(session_id)` returns list[SegmentData] |
| Pure | `secondsight.analysis.metrics` | `compute_segment_metrics(segment)` |
| Pure | `secondsight.analysis.prompts.behavior` | `build_segment_prompt(segment, metrics)` |
| Pure | `secondsight.analysis.prompts.aggregate` | `build_aggregate_prompt(flag_type, flags)` |
| Pure | `secondsight.analysis.prompts.summary` | `build_summary_prompt(session_id, project_id, segments)` |
| Storage | `secondsight.storage.events_repository.EventsRepository` | `get_session_events`, `get_segment_events` |
| Storage | `secondsight.storage.behavior_flags_repository.BehaviorFlagsRepository` | `insert`, `insert_many`, `get_session_flags`, `get_project_flags_by_type`, `count_by_type` |
| Storage | `secondsight.storage.directives_repository.DirectivesRepository` | `insert`, `get_active_conventions`, `get_by_id`, `update_status` |

## What this feature adds

### New tables (task-1)
- `analysis_runs` — pipeline-progress audit trail
- `session_reports` — session-summary artifact persistence

### Migrated table (task-1)
- `directives` — adds `identity_key TEXT` + `UNIQUE(project_id, identity_key)`

### New Protocol (task-2)
- `AnalysisAgent` Protocol + `AnalysisAgentError` in `analysis/agent.py`

### New modules (task-3, task-4, task-5)
- `analysis/behavior.py` — segment-level detector
- `analysis/aggregator.py` — cross-session aggregator
- `analysis/orchestrator.py` — composer

## Death cases shared across tasks

These are the 8 silent-failure surfaces this PR closes. Cite the
DC-N number in test names so the death-case-to-test mapping is
discoverable.

| DC | Surface | Closed by |
|---|---|---|
| DC-1 | KILL between stage transitions | task-1 (entry-time `start_run`) + task-5 (resume logic) |
| DC-2 | Detector partial commit on `_guard` mid-batch failure | task-3 (validate-before-insert) |
| DC-3 | Aggregator silently truncates ties at `top_n` | task-4 (deterministic tie-break) |
| DC-4 | Re-run silently re-LLMs completed session | task-5 (`force=True` guard) |
| DC-5 | Aggregator reads stale flags after retention purge | task-4 (`flags_read` in result) |
| DC-6 | Identity-key collision across distinct emerged patterns | task-4 (hash from emerged pattern, not input) |
| DC-7 | Consumer-not-recoverer violation runs vacuously | task-5 (`_verify_session_complete`) |
| DC-8 | Short-circuit elides aggregation with stale conventions | task-5 (stale-conventions logging) |

## Architecture decisions (D1–D8) — read pre-thinking + plan for full reasoning

- **D1.** AnalysisAgent Protocol body in `agent.py` verbatim per plan §3.4.
- **D2.** Two distinct tables: `analysis_runs` (run identity) ≠ `session_reports` (artifact identity).
- **D3.** `directives.identity_key = sha256(flag_type + "|" + sorted(repr_session_ids).join(","))`.
- **D4.** Three orchestrator callables; chained wrapper short-circuits on zero new flags.
- **D5.** `DEFAULT_CONVENTION_TOP_N = 15` hard-coded constant; no AnalysisConfig in v1.
- **D6.** Re-run completed session requires `force=True`; otherwise raises `SessionAlreadyAnalyzedError`.
- **D7.** Backfill stage = verifier (consumer-not-recoverer principle); raises `SessionIncompleteError` on contract violation.
- **D8.** disable-on-arrival ≤30% across first 20 sessions = deferred Phase 2 ship gate (blocks GUR-104, not GUR-102).

## Conventions all tasks follow

- **Test ordering:** death tests written and made to fail BEFORE happy-path tests.
- **Naming:** test names cite DT-N or HP-N IDs from acceptance.yaml.
- **Async-first:** `analysis/orchestrator.py`, `analysis/behavior.py`, `analysis/aggregator.py` are `async`. Tests use `pytest-asyncio` (already installed; see GUR-99 conventions).
- **No global state:** every module takes its repository + agent dependencies as constructor/function arguments.
- **Idempotent inserts:** behavior_flags use ON CONFLICT DO NOTHING on `id`. session_reports UPSERT on `session_id` UNIQUE. directives UPSERT on `(project_id, identity_key)`.
- **Errors propagate:** no catch-all `except` blocks. `AnalysisAgentError`, `SessionIncompleteError`, `SessionAlreadyAnalyzedError`, `ValidationError` propagate to caller; orchestrator only catches at outer pipeline boundary to record `analysis_runs.stage='failed'` and re-raise.

## What is NOT in scope

- LLM router / model selection / fallback — GUR-103.
- PydanticAI agent loop — GUR-103.
- Session-end auto-trigger / `secondsight analyze` CLI — GUR-103 task P2-15.
- Directive lifecycle transitions beyond initial insert — GUR-104.
- Dashboard / API endpoints over flags + directives — GUR-106.
- AnalysisConfig plumbing — deferred until 2nd config knob exists.
