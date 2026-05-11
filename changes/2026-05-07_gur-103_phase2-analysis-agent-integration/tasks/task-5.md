# Task 5 (P2-15): Trigger layer — sdk/trigger.py + cli/analyze.py + pipeline subscription

## Context

Read: `overview.md`, `2-plan.md` §2 (D2, D3, D9, D10, D14), §3
(DC-4 through DC-7), `2-pre-thinking.md` §F.

Three trigger paths converge on a single `Trigger.dispatch()`:
event-driven (post-pipeline.ingest of SESSION_END), timeout-based
(periodic sweeper), manual (`secondsight analyze` CLI). All use
the existing `analysis_runs` table via
`get_latest_for_session()` for dedup. Per-session asyncio lock
prevents trigger-race duplicate dispatch.

Hook handler in `api/hooks.py` is **not modified** (D3). The
subscription point is `observation/pipeline.py` post-DB-write.

This task depends on tasks 1–4.

## Files

- Create: `src/secondsight/sdk/trigger.py`
- Create: `src/secondsight/cli/analyze.py`
- Modify: `src/secondsight/observation/pipeline.py` — add 1
  callback registration for `EventType.SESSION_END` post-write
- Modify: `src/secondsight/cli/app.py` — register the `analyze`
  subcommand
- Modify: `src/secondsight/api/server.py` — start `Sweeper` task
  in lifespan startup; cancel + await on shutdown
- Test: `tests/sdk/test_trigger.py`
- Test: `tests/cli/test_analyze.py`

## Death Test Requirements

- **DT-5.1 concurrent dispatch (DC-4).** Two concurrent calls
  to `dispatch(project_id, session_id, source="event")` with the
  same session_id, no prior `analysis_runs` row. Use a fake
  orchestrator whose `analyze_and_aggregate` records an
  invocation count. Assert exactly ONE invocation.
- **DT-5.2 blocking I/O wrapped in to_thread (DC-5).** Stress
  test: under N=10 concurrent
  `pipeline.ingest(non_session_end_event)` calls, dispatch a
  session_end event whose analysis pipeline calls
  `read_project_file` on a 256 KiB file. Measure the p95 of the
  10 hook handler latencies. Assert it stays within 10% of a
  baseline measurement taken without the analysis dispatch.
  (Pragmatic: the FS read in tools.py uses `asyncio.to_thread`,
  so the event loop stays unblocked; this test verifies the
  property end-to-end.)
- **DT-5.3 sweeper uses last_event_ts (DC-6).** Construct a
  session whose `last_event_ts` is 5 minutes ago (within
  timeout) but `events` table contains no `session_end`
  event row. Run `Sweeper.sweep_stale_sessions()`. Assert
  `dispatch` is NOT called. (If sweeper used "no session_end"
  as primary trigger, it would dispatch.)
- **DT-5.4 already-analyzed CLI exits code 2 (DC-7).** Pre-seed
  `analysis_runs` with `stage='summary_written'` for a session.
  Run `secondsight analyze --session SESSION_ID` (no `--force`).
  Assert exit code is 2; STDERR contains
  `"Skipped: session ... already analyzed at ..."` and
  `"pass --force to re-run"`.

## Implementation Steps

- [ ] Step 1: Write death tests (4 above).
- [ ] Step 2: Run — verify fail.
- [ ] Step 3: Write degradation + happy-path tests:
      - DG-1.2 in-process fallback when API server not reachable
      - HP-1.1 event-driven full evidence chain (smoke test:
        feed an events fixture, fire SESSION_END, observe
        `analysis_runs.stage='aggregated'` after the dispatch
        task completes)
      - HP-5.5 happy-path event-driven dispatch records run_id
      - HP-5.6 sweeper happy-path on a session whose
        `last_event_ts` is 35 min old (timeout 30 min)
