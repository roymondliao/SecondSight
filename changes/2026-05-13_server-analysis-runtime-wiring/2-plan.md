# Plan: Server Analysis Runtime Wiring

**Inputs:** investigation from live `~/.secondsight/projects/SecondSight` state, current `src/secondsight` server/runtime code, and prior Phase 2/3 planning artifacts.

## 1. Feature description

Repair the server-side analysis lifecycle so that:

1. a successfully ingested `session_end` event triggers analysis dispatch automatically, and
2. the stale-session sweeper performs real timeout recovery instead of emitting warning-only reminders.

No new DB tables are required. The fix is architectural wiring inside the existing server/runtime path, not a schema change and not a dashboard-only patch.

## 2. Confirmed root cause

The observed stale-warning loop is caused by missing server-side orchestration wiring, not by missing `session_end` events.

Confirmed facts from the live project DB:

- Session `38f3d30b-afab-4116-92ed-21f391088676` already has `session_end` persisted in `events`.
- The project has zero rows in `analysis_runs`, `session_reports`, `behavior_flags`, and `directives`.
- `Trigger.register_pipeline_callback()` exists, but has no call site in the server path.
- `_ServerSweepCoordinator` only warns on stale sessions and never dispatches analysis.

Implication:

- observation ingestion is functioning
- analysis runtime ownership exists only in the CLI/in-process path
- the server can accumulate complete sessions forever without ever creating an analysis run

## 3. Ratified decisions

- **D1.** The fix lives at the server runtime composition layer, not in the adapter payload layer. `session_end` data is already present and correct.
- **D2.** Event-driven dispatch and timeout dispatch must share the same per-project `Trigger` instance so all idempotency and lock semantics remain single-source.
- **D3.** The hook handler latency contract remains unchanged. Hook routes still return after scheduling background ingest; analysis dispatch must stay downstream of successful DB write.
- **D4.** `ProjectRegistry` (or an adjacent project-runtime builder it owns) becomes the canonical place to materialize server-side analysis runtime for a project.
- **D5.** The server sweeper must become an execution path, not merely an operator hint. Logging remains, but only as evidence of dispatch/skip/failure outcomes.
- **D6.** CLI and server analysis wiring should converge structurally. The server must not grow an ad hoc second copy of orchestrator assembly logic.

## 4. Death cases

- **DC-1: Session completed, analysis never starts.**
  - Trigger: `session_end` is ingested successfully, but no pipeline callback is registered.
  - Lie: "session lifecycle is complete because `session_end` exists."
  - Truth: the project never produces `analysis_runs`, so the dashboard and directive layers remain permanently empty.
  - Detection: `events` contains `session_end`, but `analysis_runs` stays empty after a bounded wait.

- **DC-2: Stale warning masquerades as recovery.**
  - Trigger: timeout sweeper finds a stale session and emits warning-only logs.
  - Lie: "the system noticed the stale session, so recovery is happening."
  - Truth: no dispatch occurs; the same session warns forever every sweep interval.
  - Detection: repeated warnings for the same `session_id` with unchanged `last_event_ts`, while `analysis_runs` remains empty.

- **DC-3: Dual dispatch race doubles analysis cost.**
  - Trigger: event-driven dispatch and timeout sweeper both target the same session around the same time.
  - Lie: "both paths improve reliability."
  - Truth: without shared trigger ownership, duplicate background tasks can run, doubling LLM cost.
  - Detection: more than one `analysis_runs` row or more than one scheduled analysis task for the same session without `force=True`.

- **DC-4: Server-only runtime drift from CLI runtime.**
  - Trigger: server assembles analysis runtime differently from CLI `_build_orchestrator()`.
  - Lie: "both paths support analysis."
  - Truth: one path succeeds while the other silently lacks required repos/config/model wiring.
  - Detection: the same session is analyzable via CLI but cannot be auto-analyzed by the server.

## 5. Scope

In scope:

- server-side per-project analysis runtime materialization
- pipeline callback registration for `session_end`
- sweeper upgrade from warning-only to real timeout dispatch
- logs and tests that make dispatch outcomes observable

Out of scope:

- changing the `session_end` payload schema
- dashboard UX changes
- adding new DB tables
- changing the manual CLI user contract beyond shared runtime reuse

## 6. Module headlines

