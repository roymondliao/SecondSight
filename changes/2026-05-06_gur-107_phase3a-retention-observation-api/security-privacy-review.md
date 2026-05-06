# Security & Privacy Review — GUR-147 Bundle

**Verdict:** PASS_WITH_ONE_MEDIUM — 0 HIGH, 1 MEDIUM finding (defense-in-depth recommendation), 0 risks accepted that block ship.

**Reviewed:** bundle commit range `3278492..06eb44f` (GUR-147
implementation, 7 tasks A1–A7). Phase 1 surfaces (events table,
RawTraceStore write path, adapter layer) are out of scope — covered
by `Security & privacy review: GUR-96 bundle` (commit 553a739) and
`Security & privacy review: GUR-97 bundle` (commit a170aea).

## Scope (in-scope production files)

- `src/secondsight/storage/retention.py` (new — RetentionConfig,
  ExpiredSession, enumerate_expired_sessions, RawTracesPurger,
  PurgeFailure, PurgeResult)
- `src/secondsight/api/observation.py` (new — Pydantic schemas + 4
  GET endpoints + ETag helpers)
- `src/secondsight/api/server.py` (3-line change to mount
  observation router)
- `src/secondsight/cli/cleanup.py` (new — Typer subcommand)
- `src/secondsight/cli/app.py` (1-line change to register cleanup
  subcommand)
- Tests: `tests/storage/test_retention_{config,enumerator,purger}.py`,
  `tests/api/test_observation_{schemas,router,pagination}.py`,
  `tests/cli/test_cleanup.py`

## Method

Manual code review against the OWASP-style threat surface map
implied by GUR-147's components:

- **HTTP endpoints (new):** authentication, authorization,
  parameter validation, info disclosure, ETag side-channels,
  pagination cursor opacity.
- **Filesystem deletion:** path traversal, symlink races, DB/FS
  drift, idempotency.
- **DB DELETE:** parameterization, transaction scope, partial
  failure handling.
- **CLI destructive command:** confused-deputy, exit-code honesty,
  shell injection in arguments.
- **TOML config loader:** untrusted-file parsing, schema validation
  (DC-6).

Plus precedent compliance: did the bundle preserve invariants
established in earlier security reviews (parameterized SQL, no PII
in logs, no hardcoded secrets)?

## Findings

### MEDIUM-1: Path-traversal defense-in-depth gap in `RawTracesPurger`

**File:** `src/secondsight/storage/retention.py:269-281`
(`_delete_fs_session`).

`session_dir = store.project_root / "sessions" / session_id`
followed by `shutil.rmtree(session_dir)`. The `session_id` arrives
from the events table via `enumerate_expired_sessions`, which
trusts whatever is in the DB. `RawTraceStore` enforces a strict
character regex `^[A-Za-z0-9_\-:.]+$` at write time
(raw_trace_store.py:30), so under the current adapter chain no
path-traversal session_id can ever land in the DB.

**Why it's still worth flagging:** the purger is the single most
destructive operation in the bundle (`shutil.rmtree`) and it is
the only consumer that does NOT re-validate `session_id` before
treating it as a path component. Future adapters, DB tampering,
or a writer that bypasses RawTraceStore would land in this code
path with no second line of defense. The `RawTraceStore` class
already exposes the regex constant; calling
`_SAFE_SESSION_ID.fullmatch(session_id)` (or extracting it as a
public helper) before `rmtree` would close the gap at near-zero
cost.

**Severity:** MEDIUM (defense-in-depth, not exploitable under
current invariants). Threat model is constrained by the local-only
bind (memory `dashboard_api_contracts`) and the single-writer
RawTraceStore.

**Recommended fix (deferred to a follow-up issue, not blocking
ship):** add a regex assert in `_delete_fs_session` and/or have
`RawTraceStore` expose a public `safe_session_id_path(session_id)`
helper that the purger uses. Track as a hardening item.

## Threat surface verification (no findings, recorded for ship-manifest visibility)

### HTTP endpoints (`api/observation.py`)

- ✅ **DC-4 enforcement.** Every endpoint declares
  `project_id: str = Query(..., min_length=1, max_length=128)`
  with no default. FastAPI returns 422 automatically if absent.
  Cross-project enumeration is impossible by construction
  (`api/observation.py:426, 472, 489, 521`).
- ✅ **Parameterized SQL throughout.** All read-side aggregation
  uses SQLAlchemy Core `sa.select(...).where(col == param)`. No
  `text()`, no string concatenation, no f-string SQL.
- ✅ **ETag uses SHA-1 with `usedforsecurity=False`** — the
  documented stdlib pattern for non-cryptographic digests
  (`observation.py:365`). Truncated to 16 hex chars; collision
  domain is per-project per-process which is fine for a freshness
  marker.
