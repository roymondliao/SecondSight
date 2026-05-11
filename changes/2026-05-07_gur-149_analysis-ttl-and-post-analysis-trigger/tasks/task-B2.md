# task-B2 — AnalysisResultsPurger + enumerator

## Context

Per `2-plan.md §2.2, D1, D4`. New module `src/secondsight/storage/analysis_retention.py` (parallel
to `retention.py`). Reaps `session_reports` + `behavior_flags` rows older than the resolved
`analysis_ttl_days`.

Boundary: `session_reports.created_at` (D1). Re-runs preserve `created_at` per the existing
`SessionReportsRepository.upsert` contract (lines 5-7 of that module's docstring), so the boundary
is stable.

Order: `behavior_flags` first, `session_reports` second (D4). On crash between stages, the next
purge run re-detects the same `session_reports` row and re-attempts `behavior_flags` deletion as a
no-op. Forward progress, no orphans.

## Interface

```python
@dataclass(frozen=True)
class ExpiredAnalysis:
    session_id: str
    report_created_at: datetime  # boundary basis

PurgeStage = Literal["filesystem", "database", "session_reports", "behavior_flags"]

def enumerate_expired_analyses(
    session_reports_repo,
    *,
    analysis_ttl_days: int,
    now: datetime,
) -> list[ExpiredAnalysis]: ...

class AnalysisResultsPurger:
    def __init__(
        self,
        *,
        session_reports_repo,
        behavior_flags_repo,
    ) -> None: ...

    def purge(self, expired: Sequence[ExpiredAnalysis]) -> PurgeResult: ...
```

Reuse `PurgeResult` / `PurgeFailure` from `retention.py`. Extend `PurgeStage` to include the new
stage strings (or define a parallel `AnalysisPurgeStage` — your call, document in scar).

## Death tests required

- **DC-B6** — partial-purge orphan guard. Simulate a test double on `behavior_flags_repo` that
  raises after the first delete; assert subsequent purge run re-detects + completes.
- **DC-B7** — empty install. Empty `session_reports` table → enumerator returns `[]`, purger no-ops
  cleanly.
- Inclusive boundary: a row whose `created_at` is exactly `now - ttl` IS expired (mirrors
  GUR-147 enumerate_expired_sessions).
- Stable order: enumerator returns sessions ordered by `session_id` ascending (matches GUR-147).

## Scar report items

- **Repo API choice:** does `enumerate_expired_analyses` use `session_reports_repo.get_all()` +
  Python filter, or a SQL `WHERE created_at <= cutoff` query? Prefer SQL for scale; document if
  the repo doesn't currently expose that.
- **`behavior_flags` enumeration:** the purger enumerates by `session_id`; if a session has zero
  flags, the DELETE should still succeed (rowcount 0) rather than raise.

## Out of scope

- FS report.json removal — that's the eager-cleanup path (task-B4), not the steady-state purger.
  AnalysisResultsPurger is DB-only.
- CLI wiring — task-B5.

## Done when

- New unit tests in `tests/unit/storage/test_analysis_retention.py` cover DC-B6, DC-B7, inclusive
  boundary, and stable order.
- Purger handles partial failure per `PurgeFailure` contract; assertions on `had_failures`.
