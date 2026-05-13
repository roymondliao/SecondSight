# Task 1: Create Shared Server-Side Project Analysis Runtime

## Goal

Add a single server-owned per-project analysis runtime so the server path can assemble and reuse the same project-scoped analysis machinery needed for automatic dispatch.

## Scope

- Define the runtime ownership boundary.
- Materialize the analysis repos, orchestrator, and trigger once per project.
- Reuse the same runtime for both event-driven and timeout-driven dispatch.

## Files

- `src/secondsight/api/registry.py`
- optional helper module if the builder deserves extraction
- possibly `src/secondsight/cli/analyze.py` if a shared builder is introduced

## Death tests

- Server path must not assemble a second divergent runtime from the CLI path.
- The same project must not receive multiple trigger instances for the same lifecycle.

## Completion criteria

- A project runtime can be resolved from the server path.
- The runtime exposes a reusable `Trigger`.
- Runtime assembly failures are logged clearly and do not masquerade as stale-session warnings.