- [ ] Step 4: Run — verify fail.
- [ ] Step 5: Implement:
      - `class LockRegistry`:
        - `_locks: weakref.WeakValueDictionary[str,
          asyncio.Lock]`
        - `acquire(session_id) -> AsyncContextManager` that
          tries non-blocking; returns sentinel on contention so
          callers can return `dispatched=False`.
      - `@dataclass class DispatchResult` with `dispatched:
        bool`, `reason: str`, `run_id: str | None`.
      - `class Trigger`:
        - `__init__(orchestrator, analysis_runs_repo,
          events_repo, lock_registry,
          trigger_lock_seconds=30)`.
        - `async dispatch(project_id, session_id, *, source,
          force=False) -> DispatchResult`:
          - Try `lock_registry.acquire(session_id)` non-blocking;
            on contention, return `(False, "lock-held")`.
          - Inside the lock:
            - `latest = analysis_runs_repo.
              get_latest_for_session(session_id)`.
            - If `latest.stage in {'summary_written',
              'aggregated'}` and not force → return
              `(False, "already-analyzed",
              run_id=latest.id)`.
            - If latest is non-terminal and `(now -
              latest.updated_at) < trigger_lock_seconds`,
              return `(False, "another-run-in-flight")`.
          - Schedule `asyncio.create_task(orchestrator.
            analyze_and_aggregate(session_id, force=force))`.
          - Log INFO with `source`, `session_id`, `dispatched=True`.
          - Return `(True, "dispatched")`.
      - `class Sweeper`:
        - `__init__(trigger, events_repo, analysis_runs_repo,
          interval_seconds=60, session_timeout_minutes=30)`.
        - `async run()`: a while-True loop with
          `asyncio.sleep(interval_seconds)`. Each tick:
          - Query: sessions where `last_event_ts < now() -
            timeout` AND `latest analysis_runs stage NOT IN
            (summary_written, aggregated)`.
          - For each, call
            `await self.trigger.dispatch(project_id,
            session_id, source="timeout")`.
          - Catch + log every per-session error; never let
            one session's failure stop the sweep loop.
        - `async cancel()`: idempotent task cancel + await.
      - `observation/pipeline.py` modification:
        - In `pipeline.ingest()`, after the DB write succeeds
          and the event is `EventType.SESSION_END`, schedule
          `asyncio.create_task(trigger.dispatch(project_id,
          session_id, source="event"))`. Use a module-level
          weak ref or DI to the trigger; pipeline owns the
          subscription.
      - `cli/analyze.py` (Typer subcommand):
        - Args: `--session ID` (optional), `--project P`
          (optional, default from project_config), `--force`.
        - Default flow: try `httpx.post(server_url +
          "/api/analyze", json={...})`; on `ConnectError`,
          log INFO + run in-process.
        - In-process: load project_config, construct
          dependencies (events_repo, analysis_runs_repo,
          orchestrator with PydanticAIAnalysisAgent), run
          `Trigger.dispatch(...)`, await the dispatched task,
          stream stage transitions to STDOUT.
        - On `dispatched=False reason="already-analyzed"`,
          print message + exit code 2.
        - On orchestrator failure → exit code 1.
        - On success → exit code 0.
      - `api/server.py` lifespan:
        - On startup, construct Sweeper, store on app.state,
          schedule `asyncio.create_task(sweeper.run())`.
        - On shutdown, `await sweeper.cancel()` with timeout.
      - `cli/app.py`: register `analyze` typer subcommand.
- [ ] Step 6: Run — verify pass.
- [ ] Step 7: Write scar report.
- [ ] Step 8: Commit.

## Expected Scar Report Items

- `LockRegistry` uses `weakref.WeakValueDictionary` so completed
  sessions release. Verify a long-running session that holds a
  lock + GC pressure does not free the lock prematurely (Python
  weakref semantics: the dict drops keys when the *value* —
  the Lock object — is otherwise unreferenced; the
  context manager keeps a strong ref during its scope, so
  premature GC is impossible by construction, but verify with a
  test).
- Sweeper catches exceptions per-session; one bad session must
  not poison the loop. Document the catch-and-log pattern in
  the implementation comment.
- Pipeline subscription is the smallest possible footprint: 1
  callback registration after the DB write. If the subscription
  mechanism doesn't exist, this task adds it as a 5-line
  `_post_ingest_callbacks: list[Callable]` on the pipeline
  class — NOT a refactor.
- CLI defaults to in-process when the API server is down. The
  alternative (always require `secondsight serve`) would block
  manual analyses on a server-less install. The chosen default
  surfaces the path taken in STDERR so it's never silent
  (DG-1.2).
- The trigger layer NEVER inserts `analysis_runs` rows. Only
  the orchestrator's `start_run()` does (D14). Verify the impl
  doesn't accidentally add a status-tracking row in the
  trigger.
- New API endpoint (`POST /api/analyze`) is OPTIONAL — the
  default path goes in-process for the CLI. Skip the new
  endpoint in v1; document the future addition path.

## Acceptance Criteria

Covers from `acceptance.yaml`:
- DT-5.1 (DC-4), DT-5.2 (DC-5), DT-5.3 (DC-6), DT-5.4 (DC-7)
- DG-1.2 (in-process fallback)
- HP-1.1 (full evidence chain), HP-5.5, HP-5.6
