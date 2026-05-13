# Task 3: Upgrade Sweeper To Timeout Recovery And Verify Dedup

## Goal

Turn the stale-session sweeper from warning-only behavior into a real timeout recovery path.

## Scope

- Use the shared per-project trigger to dispatch stale sessions.
- Preserve explicit logs for dispatch, skip, and failure outcomes.
- Verify timeout dispatch does not race into duplicate analysis when event-driven dispatch is also active.

## Files

- `src/secondsight/api/server.py`
- `src/secondsight/sdk/trigger.py`
- server/trigger tests

## Death tests

- Repeated stale warnings without dispatch are not acceptable after this change.
- Event path plus timeout path must not schedule duplicate analysis for the same session.

## Completion criteria

- A stale session causes timeout dispatch instead of endless warning-only repetition.
- Duplicate-dispatch protection is covered by automated tests.
- Logs distinguish `dispatched`, `already-analyzed`, and `another-run-in-flight`.
