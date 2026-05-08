# task-B3 — Orchestrator on_analysis_complete callback

## Context

Per `2-plan.md §2.3, D2, D3`. Modify
`src/secondsight/analysis/orchestrator.py:Orchestrator`.

## Changes

```python
class Orchestrator:
    def __init__(
        self,
        ...,
        *,
        segmenter: Segmenter | None = None,
        on_analysis_complete: Callable[[str], None] | None = None,  # NEW kwarg
    ) -> None:
        ...
        self._on_analysis_complete = on_analysis_complete
```

Invocation: at end of `analyze_session`, **after** `advance_stage(run_id, "summary_written")` and
**before** `return AnalyzeSessionResult(...)`. Wrap in `try / except Exception`:

```python
if self._on_analysis_complete is not None:
    try:
        self._on_analysis_complete(session_id)
    except Exception as exc:
        sanitized = _sanitize_failure_message(exc)
        _logger.error(
            "on_analysis_complete callback raised for session_id=%r: %s",
            session_id, sanitized,
        )
        # Do NOT re-raise. Analysis itself succeeded.
```

## Failure policy (D3)

Callback exceptions are swallowed + logged at ERROR. The `analysis_runs` row stays at
`summary_written`; the caller receives a normal `AnalyzeSessionResult`. Justification: cleanup is
downstream of analysis. A failed cleanup must not retroactively poison a successful analysis.

## Death tests required

- **DC-B3** — callback raises `RuntimeError("boom")`. Assert: `analyze_session` returns
  `AnalyzeSessionResult` (no exception propagates), `result.stage == SUMMARY_WRITTEN`, log
  capture contains the sanitized error.
- Callback NOT invoked when `on_analysis_complete is None` (default). Simple smoke test.
- Callback receives the correct `session_id` (the one passed to `analyze_session`, not some
  stale value).

## Scar report items

- **Invocation site coupling:** the callback is wired to the end of the outer try/except in
  `analyze_session`. If a future refactor splits analyze_session into smaller methods, the
  callback site must move with the stage transition, not with method boundaries.
- **Async future:** if `Orchestrator` ever becomes fully async, `Callable[[str], None]` widens to
  `Callable[[str], Awaitable[None]] | Callable[[str], None]`. Note the variance for a future
  contributor.

## Out of scope

- The trigger consumer — task-B4.
- Factory wiring — task-B6.

## Done when

- Orchestrator unit tests cover the three death tests.
- `Orchestrator.__init__` docstring documents callback contract per acceptance B-D2.
