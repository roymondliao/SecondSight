# Security & Privacy Review — GUR-147 Bundle (revised)

**Verdict:** PASS — 0 HIGH, 0 MEDIUM, 0 LOW after hardening commit.
0 risks accepted.

**History:** the prior revision of this document landed at commit
`bec91ce` with verdict `PASS_WITH_ONE_MEDIUM`. A follow-up review
(this document) on the same diff range surfaced two additional findings
that the first pass missed:

- **HIGH-1:** Observation API `project_id` Query parameter had only
  `min_length=1, max_length=128` validation; arbitrary characters
  reached `ProjectRegistry._build_resources()` which uses `project_id`
  as a directory name. A request like
  `GET /api/sessions?project_id=../../tmp/pwn` would `mkdir` and create
  a SQLite DB outside the SecondSight home root. Asymmetric with the
  hooks router which already had `_is_safe_id` enforcement.
- **MEDIUM-1 (cleanup CLI `--project-id`):** the same path-traversal
  class via the operator boundary. Treated as accepted-self-DoS in the
  prior review; on second review, treated as a unified surface with
  HIGH-1 since both flow through `_build_resources`.
- **MEDIUM-2 (purger `_delete_fs_session`):** the prior MEDIUM-1
  finding (now MEDIUM-2). Defense-in-depth gap before `shutil.rmtree`.

The board (`cd574a9a`) chose option (B): pause ship, fix all three on
a fresh commit, re-review. This document is the re-review.

**Reviewed:** bundle commit range `3278492..HEAD` (GUR-147 A1–A7 plus
the hardening commit).

## Scope (in-scope production files, post-fix)

- `src/secondsight/api/_id_safety.py` (new — shared `is_safe_id` helper)
- `src/secondsight/api/hooks.py` (refactor — imports shared helper)
- `src/secondsight/api/observation.py` (HIGH-1 fix — calls `is_safe_id`
  in `_aresources`)
- `src/secondsight/api/server.py` (unchanged from prior revision)
- `src/secondsight/cli/cleanup.py` (MEDIUM-1 fix — validates
  `--project-id` at the CLI boundary)
- `src/secondsight/storage/raw_trace_store.py` (exposes public
  `is_safe_session_id` helper)
- `src/secondsight/storage/retention.py` (MEDIUM-2 fix —
  `_delete_fs_session` re-validates `session_id` before
  `shutil.rmtree`)
- Tests: `tests/storage/test_retention_{config,enumerator,purger}.py`,
  `tests/api/test_observation_{schemas,router,pagination}.py`,
  `tests/api/test_id_safety.py` (new), `tests/cli/test_cleanup.py`

## Method

Same as prior pass: manual code review against the OWASP-style threat
surface map (HTTP endpoints, FS deletion, DB DELETE, CLI destructive
command, TOML loader). On this pass, additional attention to the
asymmetry between the hooks router (`_is_safe_id` enforced) and the
observation router (no such check).

## Findings (post-fix)

### HIGH-1 — RESOLVED

**Was:** Observation API `_aresources` accepted any `project_id`
matching `min_length=1, max_length=128` and forwarded it to
`state.registry.get(project_id)` → `_build_resources()` →
`(self._home / "projects" / project_id).mkdir(parents=True,
exist_ok=True)`. Path traversal characters created directories outside
the SecondSight home root.

**Fix:** `_aresources` now calls `is_safe_id(project_id)` (extracted
shared helper at `src/secondsight/api/_id_safety.py`) and raises
`HTTPException(status_code=422)` on unsafe values BEFORE registry
materialisation. Verified by `tests/api/test_id_safety.py` parametric
cases (`../../tmp/pwn`, `..`, `x/y`, `x\\y`, `null\\x00byte`, `.`)
plus a no-traversal-side-effect test that confirms no escape directory
is created on the host filesystem.

**Same gate now applies to all four endpoints** (sessions list,
sessions detail, segments list, segment detail) since they all flow
through `_aresources`.

### MEDIUM-1 — RESOLVED

**Was:** `secondsight cleanup --project-id PID` forwarded `PID`
unchecked to `home / "projects" / project_id` and to
`ProjectRegistry._build_resources()`.

**Fix:** `cleanup()` now calls `is_safe_id(project_id)` immediately
after resolving `home_path`; on mismatch, prints a stderr message and
exits 2 (Click usage-error convention). Verified by
`tests/cli/test_cleanup.py::TestProjectIdSafetyGate` parametric cases.

### MEDIUM-2 — RESOLVED

**Was:** `_delete_fs_session(store, session_id)` did
`shutil.rmtree(store.project_root / "sessions" / session_id)` without
re-validating `session_id` against the regex enforced at write time.

**Fix:** `RawTraceStore` exposes a public `is_safe_session_id` helper
(same regex as `_SAFE_SESSION_ID`). `_delete_fs_session` now calls it
at the top and raises `ValueError("unsafe session_id ...")` on
mismatch; the per-session try/except in
`RawTracesPurger.purge()` surfaces this as
`PurgeFailure(stage="filesystem")` rather than rmtree-ing. Verified by
`tests/storage/test_retention_purger.py::TestUnsafeSessionIdRefused`,
which constructs a synthetic `ExpiredSession(session_id="../../escape")`
and asserts both the failure shape and that no escape directory was
created.

## Threat surface verification (carried from prior review, still green)

### HTTP endpoints (`api/observation.py`)

- ✅ **DC-4 enforcement.** `project_id: Query(..., min_length=1,
  max_length=128)` with no default. FastAPI 422 if absent.
- ✅ **HIGH-1 hardening.** `is_safe_id` rejects path-traversal
  characters at the `_aresources` boundary (this revision).
- ✅ **Parameterized SQL throughout.**
- ✅ **ETag uses SHA-1 with `usedforsecurity=False`.**
- ✅ **Cursor opacity preserved.**
- ✅ **No JSON parse on listing endpoints.**
- ✅ **Auth scope = local-only bind.**

### FS deletion + DB DELETE (`storage/retention.py:RawTracesPurger`)

- ✅ **MEDIUM-2 hardening.** `_delete_fs_session` re-validates
  `session_id` (this revision).
- ✅ **FS-first / DB-second order intentional and documented.**
- ✅ **Per-session try/except** prevents poison-batch.
- ✅ **`shutil.rmtree` is idempotent** for missing dirs.
- ✅ **Structured ERROR logging** with `session_id` only — no
  `event.data`, no event content.
- ✅ **Parameterized DELETE.**

### CLI (`cli/cleanup.py`)

- ✅ **MEDIUM-1 hardening.** `is_safe_id(--project-id)` at the CLI
  boundary (this revision).
- ✅ **Project enumeration via FS walk** — only yields existing dirs.
- ✅ **No subprocess invocations.**
- ✅ **`--dry-run` and real-run share `_enumerate_for_project`** (DC-3).
- ✅ **Exit-code honesty.**

### TOML config loader (`storage/retention.py:RetentionConfig`)

- ✅ stdlib `tomllib`, no eval, DC-6 + DC-6b enforced.

### Test code

- ✅ No hardcoded credentials. All tests use `tmp_path`.

## Risks accepted

None.

## Carry-forward

- **GUR-107b** — `analysis_ttl_days` enforcement + post-analysis
  trigger remain blocked on **GUR-101 analysis orchestrator**. Out of
  scope for this ship.

## Pre-existing flakes (not regressions)

- `tests/scripts/test_hook_fallback.py::test_dt2_parallel_writes_no_truncation`
  flakes under full-suite parallel pressure; passes in isolation.
  Pre-existing from Phase 1.
