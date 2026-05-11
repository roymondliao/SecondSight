# GUR-149 — Implementation Plan

> **Read first**: `1-kickoff.md`, `problem-autopsy.md`, `pre-thinking.md` in this directory. This plan
> assumes their reframes and accepted gaps.

## §1 Scope (load-bearing summary)

Two wires:

1. **`analysis_ttl_days` resolution + steady-state purger** — extend `RetentionConfig` with a second
   field, add `AnalysisResultsPurger` that reaps `session_reports` + `behavior_flags` rows older
   than the resolved TTL, and wire it into the existing `secondsight cleanup` CLI.
2. **Post-analysis eager-cleanup hook** — add `on_analysis_complete: Callable[[str], None] | None`
   to `Orchestrator.__init__`; invoke it once at end of `analyze_session` after the audit row hits
   `summary_written`. Provide `PostAnalysisCleanupTrigger` as the canonical consumer that, when
   `[retention].cleanup_after_analysis = true`, asks the existing GUR-147 `RawTracesPurger.purge()`
   to reap raw traces for one session_id immediately.

## §2 Tech Spec — I/O with `unknown` output state

### §2.1 `RetentionConfig` (extend)

```
INPUT:  home: Path, project_id: str
OUTPUT:
  success → RetentionConfig(
              raw_traces_ttl_days: int,
              raw_traces_source: ConfigSource,
              analysis_ttl_days: int,
              analysis_ttl_source: ConfigSource,
            )
  failure → RetentionConfigError (file present but malformed / wrong-type / non-positive)
  unknown → N/A. Absent file is the fresh-install path → builtin default. Never raised.
```

`ConfigSource` is unchanged: `per_project_config | global_config | builtin_default`. Each TTL
carries its own source attribution (raw and analysis can come from different layers).

### §2.2 `AnalysisResultsPurger` (new)

```
INPUT:  expired: Sequence[ExpiredAnalysis]
OUTPUT:
  success → PurgeResult(purged_session_ids, failures=())
  failure → PurgeResult(purged_session_ids, failures=(PurgeFailure(stage, error), ...))
  unknown → never. Each session is per-row independent; partial failure is reported,
            never inferred.
```

`ExpiredAnalysis` is a new dataclass paralleling `ExpiredSession`:

```python
@dataclass(frozen=True)
class ExpiredAnalysis:
    session_id: str
    report_created_at: datetime  # session_reports.created_at — boundary basis
```

`stage` enum extended: `Literal["session_reports", "behavior_flags"]`. Stage ordering is
**`behavior_flags` first, `session_reports` second** (delete the dependents before the parent;
mirrors FS-first/DB-second pattern from GUR-147 — touch the larger / many-row table first so a
mid-purge crash leaves a recoverable state, not orphan reports with no flags).

### §2.3 `Orchestrator` callback hook (modify)

```python
class Orchestrator:
    def __init__(
        self,
        ...,
        *,
        segmenter: Segmenter | None = None,
        on_analysis_complete: Callable[[str], None] | None = None,  # NEW
    ) -> None: ...
```

Invocation site: at end of `analyze_session`, **after** `advance_stage(run_id, "summary_written")`
and **before** `return AnalyzeSessionResult(...)`. Runs synchronously in the same call stack.

```
INPUT:  session_id: str (the just-completed session)
OUTPUT:
  success → callback returns None; analysis result returned to caller normally
  failure → callback raised. Orchestrator logs ERROR with sanitized message, then
            **continues** to return AnalyzeSessionResult. The analysis itself succeeded;
            cleanup is downstream.
  unknown → never. Callback either returns or raises. No timeout semantics.
```

### §2.4 `PostAnalysisCleanupTrigger` (new consumer)

```python
class PostAnalysisCleanupTrigger:
    def __init__(
        self,
        *,
        cleanup_after_analysis: bool,
        raw_traces_purger: RawTracesPurger,
        events_repo: EventsRepository,
    ) -> None: ...

    def __call__(self, session_id: str) -> None: ...
```

When invoked:
1. If `cleanup_after_analysis` is False, log INFO and return (no-op).
2. Read `last_event_at` for the session (one DB query against `events`).
3. Synthesize `ExpiredSession(session_id, last_event_at)` and call
   `raw_traces_purger.purge([expired])`.
