# task-B5 — Extend `secondsight cleanup` CLI to include AnalysisResultsPurger

## Context

Per `2-plan.md §4, D7`. The CLI subcommand was shipped in GUR-147 (locate via grep
`secondsight cleanup`); this task extends it to also resolve `analysis_ttl_days` and run
`AnalysisResultsPurger` per project.

Auto-include both purgers — no new CLI flag (D7). Each TTL is resolved independently; each
purger runs independently per project.

## Changes

1. After loading `RetentionConfig`, log a structured INFO line per project that names both
   resolved values AND their source attributions:
   ```
   retention resolved: project_id=... raw_traces_ttl_days=90 raw_traces_source=...
                       analysis_ttl_days=365 analysis_ttl_source=...
   ```
2. After running `RawTracesPurger`, run `AnalysisResultsPurger` against the same project.
3. Aggregate exit code: non-zero if either purger had_failures (consistent with GUR-147 D3).
4. `--dry-run` enumerates from BOTH purgers; prints a summary table including counts for
   `session_reports` + `behavior_flags`.

## Death tests required

- **DC-B1** — typo in `analysis_ttl_day` config field → log line shows
  `analysis_ttl_source=builtin_default` (assert via log capture in CLI integration test).
- **DC-B7** — empty install: CLI exits 0; log lines name both 0-counts.
- `--dry-run` does NOT call any destructive method (assert via spy on both purger classes).
- Per-project independence: project A's analysis_ttl mismatch does not stop project B's purge.

## Scar report items

- **Order of side-effects per project:** within one project, do we run raw_traces purger first
  or analysis purger first? Recommendation: **raw_traces first, analysis second**, because
  analysis material is small and re-derivable; if a crash interrupts the second, the operator
  loses less. Document and pin.
- **Log noise:** every project emits two purge INFO lines. For 1k projects that's verbose. v1
  ships verbose; future work could batch / summarize.

## Out of scope

- New CLI flags — D7 explicitly rejects them.
- Cross-project ordering — each project resolves independently.

## Done when

- Existing CLI tests still pass.
- New CLI integration tests cover DC-B1 (log capture), DC-B7 (empty), and `--dry-run` spy
  assertion.
- README / CLI `--help` text mentions both TTLs.
