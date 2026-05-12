# Task 2: Hook Transport Rewrite

## Goal

Rewrite bundled hook scripts so they act as transport shims rather than payload normalizers.

## Files

- Modify: `scripts/hooks/_lib.sh`
- Modify: `scripts/hooks/pre-tool-use.sh`
- Modify: `scripts/hooks/post-tool-use.sh`
- Modify: `scripts/hooks/user-prompt.sh`
- Modify: `scripts/hooks/session-start.sh`
- Modify: `scripts/hooks/session-end.sh`
- Modify: `tests/scripts/test_hook_fallback.py`

## Responsibilities

The hook transport must:

- read raw stdin JSON unchanged
- determine `agent` from install/source context
- determine `event_type` from the invoked script
- generate `event_id`
- inject `timestamp` if the payload does not provide one
- generate per-session `sequence_number`
- POST one stable ingress record
- append the same ingress record to fallback spool on failure

## Sequence counter design

The counter must be:

- keyed by `agent + session_id`
- monotonic across all event types
- durable enough to survive temporary server downtime
- atomic under concurrent hook invocations in one session

Recommended implementation:

- local state file under a SecondSight-owned directory
- advisory lock or equivalent file-locking during read/increment/write

### Counter state model

Define a per-session counter state keyed by:

- `session_key = "{agent}:{session_id}"`

Persist:

- `next_sequence_number`

Recommended storage locations:

- state root: `~/.secondsight/state/sequence/`
- one file per `session_key`, or
- a small SQLite DB owned by the hook transport

The first emitted event for a new session should claim:

- `sequence_number = 0`

which implies the stored `next_sequence_number` starts at:

- `0` for unseen session keys

### Claim algorithm

The transport must claim the sequence number before POST or fallback append.

Required algorithm:

1. read raw stdin payload
2. derive `agent` from install/source context
3. extract `session_id` from raw payload
4. compute `session_key = "{agent}:{session_id}"`
5. acquire exclusive lock for `session_key`
6. read current `next_sequence_number`
7. assign current value as this event's `sequence_number`
8. persist `next_sequence_number + 1`
9. release lock
10. build ingress record with claimed `sequence_number`
11. POST ingress record, or append the same record to fallback spool on failure

### Locking requirement

The implementation must serialize concurrent hook invocations for the same session.

Allowed approaches:

- file lock on per-session counter file
- SQLite transaction with `BEGIN IMMEDIATE`
- equivalent OS-backed exclusive lock

Not acceptable:

- lock-free read/modify/write
- in-memory shell variable only
- process-local cache without durable backing

### Replay invariant

Once an ingress record has claimed a `sequence_number`, that value is immutable.

This means:

- fallback spool must store the full ingress record, including `sequence_number`
- replay must reuse the stored `sequence_number`
- replay must never re-claim a fresh counter value

### Session lifecycle rule

Do not delete counter state immediately on `session_end`.

Reason:

- late-arriving hook events or fallback replay may still exist

Acceptable strategies:

- leave counter state in place until TTL cleanup
- mark session closed but keep last `next_sequence_number`

### Failure semantics

If the transport cannot extract `session_id`, it must fail loudly and refuse to fabricate a counter key.

If the transport cannot persist the incremented counter atomically, it must fail before POST so it does not emit an event with ambiguous ordering.

## Death tests

1. Two concurrent hook invocations for the same session do not emit the same `sequence_number`.
2. Server-down fallback appends the full ingress record, not just raw payload.
3. Retried POST after server recovery preserves original `event_id` and `sequence_number`.
4. Hook exits 0 even when counter state exists but server POST fails.
5. First event for a fresh session emits `sequence_number=0`.
6. `session_end` does not cause the next replayed late event to restart from `0`.
