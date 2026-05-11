# Planning Pre-thinking: Information Assumptions

## To write this plan, I am assuming:

- **[architecture]** The smallest viable surface for "post-analysis event" is a constructor-injected `on_analysis_complete: Callable[[str], None] | None` invoked once at end of `Orchestrator.analyze_session` after stage transitions to `summary_written`. (Confirmed by board comment 6578b671: "Confirmed.")
- **[targets]** TTL purger reaps `session_reports` (one row per session) + `behavior_flags` (many per session). `analysis_runs` audit rows are out of scope. (Confirmed.)
- **[default]** `cleanup_after_analysis = false` by default; opt-in only. (Confirmed — SD §3.10.1 sets `analysis_ttl_days = 365` precisely so analysis outlives raw_traces.)
- **[boundary timestamp]** TTL boundary uses `session_reports.created_at` (preserved on upsert per repo docstring lines 5-7) NOT `updated_at`. Re-running analysis on a session must not extend the TTL.
- **[FK absence]** No DB-level FK constraint between `behavior_flags` and `session_reports`; verified via `Grep -p "Foreign|REFERENCES" --glob storage/*.py` returning zero matches. Cleanup ordering is policy-driven, not DB-enforced.
- **[reuse]** Eager-on-completion raw_traces cleanup reuses GUR-147's `RawTracesPurger.purge()` by synthesizing one `ExpiredSession` from the just-completed `session_id`. No new destructive primitive.
- **[config plumbing]** `analysis_ttl_days` follows the same precedence as `raw_traces_ttl_days` (per-project → global → builtin). Returned as a new field on the existing `RetentionConfig` dataclass with paired `analysis_ttl_source`. Builtin default = 365 (SD §3.10.1).

## Gaps I cannot resolve from Research:

- **[gap-fs-collision]** The orchestrator's filesystem backup (`{home}/projects/{project_id}/sessions/{session_id}/session_report.json`) lives **inside** the same `sessions/{session_id}/` tree that `RawTracesPurger._delete_fs_session` `shutil.rmtree`s. Eager raw_traces cleanup will also wipe the report-backup JSON.
  - **Question for board:** is the report-backup JSON expected to survive eager raw_traces cleanup, or is the DB row authoritative and the FS backup is consensually destroyed?
  - **Default I'll carry forward (subject to revision):** the DB row is authoritative; the FS backup is a secondary cache for tools that bypass the DB. Eager cleanup destroying it is **acceptable** when the operator opted in via `cleanup_after_analysis = true`. This will be documented as an accepted gap in the Tech Spec.

- **[gap-callback-arity]** Should the callback receive only `session_id`, or the full `AnalyzeSessionResult`?
  - **Default I'll carry forward:** `Callable[[str], None]` with `session_id` only. Keeps the surface minimal and matches what the cleanup consumer needs. If GUR-101 later grows a real event with richer payload, the callback signature widens via overload — non-breaking.

- **[gap-callback-failure-policy]** If the callback raises (e.g., FS purge fails after analyze_session succeeds), should the orchestrator propagate the exception, swallow + log, or roll back the analysis?
  - **Default I'll carry forward:** swallow + log ERROR. The analysis itself succeeded; cleanup is a downstream concern that must not retroactively poison a successful analysis. The next scheduled CLI cleanup will retry the reap. (Mirrors GUR-147 D3 partial-failure stance.)

## Uncertainties:

- **[uncertainty-cli-flag-name]** Should the CLI accept `--analysis-only`, `--include-analysis`, or auto-include analysis cleanup whenever both TTLs are set? Marking this as nice-to-have for the kickoff; the planning sketch defaults to **auto-include** (CLI runs both purgers per project, each gated by its own resolved TTL). No new flag added.

## Output state

**Gaps exist** ([gap-fs-collision], [gap-callback-arity], [gap-callback-failure-policy]) but each carries a default I will carry forward and surface as an undocumented assumption in the Tech Spec. Per Step 1.5: this is the "accept gap" path. Surface in the planning gate comment so the board has the chance to revise before implementation locks them in.
