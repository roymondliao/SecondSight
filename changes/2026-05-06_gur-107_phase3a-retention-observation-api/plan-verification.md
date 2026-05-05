# Plan Verification — GUR-107a against current code reality

> Read-side verification of `2-plan.md` assumptions against the live
> codebase before any implementation begins. Heartbeat-bounded;
> three concrete corrections found.

## Verified assumptions (no change needed)

| Plan reference | Assertion | Verified against |
|---|---|---|
| §3.1 | `events` table holds `(session_id, sequence_number, segment_index, timestamp, event_type)` with required indexes | `src/secondsight/storage/events_table.py` — confirmed columns + `idx_events_session_seq`, `idx_events_segment` indexes |
| §3.1 | `RawTraceStore` already path-safe with regex on `session_id` and project-root containment | `src/secondsight/storage/raw_trace_store.py:30-87` — `_SAFE_SESSION_ID` regex + `event_path()` re-resolves and verifies parent containment |
| §3.2 | Observation API can mount onto existing `create_app()` next to hooks router | `src/secondsight/api/server.py:264` — `app.include_router(hooks_router)` is the precedent line; no lifespan changes required |
| §3.2 | Per-project resources accessible via `request.app.state.server_state.registry` | `src/secondsight/api/server.py:88-100` — `AppState.registry: ProjectRegistry` exposed |
| §3.3 | Typer subcommand pattern is one-file-per-command, mounted in `cli/app.py` | `src/secondsight/cli/app.py:44-47` — `init`, `serve`, `status`, `sync` follow this exact shape |
| D1 | TTL boundary on `last_event_at` derivable from `events.timestamp` | `EventsRepository` exposes `get_session_events()`; need a new `get_last_event_timestamp(session_id)` aggregate, but it's a one-line `SELECT MAX(timestamp) WHERE session_id = ?` against an existing index |

## Corrections to the plan

### C1. RetentionConfig must DEFINE the config file shape, not just consume an existing loader

**Finding:** `grep -r 'config.toml\|tomllib' src/` returns zero matches.
The plan §3.1 said `RetentionConfig` is a "TOML loader". Reality:
SecondSight has **no `config.toml` infrastructure today** — neither
global at `~/.secondsight/config.toml` nor per-project at
`~/.secondsight/projects/{pid}/config.toml`. RetentionConfig is the
**first** config consumer in the codebase.

**Impact on plan:**
- task-A1 scope expands: it must (a) define the file format, (b)
  resolve precedence (per-project → global → built-in default), and
  (c) gracefully handle the all-three-files-absent case (use
  built-in default of 90, log `source=builtin_default`).
- DC-6 (malformed TOML raises `RetentionConfigError`) still holds,
  but a sibling case is now needed: **DC-6b: missing config files
  return built-in default — never raise** (otherwise every fresh
  install fails on first cleanup).
- Python is pinned to **3.14** (verified via `.python-version` and
  `pyproject.toml:requires-python = ">=3.14"`), so stdlib
  `tomllib` is available — no new dependency needed.

### C2. CLI cleanup must NOT use `ProjectRegistry`

**Finding:** `ProjectRegistry` is async, lifecycle-bound to the
server process via `LazyCacheWithLocking`, and its `_materialise`
runs in `asyncio.to_thread`. The `secondsight cleanup` CLI is a
short-lived synchronous subprocess — it does not have an event
loop or a long-running daemon to register against.

The plan §3.3 implied "Reuses … `serve.py` for project
enumeration." The actual precedent is **`cli/sync.py:170-176`**:

```python
def _select_project_ids(home: Path, requested: str) -> list[str]:
    if requested:
        return [requested]
    projects_dir = home / "projects"
    if not projects_dir.is_dir():
        return []
    return sorted(child.name for child in projects_dir.iterdir() if child.is_dir())
```

Cleanup must mirror this pattern: walk `home/"projects"` from the
filesystem, then build `DBEngine` + `EventsRepository` +
`RawTraceStore` directly per project (mirroring
`ProjectRegistry._build_resources` synchronously), not through the
async registry.

**Impact on plan:**
- §3.3 reworded: "Reuses `cli/sync.py:_select_project_ids` enumeration
  pattern. Builds per-project resources synchronously via the same
  factory call sequence as `ProjectRegistry._build_resources` —
  extracted to a shared synchronous helper if a second CLI
  consumer needs it (deferred until then to avoid premature
  abstraction)."
- New sub-decision **D8**: cleanup builds resources synchronously
  per project; if the server is running concurrently against the
  same project, the WAL mode of SQLite (already configured by
  `DBEngine`, see `db_engine.py`) makes concurrent reads/writes
  safe; cleanup's `DELETE FROM events` will block briefly behind
  any in-flight server write, which is acceptable.

### C3. The Observation API endpoints' `project_id` resolves through `ProjectRegistry`, not by FS walk

**Finding:** Observation API endpoints run *inside* the server
process and have `request.app.state.server_state.registry`
available. The plan was right but did not state the asymmetry
explicitly: **API uses registry, CLI uses FS walk.** Without
spelling this out, an implementer could naturally try to FS-walk
in the API too and miss the cached engine.

**Impact on plan:**
- §3.2 amended explicitly: "Endpoints obtain per-project resources
  via `await request.app.state.server_state.registry.get(project_id)`,
  matching the existing hooks router pattern in
  `src/secondsight/api/hooks.py`."
- Cross-reference D8 from C2: "CLI cleanup is the only path that
  bypasses the registry; document this asymmetry in the
  observation router module docstring so a future contributor
  doesn't import it as a generic 'project enumeration' pattern."

## Death-test additions / amendments

Adding **DC-6b** to the existing seven death cases:

- **DC-6b**: `RetentionConfig.load(home, project_id)` with no
  config files present (neither global nor per-project) returns
  the built-in default (90 days, source=`builtin_default`) and
  does NOT raise. Otherwise every fresh `secondsight cleanup` fails
  for users who never wrote a config file.

## Plan amendments to apply

I will now patch `2-plan.md` and `index.yaml` in-place to reflect
C1–C3 and DC-6b. The patches are local — no decision changes, only
factual corrections grounded in the verified code.

## Confidence after verification

The plan is implementable as amended. No deeper investigation
needed before the (A)-branch implementation heartbeat begins. The
unblocked subset truly is unblocked: the only soft dependency
(config infrastructure) does not exist *and is not needed* — task-A1
introduces it as part of GUR-107a.
