# Task 4: Raw Ingress Durability and Docs/Test Migration

## Goal

Persist raw ingress records separately from normalized events and update docs/tests to reflect the new contract.

## Files

- Create: `src/secondsight/storage/ingress_record.py`
- Create: `src/secondsight/storage/raw_ingress_store.py`
- Modify: `src/secondsight/storage/raw_trace_store.py`
- Modify: `src/secondsight/api/registry.py`
- Modify: `src/secondsight/api/server.py`
- Modify: `src/secondsight/storage/sync_log.py`
- Create: `tests/storage/test_ingress_record.py`
- Create: `tests/storage/test_raw_ingress_store.py`
- Modify: `README.md`

## Storage requirement

Persist two artifacts:

1. raw ingress record
2. normalized canonical `Event`

The raw ingress artifact must contain enough information to replay normalization:

- `agent`
- `event_type`
- `event_id`
- `timestamp`
- `sequence_number`
- raw `payload`

`sequence_number` persistence is mandatory in both layers:

- raw ingress record keeps the originally claimed ordering value
- normalized `Event` keeps the same value unchanged

No replay or backfill path may allocate a new `sequence_number` for an existing ingress record.

## Replay requirement

Fallback replay and future backfill should use the raw ingress record as replay input, not rebuild from canonical event JSON.

Replay invariants:

- reuse original `event_id`
- reuse original `timestamp`
- reuse original `sequence_number`
- do not touch the live counter for already-claimed records

Counter state recovery rule:

- if replay observes an ingress record with `sequence_number=N`, future live claims for the same `agent + session_id` must continue from at least `N+1`
- implementation may satisfy this either by:
  - preserving the counter state across outages, or
  - reconciling counter state from replay input before resuming live claims

## Death tests

1. Raw ingress write succeeds even if normalized-event DB insert fails.
2. Replaying raw ingress after adapter changes reproduces the same `event_id` and `sequence_number`.
3. Corrupt raw ingress file fails loudly rather than silently skipping.
4. Replay of a stored ingress record does not advance or overwrite its original `sequence_number`.
5. After replay of late events, the next live event for that session does not restart from `0` or collide with prior numbers.
