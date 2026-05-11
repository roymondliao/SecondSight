# GUR-149 — Acceptance Criteria (death-first)

> Order: silent-failure scenarios → degradation → happy path. A test plan that flips this is
> `coverage_type: prayer`.

## §1 Silent-failure scenarios (must-have death tests)

### B-S1 — DC-B1: TTL config typo silently uses default
- **Given** a per-project `config.toml` with `analysis_ttl_day = 30` (missing `s`)
- **When** `RetentionConfig.load(home, project_id)` is called
- **Then** `config.analysis_ttl_days == 365` and `config.analysis_ttl_source == "builtin_default"`
- **And** when `secondsight cleanup` runs, a structured INFO log line names both values:
  `analysis_ttl_days=365 source=builtin_default`
- **Evidence:** log capture asserting the resolved value + source string.

### B-S2 — DC-B3: Callback exception does not poison analysis
- **Given** an `Orchestrator` constructed with `on_analysis_complete=lambda sid: raise RuntimeError("boom")`
- **When** `analyze_session(sid)` runs the full pipeline successfully through `summary_written`
- **Then** `analyze_session` returns a normal `AnalyzeSessionResult` (no exception propagates)
- **And** an ERROR log line is emitted with the sanitized exception type+message
- **And** the `analysis_runs` row is at `summary_written` (not `failed`)
- **Evidence:** assertion on returned `AnalyzeSessionResult.stage`; assertion on log capture.

### B-S3 — DC-B4: cleanup_after_analysis=true but trigger not wired
- **Given** a config with `[retention].cleanup_after_analysis = true`
- **And** an `Orchestrator` constructed with `on_analysis_complete=None`
- **When** the boot path is exercised
- **Then** a WARNING is emitted naming the inconsistent state, OR the boot path raises a
  `RuntimeError` that names the missing wire.
- **Evidence:** death test asserts the warning OR the raise. Implementation chooses one;
  consistency between code and test is what's verified.

### B-S4 — DC-B6: Partial purge does not orphan behavior_flags
- **Given** `session_reports` has 5 expired sessions, each with N `behavior_flags`
- **When** `AnalysisResultsPurger.purge()` is interrupted between `behavior_flags` deletion and
  `session_reports` deletion for one of the sessions (simulated via test double that raises after
  the first stage)
- **Then** the next `purge()` call enumerates the same session as expired, completes the reap, and
  results in zero `behavior_flags` rows for that session_id.
- **Evidence:** SELECT count from both tables before/after each call.

### B-S5 — DC-B5: Per-session race is idempotent
- **Given** raw traces for `session_id=X` already purged (FS removed, DB events row gone)
- **When** `PostAnalysisCleanupTrigger(X)` is invoked
- **Then** the call returns cleanly; no exception; structured log notes "no FS dir, no DB rows".
- **Evidence:** call twice in succession; assert both return without raising.

## §2 Degradation scenarios

### B-D1 — DC-B5 partial: FS purge succeeds, DB delete fails
Mirror GUR-147 D3: FS removed, DB row remains. Already-tested in GUR-147 for
`RawTracesPurger`; the eager-cleanup path piggybacks on it. **No new acceptance** — just
confirm the trigger does not invent its own error handling.

### B-D2 — `analysis_ttl_days` set per-project but `raw_traces_ttl_days` only global
- **Given** per-project config defines `analysis_ttl_days = 30`; global config defines
  `raw_traces_ttl_days = 90`
- **When** `RetentionConfig.load()` resolves
- **Then** `analysis_ttl_source == "per_project_config"` AND `raw_traces_source == "global_config"`.
  Each TTL resolves independently through the precedence chain.

## §3 Happy path

### B-H1 — `analysis_ttl_days` resolution end-to-end
Per-project (30) → global (60) → builtin (365) precedence; assert each layer wins when present.
Mirrors GUR-147 A-T1..A-T4 for `raw_traces_ttl_days`.

### B-H2 — `AnalysisResultsPurger` reaps eligible rows
Given `session_reports.created_at` older than `now - analysis_ttl_days` for K sessions: purge
removes K `session_reports` rows + all matching `behavior_flags` rows. Assert exact counts.

### B-H3 — Post-analysis trigger reaps raw traces eagerly
End-to-end: orchestrator runs → trigger registered with `cleanup_after_analysis=true` → after
`analyze_session` returns, `events` table has zero rows for that session AND
`{home}/projects/{pid}/sessions/{sid}/` is gone (including the report JSON, per accepted gap
[gap-fs-collision]).

### B-H4 — Empty install: cleanup runs cleanly
Fresh DB, no `session_reports` rows; `secondsight cleanup` completes with exit 0 and structured
log lines naming the resolved TTLs and 0 sessions enumerated.

### B-H5 — `analysis_runs` audit rows are NOT reaped
Given an `analysis_runs` row 400 days old (older than the 365d default) and its corresponding
`session_reports` row reaped: the `analysis_runs` row remains in the DB. Out-of-scope policy
explicitly tested as a regression guard.

## §4 Coverage gates

- **B-X1** — every death case (DC-B1..DC-B7) has at least one test in §1 or §2.
- **B-X2** — `coverage_type: silent_failure` for §1; `coverage_type: degradation` for §2;
  `coverage_type: happy_path` for §3.
- **B-X3** — no test in §3 may pass without §1 also passing (i.e., no happy-path test that
  bypasses the death-test setup).

## §5 Documentation gates

- **B-D1** — `RetentionConfig` docstring lists both fields and both source attributions.
- **B-D2** — `Orchestrator.__init__` docstring documents the callback contract: signature,
  failure policy (swallow + log), invocation site (after `summary_written`).
- **B-D3** — `2-plan.md` D5 (FS report backup destruction) referenced from the
  `PostAnalysisCleanupTrigger` module docstring as the load-bearing accepted gap.
