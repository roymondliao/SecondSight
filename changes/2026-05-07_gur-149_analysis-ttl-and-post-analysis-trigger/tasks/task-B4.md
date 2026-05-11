# task-B4 — PostAnalysisCleanupTrigger consumer

## Context

Per `2-plan.md §2.4, §3 DC-B4, DC-B5; D5`. New module
`src/secondsight/analysis/post_analysis_cleanup.py`. The canonical consumer of
`Orchestrator.on_analysis_complete`. Constructed at boot time (CLI / app factory); registered as
the orchestrator's callback when `[retention].cleanup_after_analysis = true`.

## Interface

```python
class PostAnalysisCleanupTrigger:
    """Callback consumer that eagerly purges raw_traces for one just-completed session.

    LOAD-BEARING NOTE (gap-fs-collision, 2-plan.md D5): when this trigger fires,
    RawTracesPurger.purge() shutil.rmtree's the entire `{home}/projects/{project_id}/sessions/
    {session_id}/` directory — which INCLUDES the orchestrator's session_report.json FS backup
    (orchestrator.py:_write_filesystem_backup). The DB row in `session_reports` remains
    authoritative; tools that consume the FS backup must fall back to the DB after eager cleanup.
    """

    def __init__(
        self,
        *,
        cleanup_after_analysis: bool,
        raw_traces_purger: RawTracesPurger,
        events_repo: EventsRepository,
    ) -> None: ...

    def __call__(self, session_id: str) -> None:
        # 1. If not cleanup_after_analysis: log INFO + return.
        # 2. Read last_event_at for the session (single DB query).
        # 3. Synthesize ExpiredSession(session_id, last_event_at).
        # 4. Call raw_traces_purger.purge([expired]).
        # 5. Log structured INFO line: purged | failed | no-op.
        # 6. Do NOT raise (callers swallow per Orchestrator contract anyway, but be explicit).
        ...
```

## Death tests required

- **DC-B4** — boot-time guard. Construct trigger with `cleanup_after_analysis=True` but pass
  `on_analysis_complete=None` to a separate Orchestrator (simulating a wiring drop). The factory
  / boot path must either WARN or raise. **Implementation chooses one; the test pins consistency.**
- **DC-B5** — idempotent re-invocation. Call trigger twice on the same `session_id`; assert both
  return cleanly. (Relies on GUR-147 `_delete_fs_session` returning False for missing dirs.)
- `cleanup_after_analysis=False` → trigger is a no-op; structured INFO logged; no purger called.
  Use a spy purger to assert zero invocations.
- Trigger does NOT raise even when `RawTracesPurger.purge()` returns `had_failures=True`. The
  partial-failure structured ERROR is enough; the trigger logs that and returns normally.

## Scar report items

- **Race window:** between `analyze_session` reaching `summary_written` and the trigger firing,
  a concurrent CLI cleanup run may already have reaped the session. The idempotency test pins
  this; document the window in the module docstring as a known but bounded behavior.
- **`last_event_at` lookup:** the trigger does one extra DB read per analysis. If that becomes a
  hotspot, the orchestrator could pass `last_event_at` to the callback. Document this as a
  performance footnote, not a v1 change.

## Out of scope

- The `cleanup_after_analysis` config field plumbing — already accepted by TOML loader; consumed
  here. No loader changes needed.
- Factory wiring — task-B6.

## Done when

- New tests in `tests/unit/analysis/test_post_analysis_cleanup.py` cover the four cases above.
- Module docstring includes the gap-fs-collision LOAD-BEARING NOTE (acceptance B-D3).
