# task-B1 — Extend RetentionConfig with analysis_ttl_days

## Context (zero-context-readable)

Per `2-plan.md §2.1, D6`. `RetentionConfig` currently resolves only `raw_traces_ttl_days`. Add a
parallel `analysis_ttl_days` field with the same precedence chain (per-project → global →
builtin), separate source attribution, and reuse the existing `_validate_ttl` helper.

Module: `src/secondsight/storage/retention.py`.

## Interface change

```python
@dataclass(frozen=True)
class RetentionConfig:
    raw_traces_ttl_days: int
    raw_traces_source: ConfigSource          # RENAME from `source` for symmetry
    analysis_ttl_days: int                   # NEW
    analysis_ttl_source: ConfigSource        # NEW

BUILTIN_DEFAULT_ANALYSIS_TTL_DAYS = 365
```

Note: renaming `source` → `raw_traces_source` is a load-bearing call-site update; grep the codebase
and update every reader. If the rename is too disruptive, keep `source` and add only
`analysis_ttl_source`. Make the call (justify in scar report).

## Death tests required

- **DC-B1** — TTL config typo silently uses default. Per-project config with
  `analysis_ttl_day = 30` (missing `s`) → result has `analysis_ttl_days == 365` and
  `analysis_ttl_source == "builtin_default"`.
- Coverage of all three precedence layers for `analysis_ttl_days` (mirrors GUR-147 task-A1's
  raw_traces tests).
- `_validate_ttl` rejects bool / non-int / non-positive for the analysis field too.

## Scar report items to expect

- **Decision call:** rename `source` → `raw_traces_source` vs add only `analysis_ttl_source`.
  Document which path was taken and call-site count.
- **Drift risk:** if a future TTL knob is added, the `_read_retention_section` helper currently
  hard-codes single-key extraction. Note for future expansion.

## Out of scope

- Cleanup pipeline integration — task-B5.
- Purger consumer — task-B2.

## Done when

- New unit tests in `tests/unit/storage/test_retention.py` cover DC-B1 + 3-layer precedence for
  analysis_ttl_days.
- All existing retention tests still pass with the rename (or non-rename) call-site update.
- Module docstring references SD §3.10.1 default.