4. Log result (purged | failed). Do not raise.

The trigger is **constructed at CLI / app boot time**, not by the orchestrator. The orchestrator
only knows the callback shape.

## §3 Death Cases (`coverage_type: silent_failure`)

### DC-B1 — TTL config typo silently uses default

**Trigger:** operator writes `analysis_ttl_day = 30` (missing `s`) in their TOML.
**Lie:** cleanup runs without error; operator believes 30-day TTL is in effect.
**Truth:** silent fall-through to 365-day builtin; rows older than 30 days are NOT reaped.
**Detect:** structured INFO log at every cleanup invocation naming resolved value AND source
attribution: `analysis_ttl_days=365 source=builtin_default`. Operator who expected
`per_project_config` source sees the mismatch.

### DC-B2 — FS backup collision on eager cleanup *(accepted gap [gap-fs-collision])*

**Trigger:** operator sets `cleanup_after_analysis = true`. Analysis writes
`{home}/projects/{pid}/sessions/{sid}/session_report.json` (orchestrator FS backup), then the
post-analysis trigger invokes `RawTracesPurger.purge`, which `shutil.rmtree`s the entire
`sessions/{sid}/` directory.
**Lie:** "raw traces eagerly cleaned" — operator may not realize the FS report backup is also gone.
**Truth:** the FS backup is destroyed alongside the events directory. The DB row remains
authoritative; tools that consume the FS backup must fall back to the DB.
**Detect:** structured INFO log on eager-cleanup path: `eagerly purged session_id=... — note: FS
session_report.json backup also removed; DB row remains in session_reports`.

This is an **accepted gap** carried forward from `pre-thinking.md`. Surface it in the planning gate
for board confirmation. If the board reverses this, the plan changes: trigger must `os.rename` the
report backup out of the sessions tree before purge, or purger must skip JSON files (more complex).

### DC-B3 — Callback exception poisons analysis result

**Trigger:** post-analysis trigger raises (FS lock, DB connection lost, etc.).
**Lie:** `analyze_session` raises; caller believes analysis failed.
**Truth:** analysis succeeded — DB row at `summary_written`, report persisted, FS backup written.
Only the eager cleanup side-effect failed.
**Detect:** orchestrator wraps the callback invocation in a `try / except Exception` boundary; on
exception, log ERROR with sanitized message (mirrors `_sanitize_failure_message`), do NOT re-raise,
continue to return `AnalyzeSessionResult`. The next scheduled CLI cleanup will retry the reap.

### DC-B4 — Callback registered before orchestrator wires it

**Trigger:** consumer constructs `PostAnalysisCleanupTrigger` but the orchestrator factory ignores
it / forgets to pass it to `Orchestrator.__init__`.
**Lie:** "trigger configured" — cleanup_after_analysis=true in config, no errors at boot.
**Truth:** orchestrator's `_on_analysis_complete` is None; callback never invoked; no eager
cleanup ever happens.
**Detect:** at orchestrator construction site (CLI / app factory), if
`cleanup_after_analysis is True` AND `on_analysis_complete is None` → log a WARNING at boot
*and* fail-closed in tests (a death test asserts the boot path raises if the wiring is dropped).

### DC-B5 — Per-session eager purge races with scheduled CLI cleanup

**Trigger:** post-analysis trigger fires concurrently with `secondsight cleanup` for the same
session.
**Lie:** "session reaped" — both paths report success.
**Truth:** one of them sees an empty / already-reaped session and may either no-op or raise on
"directory not found" if not idempotent.
**Detect:** GUR-147's `_delete_fs_session` already returns False (not raise) when the directory is
absent; existing `_delete_db_events_for_session` returns rowcount 0 cleanly when no rows match.
This is **already idempotent** — the death test pins that contract by calling purge twice in a row
on the same session and asserting both calls return cleanly.

### DC-B6 — `behavior_flags` orphans on partial purge

**Trigger:** `AnalysisResultsPurger` deletes `session_reports` row, then crashes before deleting
`behavior_flags`.
**Lie:** "session_reports reaped" — count drops as expected.
**Truth:** orphan `behavior_flags` rows accumulate, no FK to enforce cleanup.
**Detect:** order is `behavior_flags` FIRST, `session_reports` SECOND. A crash between the two
leaves `session_reports` intact (re-enumerable) with `behavior_flags` already gone — the next
purge run re-detects the session_reports row as expired and re-attempts behavior_flags delete
(no-op, returns 0 rowcount cleanly), then deletes session_reports. Forward progress, no orphans.
Death test: simulate crash between stages; assert next purge run completes the reap.

