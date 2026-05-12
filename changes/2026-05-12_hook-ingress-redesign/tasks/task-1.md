# Task 1: Ingress Contract Redesign

## Goal

Replace the current `HookEnvelope`-at-boundary contract with a thinner ingress envelope that preserves raw payload and moves agent identity to the route/source context.

## Files

- Modify: `src/secondsight/api/schemas.py`
- Modify: `src/secondsight/api/hooks.py`
- Modify: `src/secondsight/adapters/base.py`
- Create: `src/secondsight/api/ingress.py`
- Modify: `tests/api/test_hooks_endpoint.py`
- Create: `tests/api/test_ingress_contract.py`

## Contract

Introduce an ingress model that contains:

- `event_id`
- `timestamp`
- `sequence_number`
- `payload`

Ingress metadata semantics:

- `event_id`
  - transport-generated
  - immutable after first emission
- `timestamp`
  - transport-owned if raw payload lacks a trustworthy native timestamp
  - immutable after first emission
- `sequence_number`
  - transport-generated per-session total-order value
  - must be present at ingress time
  - must not be synthesized by adapters or tracker
  - immutable after first emission and replay

Move:

- `agent` to route/source context
- `event_type` to route/source context
- `project_id` extraction to adapter
- `session_id` extraction to adapter

Recommended route:

- `POST /hook/{agent}/{event_type}`

## Required checks

- route `agent` chooses adapter before payload parsing
- route `event_type` is validated against `EventType`
- ingress schema rejects missing or negative `sequence_number`
- adapter still rejects payload/route mismatches
- adapter-derived `project_id` and `session_id` still go through path-safety validation before storage materialization
- tracker receives `sequence_number` as an already-claimed ordering key and forwards it unchanged

## Death tests

1. Raw Claude payload plus ingress metadata is accepted without requiring `project_id` in the body.
2. Unknown `agent` fails before payload parsing.
3. Known `agent` + mismatched payload marker fails loudly with 422.
4. Unsafe adapter-derived `project_id` fails with 422 before registry materialization.
5. Missing `sequence_number` fails at ingress schema validation, not in adapter logic.
6. Tracker/output event preserves ingress `sequence_number` exactly.