- **MH-1: Per-project analysis runtime composition**
  - Add a project-scoped runtime object or equivalent server-owned structure that includes the analysis repos, orchestrator, and trigger.
  - Primary files:
    - `src/secondsight/api/registry.py`
    - optionally a new helper module if composition becomes too large for the registry

- **MH-2: Event-driven dispatch wiring**
  - Register `Trigger.register_pipeline_callback()` against each project's `ObservationPipeline` during runtime materialization.
  - Primary files:
    - `src/secondsight/api/registry.py`
    - `src/secondsight/sdk/trigger.py`

- **MH-3: Timeout recovery dispatch**
  - Replace warning-only stale handling with `Trigger.dispatch(source="timeout")`, while preserving explicit skip/failure logging.
  - Primary files:
    - `src/secondsight/api/server.py`
    - `src/secondsight/sdk/trigger.py`

- **MH-4: Verification and observability**
  - Add tests proving event-driven dispatch, timeout dispatch, and race-safe dedup.
  - Add logs that distinguish `dispatched`, `already-analyzed`, `another-run-in-flight`, and actual failures.
  - Primary files:
    - `tests/...`
    - touched runtime modules above

## 7. File map

- `src/secondsight/api/registry.py`
  - Extend `ProjectResources` or introduce adjacent runtime ownership for analysis components.
  - Ensure runtime is created once per project and reused.

- `src/secondsight/api/server.py`
  - Rework `_ServerSweepCoordinator` so it retrieves and uses the shared per-project trigger/runtime.
  - Replace warning-only stale handling with dispatch + outcome-aware logging.

- `src/secondsight/sdk/trigger.py`
  - Reuse existing trigger semantics; no new dedup mechanism.
  - Potentially expose small helpers needed by server runtime or tests, but do not split idempotency logic.

- `src/secondsight/cli/analyze.py`
  - Optional shared-builder refactor if needed to prevent server/CLI drift.

- `tests/`
  - Add or extend tests for project runtime materialization, callback wiring, sweeper dispatch, and race handling.

## 8. Implementation strategy

### Step 1: Create a reusable project analysis runtime

- Build a project-scoped runtime that owns:
  - `AnalysisRunsRepository`
  - `BehaviorFlagsRepository`
  - `DirectivesRepository`
  - `SessionReportsRepository`
  - `Orchestrator`
  - `Trigger`
- Ensure phase-2 schemas are created as needed in that runtime path.
- Prefer a shared builder over copying CLI assembly inline into `server.py`.

### Step 2: Wire event-driven dispatch

- On project materialization, call `trigger.register_pipeline_callback(pipeline)`.
- The callback must remain post-ingest, after DB write succeeds.
- The callback must not block hook response latency.

### Step 3: Wire timeout recovery

- Modify `_ServerSweepCoordinator` to retrieve the shared trigger for each materialized project.
- For each stale candidate lacking a terminal analysis run, call `dispatch(project_id, session_id, source="timeout")`.
- Log the returned `DispatchResult` meaningfully instead of always warning.

### Step 4: Harden observability

- Log runtime creation success/failure per project.
- Log callback registration once per project.
- Log timeout dispatch outcome with explicit reasons.
- Preserve operator-facing warnings only for actionable failure conditions, not as a substitute for the action itself.

## 9. Acceptance strategy

Primary evidence chain:

1. ingest `session_end`
2. `events` row exists
3. event-driven dispatch occurs
4. `analysis_runs` row appears
5. later phases may succeed or fail, but lifecycle is now visible and auditable

Timeout recovery evidence chain:

1. session becomes stale by last-event timestamp
2. sweeper detects it
3. sweeper dispatches via shared trigger
4. `analysis_runs` row appears or skip reason is explicit

## 10. Risks and controls

- **Risk:** server path duplicates CLI builder logic and drifts.
  - **Control:** centralize runtime assembly behind one server/CLI-shared builder or a clearly-owned factory.

- **Risk:** event path and timeout path each instantiate separate triggers.
  - **Control:** single trigger per project, cached with the project runtime.

- **Risk:** analysis runtime creation introduces startup or per-project latency spikes.
  - **Control:** keep materialization lazy per project, mirroring current registry behavior.

- **Risk:** tests rely on real model/provider setup.
  - **Control:** use fake/stub agent/orchestrator in server/runtime wiring tests; reserve real-model behavior for dedicated analysis integration coverage.
