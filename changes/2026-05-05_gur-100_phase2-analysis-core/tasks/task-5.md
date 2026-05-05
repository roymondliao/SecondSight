# Task 5: Supplementary metrics + SD patches (D3 ship gate)

## Context

Read: `overview.md` for full architecture. Tasks 1–4 must be complete.
This task imports `SegmentData`, `SegmentMetrics`, `ToolUseSpan` from
`secondsight.analysis.schemas`.

This task ships:

1. The pure-function `compute_segment_metrics(segment) -> SegmentMetrics`
   per SD §5.3.1 step 2.
2. **Both `docs/system_design.md` patches** that satisfy the **D3
   ship-manifest gate**:
   - §5.5.2 — add `confidence` field to the prompt output schema.
   - §7.4 — add `disabled_at` and `disabled_reason` columns to the
     directives DDL block.

**Why both SD patches land here, not earlier:** keeps the SD edit in
one task so the reviewer sees one coherent doc diff. Earlier tasks
referenced these SD updates but did not land them.

## Files

- Create: `src/secondsight/analysis/metrics.py`
- Create: `tests/analysis/test_metrics.py`
- Modify: `src/secondsight/analysis/__init__.py` to export
  `compute_segment_metrics`
- Modify: `docs/system_design.md` — §5.5.2 + §7.4 patches per spec
  below (D3 gate)

## Death Test Requirements

Write these BEFORE implementation.

- **DT-5.1** — Segment with one thinking event `token_count=None` and
  one with `token_count=500` → `total_tokens == 500`. WARNING log
  emitted naming the None-token event_id. The test fails if (a) the
  result is not 500, (b) `TypeError` is raised, or (c) no WARNING
  log is emitted.
- **DT-5.2** — Single-event segment (only USER_PROMPT) → `duration ==
  0.0` (NOT None). None would imply "no duration measurable" which
  is a different state than "0.0 seconds elapsed".
- **DT-5.3** — Empty segment (`events=[]`) → all four metrics =
  `{total_tokens: 0, unique_files: 0, duration: 0.0, error_count: 0}`.
  No raise.
- **DT-5.4** — Tool-use span with `success=None` (orphan) → does NOT
  count toward `error_count`. Only `success=False` counts as an error.
  An orphan is "unknown", not "failed".

## Implementation Steps

- [ ] Step 1: Write `tests/analysis/test_metrics.py` with DT-5.1..DT-5.4
      plus 2 happy-path tests:
      - 5-event fixture (2 thinking with 1000+1500 tokens, 1 Read
        success on /a.py, 1 Edit success on /a.py, 1 Bash failure)
        with span durations 200/100/2100ms over 7.5s segment timespan
        → metrics match {total_tokens: 2500, unique_files: 1,
        duration: 7.5, error_count: 1}.
      - Purity: re-running on same input returns identical metrics.
- [ ] Step 2: Run tests — all red.
- [ ] Step 3: Implement `metrics.py` per the spec below.
- [ ] Step 4: Run tests — all green.
- [ ] Step 5: Patch `docs/system_design.md` — §5.5.2 (add `confidence`
      field) and §7.4 (add `disabled_at` + `disabled_reason` columns)
      per the spec below.
- [ ] Step 6: Verify ship-manifest condition manually:
      `git diff main -- docs/system_design.md | grep -E '(\+.*confidence|\+.*disabled_at|\+.*disabled_reason)'`
      must show the three additions.
- [ ] Step 7: Run full test suite — no regressions.
- [ ] Step 8: Write scar report. Commit.

## Spec — `src/secondsight/analysis/metrics.py`

```python
"""Supplementary metrics — pure-function per-segment metrics (GUR-100 task-5).

Per SD §5.3.1 step 2: cheap context fed to the LLM analysis prompt
alongside the segment events. The metrics are NOT used for autonomous
filtering decisions; they are advisory inputs.

Death cases enforced here:
- Null token_count contributes 0 with a WARNING log (NEVER raises,
  NEVER silently coerces without trace).
- Empty segment returns all-zero metrics, NEVER raises.
- ToolUseSpan(success=None) does NOT count as error; only success=False
  does.
"""

from __future__ import annotations

import logging
from datetime import datetime

from secondsight.analysis.schemas import SegmentData, SegmentMetrics, ToolUseSpan

_logger = logging.getLogger(__name__)

# Tools that touch a file on the filesystem. unique_files counts
# distinct `target` values across these tool names.
FILE_TOUCHING_TOOLS: frozenset[str] = frozenset(
    {"Read", "Edit", "Write", "MultiEdit", "Glob", "Grep"}
)


def compute_segment_metrics(segment: SegmentData) -> SegmentMetrics:
    """Pure function. No DB. Side effects: WARNING log on null token_count."""
    if not segment.events:
        return SegmentMetrics(
            total_tokens=0, unique_files=0, duration=0.0, error_count=0
        )

    total_tokens = 0
    unique_files: set[str] = set()
    error_count = 0
    timestamps: list[datetime] = []

    for item in segment.events:
        if isinstance(item, ToolUseSpan):
            if item.tool_name in FILE_TOUCHING_TOOLS and item.target is not None:
                unique_files.add(item.target)
            if item.success is False:
                error_count += 1
            # Tool spans don't carry token_count or top-level timestamps;
            # those live on the underlying events. Skip.
        else:
            # Raw event dict (thinking / response / sub_agent_* / session_*).
            tc = item.get("token_count")
            if tc is None:
                if item.get("event_type") in ("thinking", "response"):
                    _logger.warning(
                        "null token_count on event_id=%r event_type=%r — "
                        "contributes 0 to total_tokens",
                        item.get("id"),
                        item.get("event_type"),
                    )
                # Else: legitimate (e.g. session_start), no warning.
            else:
                total_tokens += int(tc)

            ts = item.get("timestamp")
            if isinstance(ts, str):
                try:
                    timestamps.append(datetime.fromisoformat(ts))
                except ValueError:
                    _logger.warning(
                        "unparseable timestamp on event_id=%r ts=%r",
                        item.get("id"),
                        ts,
                    )

    if len(timestamps) <= 1:
        duration = 0.0
    else:
        duration = (max(timestamps) - min(timestamps)).total_seconds()

    return SegmentMetrics(
        total_tokens=total_tokens,
        unique_files=len(unique_files),
        duration=duration,
        error_count=error_count,
    )
```