- ✅ **Cursor opacity preserved.** The pagination cursor is
  `str(offset)`, not a SQL row id or signed token; the schema
  layer does not parse it. The cursor-vs-offset mutex check at
  `_resolve_offset` rejects ambiguity with 422 rather than
  silently picking one.
- ✅ **No JSON parse on listing endpoints.** Listing (D7) reads
  table columns only; only `_get_segment_detail` opens
  `events.data`. The dashboard threat surface for "user can post
  weird JSON to attack our parser" stays at zero on the heavy
  endpoint.
- ✅ **Auth scope = local-only bind.** Per memory
  `dashboard_api_contracts` the server binds `127.0.0.1`; the
  Observation API does not introduce its own auth and inherits the
  Phase 1 trust model. Verified via
  `api/server.py:55` (host="127.0.0.1" default).

### FS deletion + DB DELETE (`storage/retention.py:RawTracesPurger`)

- ⚠️ See MEDIUM-1 above.
- ✅ **FS-first / DB-second order intentional and documented**
  (retention.py:296-310). Partial failure (FS gone, DB DELETE
  fails) is acknowledged in D3 and surfaces as a structured
  ERROR log + DC-5 PurgeFailure in the result.
- ✅ **Per-session try/except** prevents one bad session from
  poisoning the batch. DC-5 propagation through CLI exit code
  verified by `tests/cli/test_cleanup.py`.
- ✅ **`shutil.rmtree` is idempotent** — `_delete_fs_session`
  returns False rather than raising on missing dir, allowing
  manual cleanup followed by `secondsight cleanup` to converge.
- ✅ **Structured ERROR logging** with `session_id` only. No
  `event.data`, no event content; PII surface contained.
- ✅ **Parameterized DELETE** via
  `sa.delete(events).where(events.c.session_id == session_id)`
  (retention.py:291). No SQL injection surface.

### CLI (`cli/cleanup.py`)

- ✅ **Project enumeration via FS walk** (line 266-273) — only
  yields existing directories. No untrusted-string-as-path
  surface beyond what the Phase 1 adapter chain already produces.
- ✅ **`--project-id` CLI override** is operator-typed; threat
  model is "operator typo creates an empty project dir".
  Self-DoS, not a security finding.
- ✅ **No subprocess invocations**, no shell strings, no
  shell-execution APIs.
- ✅ **`--dry-run` and real-run share `_enumerate_for_project`
  helper** (DC-3). Operator-trust contract holds.
- ✅ **Exit-code honesty.** `any_failure` flag aggregated across
  enumeration errors AND purge failures; CLI exits non-zero
  whenever the operator should look (verified by
  `tests/cli/test_cleanup.py`).
- ✅ **JSON output sorts keys, escapes content** (line 223).

### TOML config loader (`storage/retention.py:RetentionConfig`)

- ✅ **stdlib `tomllib`** — no third-party TOML parser, no
  evaluation surface.
- ✅ **DC-6 enforcement.** Malformed TOML, wrong type, or
  non-positive integer all raise `RetentionConfigError` with the
  offending source path in the message. Verified by 4 death
  tests in `test_retention_config.py`.
- ✅ **DC-6b enforcement.** Missing files return built-in default
  without raising; fresh-install path verified by 3 death tests.
- ✅ **No code execution paths.** Loader returns a frozen
  dataclass; no eval / exec / import-from-config.

### Test code

- ✅ No hardcoded credentials, tokens, or secrets in any new
  test fixture. All tests use `tmp_path` for FS scratch; no
  cross-test pollution surface.

## Risks accepted

None that block ship.

## Carry-forward

Items deferred to follow-up issues (not blocking GUR-147 ship):

- **MEDIUM-1 path-traversal defense-in-depth** — file new issue
  `GUR-147 follow-up: harden RawTracesPurger session_id
  validation`. Not blocking ship because the threat is
  unrealizable under current invariants (single-writer
  RawTraceStore + adapter validation), but the gap should be
  closed before any new writer is added.
- **GUR-107b** — `analysis_ttl_days` enforcement + post-analysis
  trigger remain blocked on **GUR-101 analysis orchestrator**
  (per `e3c7e9ee` CEO advisory and `06eb44f` task-A7 commit).
  Out of scope for this ship.
- **`secondsight cleanup` does not validate `--project-id` as a
  safe path component** — operator-typed value, self-DoS only.
  Not security-relevant under the threat model.

## Pre-existing flakes (not regressions)

- `tests/scripts/test_hook_fallback.py::test_dt2_parallel_writes_no_truncation`
  flakes under full-suite parallel pressure; passes in isolation.
  Pre-existing from Phase 1 (recorded in
  `changes/2026-05-04_phase1-adapters/scar-reports/`). Not
  introduced by GUR-147.
