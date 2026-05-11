# Plan: GUR-107a — Observation API + raw_traces Retention (unblocked subset)

> **Scope conditional on board confirmation `a0a92005`.** This plan
> covers the unblocked subset of GUR-107: P3A-13 (Observation API),
> P3A-12 (CLI cleanup with --dry-run), and the raw_traces side of
> P3A-11 (TTL function for raw_traces + DB events; the
> post-analysis trigger and analysis_ttl path defer to GUR-107b
> which blocks on Phase 2 / GUR-100).
>
> If the board picks (B) hold-atomic, this plan is shelved until
> Phase 2 ships analysis tables.

## 1. Reference contracts

- SD §3.10 — TTL policy (90d raw, 365d analysis), per-project override.
- SD §10.4 — Observation API endpoint shape.
- SD §3.7.5 — `events` schema and indexes.
- Memory `dashboard_api_contracts` — local-only bind, 5s polling with ETag/cursor, single-project MVP, project_id on every endpoint.
- Memory `directive_lifecycle_contract` — orthogonal but read for cross-impact (this scope does not touch directives).

## 2. Decisions

### D1. TTL boundary = `last_event_at`, not `created_at`

A session that started 91 days ago but appended an event 5 minutes
ago is not "expired" — its raw events should not be reaped while it
is observably alive. We compute `last_event_at` per session as
`MAX(timestamp)` over `events`. **Rationale**: see kickoff
silent-failure case 2.

### D2. `project_id` is a required query parameter on every observation endpoint

- `GET /api/sessions?project_id={pid}` — list distinct sessions for a project.
- `GET /api/sessions/{session_id}?project_id={pid}` — session header (counts, first/last timestamp, segment count).
- `GET /api/sessions/{session_id}/segments?project_id={pid}` — list of segments with summary metrics.
- `GET /api/sessions/{session_id}/segments/{segment_index}?project_id={pid}` — full event list for one segment.

`project_id` selects the per-project DB engine via the existing
`ProjectRegistry`. Without it, the API has no DB to read.
**Rationale**: SD §3.7 isolation invariant; kickoff silent-failure
case 3.

### D3. Single retention enumerator drives both real-run and `--dry-run`

```python
def enumerate_expired_sessions(
    repo: EventsRepository,
    *,
    raw_traces_ttl_days: int,
    now: datetime,
) -> list[ExpiredSession]:
    ...
```

`--dry-run` calls this then prints; real run calls this then deletes.
No second enumeration path. **Rationale**: kickoff silent-failure
case 4.

### D4. Cleanup logs the *resolved* TTL with its source

Every cleanup run emits one log line per project:

```
project=abc123 raw_traces_ttl_days=180 source=per_project_config
project=def456 raw_traces_ttl_days=90  source=global_config
project=ghi789 raw_traces_ttl_days=90  source=builtin_default
```

**Rationale**: kickoff silent-failure case 1.

### D5. `--dry-run` is opt-out via explicit absence

`secondsight cleanup` actually deletes; `secondsight cleanup --dry-run`
only reports. The default is destructive because the operator is
typing `cleanup` knowing what it does. The non-destructive path
must be explicit. (Alternative considered: default to dry-run with
`--apply` flag. Rejected because it makes scripted invocations
verbose without adding safety — the typed command name is already
the safety signal.)

### D6. Observation API does **not** stream

Every endpoint returns a bounded JSON response. Pagination via
`limit`/`offset` query params on the listing endpoints
(`GET /sessions` and `GET /sessions/{id}/segments`) with default
limit 100, max 500. **Rationale**: 5s polling cadence (memory
contract) plus dashboard MVP single-project means the worst-case
session list is hundreds, not millions. Streaming adds frontend
complexity for no payoff at MVP traffic.

### D8. CLI bypasses ProjectRegistry; API uses it

The Observation API runs inside the server and resolves project
resources via
`await request.app.state.server_state.registry.get(project_id)`
— same pattern as `api/hooks.py`.

