# Security & Privacy Review — GUR-103

**Commit reviewed:** `d47c89f` (single GUR-103 commit; diff scope limited to this commit only — prior issues already shipped + reviewed).
**Verdict:** PASS_WITH_CONCERNS — 0 Critical, 0 High, 3 Medium, 4 Low.
**Date:** 2026-05-08

## Summary

The LLM-leak surface (`read_project_file`) was the highest-priority focus area
and is solidly sandboxed: construction-time root resolution, post-resolve
`is_relative_to` containment re-check, component-level case-insensitive
denylist (with ancestor-directory matching), full-file binary detection
before truncation, and the D8 kill switch. Router logging schema avoids
prompt content; SQL is parameterized via SQLAlchemy Core; pipeline
callbacks are correctly fire-and-forget. No silent rot paths.

The Medium findings are not security bypasses — they are functional CLI
wiring bugs that fail closed (TypeError) rather than open. The reviewer
explicitly noted: "there is no path where security controls are bypassed
silently. The fix is mechanical."

## Resolved this round (commit on top of d47c89f)

- **Medium #1 — `cli/analyze.py:376-382` AnalysisTools wrong kwargs.**
  Fixed: changed `behavior_flags_repo=` → `flags_repo=`; replaced `config=`
  with explicit `extra_denylist=`, `size_cap_bytes=`, and
  `read_project_file_enabled=` plumbed from `analysis_config`. Verified by
  running `tests/cli/test_analyze.py` (8/8 pass).

- **Medium #2 — `cli/analyze.py:357` AnalysisConfig.load positional arg.**
  Fixed: changed `AnalysisConfig.load(project_config_path)` →
  `AnalysisConfig.load(config_path=project_config_path)`. The signature is
  keyword-only.

## Accepted risks (carry forward to validate-and-ship)

- **Medium #3 — `api/registry.py:175` private `_cache._values` access.**
  This is the same private-attribute pattern Samsara caught 4× during
  implementation; fixing it would require a new public `LazyCacheWithLocking.keys()`
  method. Documented in scar as deferred; the silent failure mode (Sweeper
  loses visibility) is bounded by operator log inspection.

- **Low #1 — `sdk/router.py:655` model-name validation.**
  Suggested defensive regex on model names to prevent an operator from
  embedding API key material in the model spec. Not a current threat surface
  (operators don't normally do this); deferred.

- **Low #2 — `cli/analyze.py:104-109` SECONDSIGHT_SERVER_URL env-var
  validation.** Operator-controlled env var; threat model is local-only.
  Adding scheme/host allowlisting is a hardening item for feature iteration.

- **Low #3 — `analysis/tools.py:343-349` redundant decode.** Performance
  micro-issue (full-file decode + slice re-decode); no security impact.
  Deferred.

- **Low #4 — `analysis/config.py:143` size_cap_kb upper bound.** A
  pathological config value (e.g., 1 TB) could let `read_project_file`
  attempt to read multi-GB into memory before truncation. Add a 16 MiB
  cap in feature iteration.

## Privacy considerations

- **Router logging schema** emits metadata only (`provider, model, tokens_in,
  tokens_out, duration_ms, attempt, total_attempts, outcome`) — no prompt
  or output content. The exception is `validation_error` in WARN-level
  terminal logs, which can include the failing field value (Pydantic's
  repr); this could surface LLM-generated content if the output schema
  validates against user-derived strings. Recommendation: route to
  operator-only log channel.
- **Sweeper logging** emits `project_id`, `session_id`, `last_event_ts`
  at WARNING. Confirmed via project memory that these IDs are not user-PII
  in this domain.
- **CLI HTTPStatusError handling** uses `httpx.HTTPStatusError` repr —
  status + URL only, no response body. No body leak.
- **`read_project_file` denylist** covers the documented set; misses a few
  common patterns worth adding via `extra_denylist` in operator config:
  `*.key`, `*.p12`, `*.pfx`, `.netrc`, `.npmrc`, `htpasswd`, `*.kdbx`.
  Not blocking; the extra_denylist mechanism is the documented escape valve.

## Follow-up scar item (not blocking ship)

Add a smoke test that exercises the in-process CLI orchestrator
construction end-to-end. This would have caught both Medium findings
before ship. Recommended for the next feature iteration touching the
CLI surface.

## Verdict transition

PASS_WITH_CONCERNS → after the two Medium fixes land, this advances to
PASS-equivalent for the validate-and-ship gate. The accepted-risk items
are documented for ship-manifest record.