### DC-B7 — Empty orchestrator (no analyses ever)

**Trigger:** fresh install; no `session_reports` rows exist; `secondsight cleanup` runs.
**Lie:** N/A — risk is that the empty path crashes (e.g., `min()` on empty list, division by zero).
**Truth:** `enumerate_expired_analyses` returns `[]`; purger receives empty input; logs
`analysis_results purge: 0 sessions enumerated` and exits 0.
**Detect:** unit test with empty `session_reports` table; assert clean exit + structured log line.

## §4 File Map

```
NEW:
  src/secondsight/storage/analysis_retention.py
    — ExpiredAnalysis dataclass
    — enumerate_expired_analyses(session_reports_repo, *, analysis_ttl_days, now) -> list
    — AnalysisResultsPurger (FS-aware: removes session_report.json backup if present)
    — purge ordering: behavior_flags → session_reports

  src/secondsight/analysis/post_analysis_cleanup.py
    — PostAnalysisCleanupTrigger consumer (constructs ExpiredSession, calls RawTracesPurger)

  tests/unit/storage/test_analysis_retention.py
  tests/unit/analysis/test_post_analysis_cleanup.py

MODIFY:
  src/secondsight/storage/retention.py
    — RetentionConfig: add `analysis_ttl_days`, `analysis_ttl_source` fields
    — RetentionConfig.load(): resolve `analysis_ttl_days` with same precedence
    — BUILTIN_DEFAULT_ANALYSIS_TTL_DAYS = 365
    — _validate_ttl: reused (already generic)

  src/secondsight/analysis/orchestrator.py
    — Orchestrator.__init__: add on_analysis_complete kwarg
    — analyze_session: invoke callback inside try/except after summary_written

  src/secondsight/cli/cleanup.py  (or wherever cleanup CLI lives — TBD per task-B5)
    — Resolve analysis_ttl_days
    — Construct AnalysisResultsPurger and run it per project
    — Log resolved values + sources

  src/secondsight/cli/<orchestrator-factory>  (TBD per task-B4)
    — When cleanup_after_analysis=true, construct PostAnalysisCleanupTrigger
    — Pass it to Orchestrator(on_analysis_complete=trigger)
    — Boot-time WARNING if config says true but trigger not wired

  tests/unit/storage/test_retention.py — extend for analysis_ttl_days
  tests/unit/analysis/test_orchestrator.py — add callback invocation + failure-policy tests
```

## §5 Decisions Log

| ID | Decision | Rejected alternatives |
|----|----------|------------------------|
| D1 | TTL boundary on `session_reports.created_at` | `analysis_runs.completed_at` — runs may exist without reports on failure path; less stable. `updated_at` — re-runs would extend TTL indefinitely. |
| D2 | Constructor-injected callback, single-subscriber | Pub/sub event bus — overbuilt for one consumer. Module-level global registry — hidden coupling. |
| D3 | Callback failure swallowed + logged ERROR | Re-raise — poisons a successful analysis. Retry inline — duplicates the next CLI cleanup pass. |
| D4 | Purge order: `behavior_flags` first, then `session_reports` | Reverse — leaves orphan flags on partial failure. Single transaction — would work but mixes destructive + read paths in one txn block; this codebase keeps each per-row. |
| D5 | Eager cleanup destroys FS report backup (accepted gap) | Move report.json out before rmtree — adds complexity, races with concurrent reads. Skip *.json in rmtree — purger becomes content-aware, fragile. |
| D6 | `analysis_ttl_days` resolved per-project (same as raw) | Global-only — operators already expect per-project override from raw_traces. |
| D7 | Auto-include analysis purger in `secondsight cleanup` | Add `--analysis-only` flag — extra surface; both TTLs are independent so running both is the consistent default. |

## §6 Out of Scope

- `analysis_runs` audit table TTL (separate policy decision).
- A general event bus on `Orchestrator` (single-callback only).
- Soft-delete / archival semantics — hard delete only in v1.
- Cross-project cleanup ordering (each project resolved independently, same as GUR-147).