The `cleanup` CLI runs as a one-shot subprocess with no event loop;
it builds per-project resources synchronously, mirroring
`ProjectRegistry._build_resources`. SQLite WAL mode (already
configured by `DBEngine`) makes concurrent CLI cleanup vs. server
ingest safe; cleanup's `DELETE FROM events` will block briefly
behind in-flight server writes, which is acceptable.

The Observation router module docstring must explicitly call out
this asymmetry so future contributors don't import the CLI's FS
walk as a generic project-enumeration pattern.

### D7. Pure SQL paths only — no per-event JSON parse on listing endpoints

Listing endpoints rely on table columns + indexes (`session_id`,
`segment_index`, `timestamp`, `event_type`). The `data` JSON column
is opened only on the segment-detail endpoint
(`GET /sessions/{id}/segments/{idx}`), where the dashboard needs
the full event payload anyway.

## 3. Components

### 3.1 New: `secondsight.storage.retention` (module)

> **Verification correction (C1):** RetentionConfig is the *first*
> config consumer in the codebase — no `config.toml` infrastructure
> exists today (`grep -r config.toml src/` returns zero). Task-A1
> must DEFINE the file format, not just consume an existing loader.
> Python 3.14 is pinned, so stdlib `tomllib` is used — no new
> dependency.

- `class RetentionConfig` — TOML loader; per-project override on top of global.
  Missing files return built-in default (90d, source=`builtin_default`)
  and never raise — otherwise every fresh install fails on first
  cleanup. **DC-6b** enforces this.
- `class RawTracesTTL` — pure computation:
  - `enumerate_expired_sessions(repo, *, raw_traces_ttl_days, now) -> list[ExpiredSession]`
  - `compute_resolved_ttl(global_cfg, project_cfg) -> tuple[int, str]` (returns `(days, source)`)
- `class RawTracesPurger` — destructive side; takes the enumeration result and:
  - Deletes filesystem dir `sessions/{session_id}/events/` via `RawTraceStore` helper.
  - `DELETE FROM events WHERE session_id IN (...)`.
  - Both wrapped in a single transactional unit that surrenders cleanly on partial failure (FS first, DB second; if DB delete fails the FS files are already gone — log loudly and continue).

### 3.2 New: `secondsight.api.observation` (module + router)

- `GET /api/sessions` — `ListSessionsResponse { sessions: [SessionSummary], next_cursor }`.
- `GET /api/sessions/{session_id}` — `SessionDetail`.
- `GET /api/sessions/{session_id}/segments` — `ListSegmentsResponse { segments: [SegmentSummary] }`.
- `GET /api/sessions/{session_id}/segments/{segment_index}` — `SegmentDetail` with full `events: [Event]`.

Mounted onto the same `app` produced by `api.server.create_app`.
ETag header on listing endpoints derived from
`max(events.timestamp) WHERE session_id = ?` (or session list
analogue) so dashboard polling can short-circuit on 304.

### 3.3 New: `secondsight.cli.cleanup` (Typer subcommand)

> **Verification correction (C2):** ProjectRegistry is async and
> server-bound; it cannot be used by the synchronous one-shot CLI.
> The precedent for project enumeration in CLI is
> `cli/sync.py:170-176` — FS walk over `home/"projects"`.

- `secondsight cleanup [--project-id PID] [--dry-run]`
- Reuses `cli/_home.py:secondsight_home()` for home resolution and
  the `cli/sync.py:_select_project_ids` enumeration pattern (FS
  walk over `home/"projects"`, sorted directory names).
- Builds per-project `DBEngine` + `EventsRepository` +
  `RawTraceStore` synchronously per project, mirroring
  `ProjectRegistry._build_resources` — see D8.
- Default: walks every project under home; with `--project-id`,
  scopes to one.

### 3.4 Touched: `secondsight.api.server.create_app`

Add `app.include_router(observation_router)` next to existing
`app.include_router(hooks_router)`. No lifespan changes.

### 3.5 Touched: `secondsight.cli.app`

Register the `cleanup` subcommand.

## 4. Wave / dependency graph

