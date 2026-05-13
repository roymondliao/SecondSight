# Task 2: Wire Event-Driven Dispatch On `session_end`

## Goal

Ensure a successfully ingested `session_end` event automatically triggers analysis dispatch.

## Scope

- Register `Trigger.register_pipeline_callback()` against the project's `ObservationPipeline`.
- Keep the callback post-ingest so DB durability remains upstream of dispatch.
- Preserve hook fire-and-forget latency behavior.

## Files

- `src/secondsight/api/registry.py`
- `src/secondsight/sdk/trigger.py`
- any affected tests

## Death tests

- `session_end` present in `events` must not leave `analysis_runs` empty indefinitely.
- Callback registration must happen exactly once per project runtime.

## Completion criteria

- After ingesting `session_end`, the server creates an `analysis_runs` row without manual CLI action.
- Logs make it clear that event-driven dispatch occurred.
