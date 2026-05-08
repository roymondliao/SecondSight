# task-B6 — Orchestrator factory wiring

## Context

Per `2-plan.md §4` (orchestrator-factory TBD). The factory is the boot-time site that constructs
`Orchestrator`. When `[retention].cleanup_after_analysis = true`, the factory must construct
`PostAnalysisCleanupTrigger` and pass it as `on_analysis_complete=trigger`.

**First step in this task:** locate the orchestrator construction site. Likely candidates:
- `src/secondsight/cli/` (some subcommand likely constructs it for analyze workflows)
- An app factory in `src/secondsight/api/` if the analysis runs via HTTP trigger
- A test fixture (out of scope here)

If no production construction site exists yet (orchestrator only constructed in tests), document
that as a gap; the wiring will land later when the orchestrator gets a real entry point.
**Do NOT invent a fake factory just to satisfy this task — that would be a silent rot path.**

## Changes (assuming a factory exists)

```python
def build_orchestrator(...) -> Orchestrator:
    config = RetentionConfig.load(home=..., project_id=...)
    on_complete: Callable[[str], None] | None = None
    if config.cleanup_after_analysis:        # NEW config flag — see scar
        purger = RawTracesPurger(repo=..., raw_trace_store=...)
        on_complete = PostAnalysisCleanupTrigger(
            cleanup_after_analysis=True,
            raw_traces_purger=purger,
            events_repo=...,
        )
    return Orchestrator(..., on_analysis_complete=on_complete)
```

## Boot-time guard (DC-B4)

If `config.cleanup_after_analysis is True` AND `on_complete is None` after the branch, that's a
wiring drop. Choose ONE policy and document:
- (A) Log WARNING and continue (eager cleanup silently disabled).
- (B) Raise `RuntimeError` with a clear message.

**Recommendation: (B) raise.** Silent disablement is the silent-failure pattern this whole
ticket exists to close.

## Death tests required

- **DC-B4** — config says `cleanup_after_analysis=true` but the factory branch is mocked to
  return None. Assert: factory raises (or logs WARN, per chosen policy) with a message that
  names the missing wire.
- Factory with `cleanup_after_analysis=false` builds an orchestrator with
  `on_analysis_complete is None`. Smoke test.

## Scar report items

- **Config field already accepted? Or new?** `cleanup_after_analysis` may not yet be a recognized
  TOML key in `RetentionConfig.load()`. If not, this task adds it. Document as a config schema
  expansion in the scar report.
- **Factory location:** if the production site doesn't exist, this task ships partial wiring +
  documents the dead-code-until-orchestrator-has-an-entry-point gap.

## Out of scope

- Adding new entry points for analysis (CLI, HTTP, etc.) — that's GUR-103 territory.

## Done when

- Factory wiring lands at the production site, OR a structured note in `index.yaml` records that
  no production factory exists yet.
- DC-B4 test passes.
- B-S3 acceptance reflects the chosen policy (WARN vs raise).
