# Task 1: BehaviorFlagsRepository.count_per_session_for_project

## Context

Read: `overview.md`. This task adds a single repository method that backs
`GET /api/analysis/trends` (per-session granularity) and the cross-session
piece of `GET /api/analysis/aggregation`. The SQL must SELECT the session
set first (LIMIT applied there), then JOIN flags — DC-7 defense.

Existing surface to study:
- `src/secondsight/storage/behavior_flags_repository.py:113` — `count_by_type`
  (the single-dimension precedent)
- `src/secondsight/storage/session_reports_repository.py:81` — `list_for_project`
  (the canonical "most-recent N sessions" pattern; same ORDER BY rule)
- `src/secondsight/analysis/schemas.py:43` — `BehaviorFlagType` enum

## Files

- Modify: `src/secondsight/storage/behavior_flags_repository.py` — add
  `count_per_session_for_project` method + `SessionFlagBreakdown` dataclass
  (or `TypedDict`).
- Test: `tests/storage/test_behavior_flags_repository_count_per_session.py`
  (new file).

## Death Test Requirements

Write these tests **before** the implementation:

- **DT-1.1** (DC-7): Insert 50 sessions × 5 flags each into `session_reports`
  + `behavior_flags`. Call `count_per_session_for_project(project, limit=10)`.
  Assert `len(result) == 10` (sessions, not 50 and not 250). Assert
  `sum(sum(b.counts.values()) for b in result) == 10 * 5 = 50` (each session
  contributes its 5 flags to its bucket).

- **DT-1.2** (ordering): Insert sessions with `session_reports.created_at`
  spanning a known range. Assert result is sorted by `analyzed_at DESC`.
  The first session in the result is the most recently analyzed.

- **DT-1.3** (zero-flag sessions): Insert a session with a `session_reports`
  row but **zero** behavior_flags. Assert it appears in the result with
  an empty `counts_by_type` dict (or all zeros — pick one convention and
  document).

- **DT-1.4** (cross-project): Insert sessions in project A and project B.
  Call `count_per_session_for_project(A)`. Assert no project B session
  appears.

- **HP-1.1** (happy path): Fixture with 5 sessions, distinct flag-type
  distributions per session. Assert exact counts per session.

## Implementation Steps

- [ ] Step 1: Write the 5 tests above; commit them as failing.
- [ ] Step 2: Run tests — verify all fail with `AttributeError` (method
  doesn't exist).
- [ ] Step 3: Add `SessionFlagBreakdown` dataclass / TypedDict at top
  of `behavior_flags_repository.py` (alongside other types). Fields:
  `session_id: str`, `analyzed_at: datetime`, `counts_by_type: dict[BehaviorFlagType, int]`.
- [ ] Step 4: Implement `count_per_session_for_project(project_id, *,
  limit=50)`. SQL shape:
  ```sql
  WITH recent_sessions AS (
    SELECT session_id, created_at AS analyzed_at
    FROM session_reports
    WHERE project_id = :project_id
    ORDER BY created_at DESC
    LIMIT :limit
  )
  SELECT rs.session_id, rs.analyzed_at, bf.flag_type, COUNT(*) AS cnt
  FROM recent_sessions rs
  LEFT JOIN behavior_flags bf
    ON bf.session_id = rs.session_id AND bf.project_id = :project_id
  GROUP BY rs.session_id, rs.analyzed_at, bf.flag_type
  ORDER BY rs.analyzed_at DESC, bf.flag_type ASC
  ```
  Assemble per-session dicts in Python from the rowset.
- [ ] Step 5: Run tests — verify all 5 pass. Run full
  `tests/storage/` suite — verify no regressions.
- [ ] Step 6: Write scar report at
  `changes/2026-05-08_gur-104_phase2-analysis-cli-api/scar-reports/task-1.md`.
- [ ] Step 7: Commit with message `GUR-104 task-1: BehaviorFlagsRepository.count_per_session_for_project`.

## Expected Scar Report Items

- LEFT JOIN preserves zero-flag sessions; INNER JOIN would silently
  drop them. Verify your SQL chose the right join.
- `analyzed_at` source = `session_reports.created_at` (NOT events
  MAX(timestamp)). Reason: consistency with `list_for_project` ordering.
- BehaviorFlagType decoding: rows in DB store the enum's `.value`
  (str). Convert back to enum via `BehaviorFlagType(row.flag_type)`;
  log + skip on `ValueError` (matches `count_by_type` precedent at
  `behavior_flags_repository.py:128-135`).
- A bare `LIMIT` outside the CTE is the DC-7 trap. Code review must
  confirm the CTE / SUBQUERY structure.

## Acceptance Criteria

Covers `acceptance.yaml`:
- "Silent failure - trends LIMIT applied to flags table not session set"
  (DC-7 — the single most expensive bug if missed)
- Indirectly enables: "Success - GET /api/analysis/trends respects per-session limit"