## Spec — `docs/system_design.md` patches (D3 ship gate)

### §5.5.2 — add `confidence` field to prompt output schema

In the `[Output Format]` JSON block in §5.5.2, add `confidence` to
each flag object. Locate this block (around line 932 of current SD):

```
[Output Format]
回傳 JSON：
{
  "segment_summary": "對此 segment agent 整體表現的一句話評價",
  "flags": [
    {
      "flag_type": "必須是上述定義的合法類型",
      "event_ids": ["涉及的事件 ID"],
      "reason": "為什麼判定為低效（一句話）"
    }
  ],
  "total_events": number,
  "flagged_events": number
}
```

Patch to add `confidence` after `reason`:

```
[Output Format]
回傳 JSON：
{
  "segment_summary": "對此 segment agent 整體表現的一句話評價",
  "flags": [
    {
      "flag_type": "必須是上述定義的合法類型",
      "event_ids": ["涉及的事件 ID"],
      "reason": "為什麼判定為低效（一句話）",
      "confidence": "high | medium | low — LLM 對此 flag 判定的信心度"
    }
  ],
  "total_events": number,
  "flagged_events": number
}
```

Add a paragraph immediately below the JSON block explaining the
field:

> `confidence` 由 LLM 自行判定。Orchestrator 可選擇丟棄低信心 flag
> 以降低 false-positive；schema 層不過濾。

### §7.4 — add `disabled_at` + `disabled_reason` columns

Locate the directives DDL block in §7.4 (around line 1262 of current
SD). Add two columns before the closing `);`:

```
CREATE TABLE directives (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    type            TEXT NOT NULL,       -- 'convention' | 'hint'（hint reserved for future）
    status          TEXT NOT NULL,       -- 'active' | 'disabled' | 'expired' | 'superseded' | 'obsolete'
    instruction     TEXT NOT NULL,
    frequency       REAL,
    trigger_pattern TEXT,
    confidence      REAL,
    max_firing      INTEGER,
    source_flag_type TEXT,
    source_sessions TEXT,
    created_at      DATETIME,
    expires_at      DATETIME,
    updated_at      DATETIME,
    disabled_at     DATETIME,            -- NULL except when status = 'disabled'
    disabled_reason TEXT                 -- NULL except when status = 'disabled'
);
```

Add a sentence below the DDL noting the soft-disable contract:

> 若 status 從 'disabled' 轉回 'active'，`disabled_at` 與 `disabled_reason`
> 必須清除為 NULL（避免 stale 元資料）。`status` 從 'disabled' 轉到
> 任何非 'disabled' 值都同樣清除——只有 'disabled' 狀態擁有這兩個欄位。

## Expected Scar Report Items

- Potential shortcut: count `success=None` (orphan) as an error.
  Rejected — DT-5.4 closes this. "Unknown" is not "failed".
- Potential shortcut: raise on null token_count "to surface upstream
  bug". Rejected — would break analysis flow when a single thinking
  event has null tokens. Log + 0-contribution is the right balance.
- Potential shortcut: derive `duration` from `ToolUseSpan.duration_ms`
  sums. Wrong — that's tool-execution time, not segment wall-clock.
  Use min/max event timestamps.
- Potential shortcut: skip the SD §7.4 patch in this task because
  task-3 already implements the columns in code "and the SD will be
  updated in a follow-up". Rejected — D3 ship gate fails the merge
  if the diff is missing.
- Assumption to verify: existing test fixtures use ISO-format
  timestamps. If not, the `datetime.fromisoformat` parse may fail
  silently — verify or add an explicit format detector.

## Acceptance Criteria

Covers the following acceptance.yaml scenarios:
- "Silent failure - null token_count silently coerces or raises"
- "Ship gate - PR diff includes SD 5.5.2 confidence patch"
- "Ship gate - PR diff includes SD 7.4 directive lifecycle columns"
- "Success - compute_segment_metrics on 5-event fixture matches
  hand-computed values"
