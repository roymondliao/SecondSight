# Overview: Server Analysis Runtime Wiring

## Goal

Make the server path truly own automatic analysis:

- `session_end` should trigger analysis without manual CLI intervention
- stale-session sweeper should perform timeout recovery instead of warning-only loops

## What We Learned

The production issue is not a missing `session_end`.

For project `SecondSight`, investigation showed:

- the stale session already has a persisted `session_end`
- the project has no `analysis_runs`
- the server never registers the pipeline callback that would dispatch on `session_end`
- the sweeper only emits warnings and never dispatches

So the real defect is server-side orchestration wiring.

## Core Fix

Introduce shared per-project analysis runtime ownership on the server:

1. materialize a project-scoped `Trigger` + `Orchestrator` path
2. register the trigger callback against the project's `ObservationPipeline`
3. let the sweeper reuse the same trigger for timeout recovery

## Why This Matters

Without this wiring:

- sessions can complete normally but never enter analysis
- the dashboard remains empty even though observation data exists
- stale-session warnings create operational noise without recovering anything

## Main Constraints

- keep hook latency fire-and-forget
- reuse existing trigger idempotency and locks
- avoid a second divergent server-only orchestrator builder
- no schema changes, no new tables