```
Wave 1 (independent, parallelisable):
  task-A1: RetentionConfig loader + tests
  task-A2: enumerate_expired_sessions + tests (pure, no side effects)
  task-A3: ListSessionsResponse / SessionDetail / SegmentSummary / SegmentDetail Pydantic schemas + tests

Wave 2 (depends on Wave 1):
  task-A4: RawTracesPurger wired to RawTraceStore + EventsRepository (side-effecting)
  task-A5: Observation API router (mounts the schemas, queries the repo)

Wave 3 (depends on Wave 2):
  task-A6: secondsight cleanup CLI subcommand (orchestrates A1+A2+A4)
  task-A7: ETag/cursor wiring on listing endpoints (depends on A5 baseline behaviour)
```

## 5. Death tests (samsara: write before unit tests)

Each task ships its death tests first. The non-negotiable death
cases this scope must cover:

- **DC-1**: `enumerate_expired_sessions` returns empty list for a
  project with zero events (no spurious deletions).
- **DC-2**: `enumerate_expired_sessions` skips sessions whose
  `last_event_at` is newer than TTL even if `created_at` is older
  (boundary correctness — see D1).
- **DC-3**: `--dry-run` calling the same enumerator path as real
  run produces an identical session set (no enumeration drift —
  see D3).
- **DC-4**: Observation API rejects requests without `project_id`
  with a 422; never returns data scoped to "first project found"
  (no cross-project leak — see D2).
- **DC-5**: `RawTracesPurger` partial failure (DB delete throws
  after FS files removed) leaves a structured ERROR log with the
  affected session_id and surfaces a non-zero exit code from the
  CLI (silent FS/DB drift is unacceptable).
- **DC-6**: `RetentionConfig` with malformed per-project TOML
  raises `RetentionConfigError` rather than silently falling back
  to global (see D4 / silent-failure case 1).
- **DC-6b**: `RetentionConfig.load(home, project_id)` with NO
  config files present (neither global nor per-project) returns
  the built-in default (90 days, source=`builtin_default`) and
  does NOT raise. (Verification finding C1: this is the fresh-
  install path; failing here would brick every clean install.)
- **DC-7**: ETag computation on `GET /sessions` is stable across
  identical underlying state and changes only when a new event is
  appended.

## 6. Out of scope (explicit)

- `analysis_ttl_days` enforcement — defers to GUR-107b (Phase 2 dependency).
- Post-analysis cleanup trigger — defers to GUR-107b.
- Behavior flags / directives endpoints — owned by GUR-104.
- Cross-project listing endpoint — single-project MVP per dashboard memory.
- WebSocket / SSE streaming — D6.
- `secondsight cleanup --before TIMESTAMP` ad-hoc range — escape
  hatch deferred unless asked.

## 7. Risk register

| Risk | Likelihood | Mitigation |
|---|---|---|
| `events.timestamp` skew between sessions confuses ETag | low | ETag is per-session, not global; per-session timestamp ordering already used by Phase 1 |
| `--dry-run` enumerator and real-run enumerator drift | medium | DC-3 enforces identity; single function called by both paths |
| Cleanup races concurrent ingest on same session | medium | D1 (`last_event_at`) makes the window vanishingly narrow at 90d boundary; if it bites, follow-up issue gates cleanup on `now - last_event_at > 24h` even after TTL |
| Pagination cursor stability under writes | medium | offset/limit explicit cursor, not row-id; document trade-off in Observation API tests |
| FastAPI `app.state.server_state` access pattern | low | Mirrors hooks router; copy proven pattern from `api/hooks.py` |

## 8. What "done" looks like (preview of acceptance criteria)

See `acceptance.md` for the full list. Headline:

- `pytest tests/storage/test_retention.py tests/api/test_observation.py tests/cli/test_cleanup.py` green.
- Full suite stays at parity (no regression on phase 1 tests).
- `secondsight cleanup --dry-run` against a fixture project with one
  90+day-old session reports it; without `--dry-run`, the next
  invocation reports nothing.
- `curl -s "$URL/api/sessions?project_id=X"` returns a JSON list
  whose ETag matches the next call's `If-None-Match: <etag>` →
  304.

## 9. Next action

Immediately after this plan lands, I file `acceptance.md` and the
`tasks/` directory. Confirmation gate `a0a92005` controls whether
implementation starts on the next heartbeat or whether this plan is
shelved.
