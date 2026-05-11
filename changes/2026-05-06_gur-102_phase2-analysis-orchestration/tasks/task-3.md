# Task 3 — `analysis/behavior.py` — Segment-level behavior detector

**Depends on:** task-1 (BehaviorFlagsRepository), task-2 (AnalysisAgent).
**Blocks:** task-5.

## Goal

Build the segment-level pipeline: prompt → agent → validate → persist.
A pure async function (not a class) that the orchestrator calls once
per segment.

## Files to create

- `src/secondsight/analysis/behavior.py`
- `tests/analysis/test_behavior.py`

## Files to modify

- `src/secondsight/analysis/__init__.py` — re-export `detect_segment_flags`

## Function signature

```python
async def detect_segment_flags(
    segment: SegmentData,
    metrics: SegmentMetrics,
    *,
    session_id: str,
    project_id: str,
    behavior_flags_repo: BehaviorFlagsRepository,
    agent: AnalysisAgent,
) -> int:
    """Detect behavior flags in one segment and persist them.

    Pipeline:
      1. build_segment_prompt(segment, metrics) -> prompt str
      2. agent.analyze_segments([prompt]) -> [SegmentAnalysis]
      3. Validate every flag in result.flags against _guard rules
         (flag_type ∈ BehaviorFlagType, confidence ∈ {high,medium,low})
         BEFORE any insert.
      4. Convert SegmentAnalysisFlag → BehaviorFlag with new id, project_id,
         session_id, segment_index, created_at.
      5. behavior_flags_repo.insert_many(flags) — atomic.

    Returns: number of flags persisted (0 to len(result.flags)).

    Raises:
      - AnalysisAgentError: agent call failed irrecoverably.
      - ValueError: pre-insert _guard validation rejected a flag (DC-2).
      - ValidationError: agent returned malformed SegmentAnalysis.

    On any raise, NO flags are persisted from this call (atomic).
    """
```

## Death tests (write FIRST)

- **DT-3.1 (= DT-1.2) — Validation before insert atomicity (DC-2).**
  Configure `FakeAnalysisAgent` to return a `SegmentAnalysis` with
  3 flags where flag #2's `flag_type` would fail `_guard`
  (use `model_construct` to bypass Pydantic at fake-setup, simulating
  GUR-100's defensive guard surface).
  Assert: `detect_segment_flags(...)` raises `ValueError` BEFORE
  `behavior_flags_repo.insert_many` is called. After the raise, the
  `behavior_flags` table is unchanged (verify count via
  `count_by_type` for the project).

- **DT-3.2 — Agent failure does not partial-write.** Fake configured
  with `raise_on_segments_call=True`; `detect_segment_flags(...)`
  raises `AnalysisAgentError`; behavior_flags table unchanged.

- **DT-3.3 — Empty SegmentAnalysis (zero flags).** Fake returns
  `SegmentAnalysis(flags=[], total_events=N, flagged_events=0,
  segment_summary="...")`. Function returns 0 cleanly without
  calling `insert_many` (or calls it with empty list — verify it's
  a no-op via insert_many's existing `if not batch: return 0` early
  return).

- **DT-3.4 — Confidence guard at pre-insert.** Fake returns a flag
  with `confidence='unknown'` (not in {high,medium,low}); function
  raises `ValueError` before insert.

## Happy-path tests

- **HP-3.A** — Single-segment, 2 valid flags. Function returns 2;
  `behavior_flags_repo.get_session_flags(session_id)` returns 2 rows
  with correct flag_type values, segment_index, and confidence.

- **HP-3.B** — Idempotent re-call. Calling twice with the same
  `(segment, ...)` and same fake outputs: second call inserts 0
  new rows (ON CONFLICT DO NOTHING on `id`). Verify both calls
  return 2 (the count of flags *attempted*, matching `insert_many`
  return semantics from GUR-100).

  *Note:* this only works if flag IDs are deterministic given the
  same input. The function should compute deterministic IDs:
  `id = sha256(session_id + "|" + segment_index + "|" + event_ids[0]
  + "|" + flag_type)` or similar. Document the choice in a scar item.

  *Alternative:* if non-deterministic (`uuid4()`) IDs are chosen,
  this test should be reformulated as "second call inserts 2 new
  rows; total = 4" — and `count` returned remains semantic-attempted.
  Pick one and document the choice.

## Scar items to record

- Pre-insert `_guard` validation duplicates the repository's defensive
  guard. Defense-in-depth (validation at boundary + at sink) is
  accepted. Drift between the two is mitigated by the repository
  raising `ValueError` if the function ever lets a bad flag through
  — a regression there will fail death-tests in `task-1`.
- BehaviorFlag ID generation policy: **deterministic** preferred
  (enables HP-3.B re-call idempotency proof). If chosen, document
  the hash inputs and assert in tests.
- Sync agent calls outside this layer are NOT supported. Tests use
  `pytest-asyncio` (`@pytest.mark.asyncio`).
- `analyze_segments` is called with a single-element list per
  segment. The orchestrator may eventually batch multi-segment calls
  for concurrency — at which point `detect_segment_flags` will
  remain single-segment (the batch boundary moves up to the
  orchestrator). Document this so future readers don't conflate
  the layer with the batch.
