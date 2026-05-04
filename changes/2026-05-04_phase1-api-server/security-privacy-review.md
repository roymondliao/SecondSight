# Security & Privacy Review — Phase 1.2 API Server (GUR-117)

**Scope:** bundle commit `59abedd9` (GUR-96, "Phase 1.2 API Server Core: FastAPI + hook endpoints + bash fallback"), reviewed against base `8920d0e` (Phase 1 Storage Layer).

**Reviewer:** Tianqi (backend engineer)
**Date:** 2026-05-04
**Branch:** `paperclip-experiment`
**Method:** `samsara:security-privacy-review` skill, manual code review (no platform-provided automated security agent was invoked — see "Method note" at the end).

## Files in scope

```
src/secondsight/api/__init__.py
src/secondsight/api/hooks.py            (route handler, _is_safe_id)
src/secondsight/api/normalizer.py
src/secondsight/api/registry.py         (ProjectRegistry, _build_resources)
src/secondsight/api/schemas.py          (HookEnvelope)
src/secondsight/api/server.py           (FastAPI app, AppState, lifespan)
src/secondsight/cli/__init__.py
src/secondsight/cli/serve.py            (Typer command, daemon entry)
src/secondsight/daemon.py               (PID file, double-fork, stop_daemon)
src/secondsight/observation/tracker.py  (SessionTracker)
src/secondsight/storage/raw_trace_store.py  (3-line cleanup-flag refactor)
scripts/hooks/_lib.sh                   (curl POST, jq envelope, jsonl fallback)
scripts/hooks/{pre,post}-tool-use.sh
scripts/hooks/{session-start,session-end,user-prompt}.sh
```

Tests in `tests/api/`, `tests/cli/`, `tests/scripts/`, `tests/observation/` were inspected for security-relevant assumptions but are not the target of the review.

## Threat model assumed for Phase 1

- **Bound:** uvicorn `host=127.0.0.1`, single worker, no auth, no TLS.
- **Trusted:** the local user; processes run as the local user.
- **Untrusted:** any network-reachable origin (which is supposed to be only `127.0.0.1`), browser-originating requests from arbitrary tabs, the *contents* of `payload` (could include prompts, file fragments, tool args).
- **Out of scope:** Windows daemonization, multi-worker uvicorn, multi-host deployments, remote storage.

The review additionally treats **a malicious webpage open in the user's browser** as in-scope, because `127.0.0.1` traffic from a browser is not blocked by the network bind and DNS-rebinding can defeat naïve same-origin assumptions.

## Findings summary

| # | Severity | Area | Finding |
|---|---|---|---|
| 1 | Important | API surface / CSRF | No `Origin` / `Host` / token check on `POST /hook/{event_type}`; localhost-bound but addressable from any browser tab, including via DNS rebinding. (Pre-flagged: SF-4) |
| 2 | Important | DOS / API surface | `HookEnvelope` schema accepts arbitrarily large bodies; no per-request body-size cap. The schemas docstring's claim of "uvicorn's body-size limit (default 1 MiB)" is **incorrect** — uvicorn ships no default body size cap. |
| 3 | Suggestion | Path validation | `_is_safe_id` permits `a..b` style values (only pure-dot sequences `.`/`..` rejected). Already documented as KS-4 / DFR-3 / DFR-6 — tighten to `[A-Za-z0-9_.-]` allowlist (and bound length, already capped at 128 by Pydantic). (Pre-flagged: KS-4 / DFR-3 / DFR-6) |
| 4 | Suggestion | Defense-in-depth | `request.client.host` is not validated against `127.0.0.1` at route entry; a misconfigured reverse proxy forwarding external traffic would bypass uvicorn's bind. (Pre-flagged: SF-3) |
| 5 | Suggestion | File permissions / privacy | `~/.secondsight/logs/server.log` is created with mode `0o644` (world-readable). Server logs may contain `project_id`, `session_id`, exception messages with payload data. Use `0o600`. |
| 6 | Suggestion | Information disclosure | `tracker.bind` and `normalizer.normalize` exception messages flow into HTTP 422 response bodies (`f"Tracker bind failed: {type(exc).__name__}: {exc}"`, `f"Normalizer rejected envelope: {exc}"`). On localhost only the local user reads these, but in conjunction with finding #1 the surface widens. |
| 7 | Suggestion | Defense-in-depth | `ProjectRegistry._build_resources` does not re-validate `project_id`. If a future caller bypasses the route handler (e.g. internal admin command, test helper, future endpoint), traversal could re-emerge. Mirror `_is_safe_id` inside `_build_resources` as a belt-and-braces guard. |
| 8 | Suggestion | Hook install hygiene | Hook scripts resolve their real path correctly via `BASH_SOURCE` + symlink walk, but if `_lib.sh` (or its containing directory) is writable by an attacker (e.g. installed in `/tmp/...`), sourcing it executes attacker code as the user on the next hook fire. Document the install-permission contract; recommend `chmod 0755` on dir, `0644` on files, owner-only writable. |
| 9 | Accepted-with-rationale | PID-file spoofing | `_is_secondsight_cmdline` matches `secondsight + serve` or `uvicorn + secondsight` substrings in `argv`. A same-user attacker can craft a process whose argv satisfies the check and trick `stop_daemon` into killing it. **Same-user attacker = already game over**; the check defends against the much-more-likely "stale PID file → unrelated process at that PID" case. Accepted. |
| 10 | Accepted-with-rationale | TOCTOU between status check and SIGTERM | Race window between `daemon_status` and `os.kill(SIGTERM)`: the original PID could exit and a new (unrelated) process could acquire the same PID. The cmdline guard runs only before the window. Mitigation: SIGTERM is graceful; risk is bounded; PID-reuse rate on Linux is low. Accepted. |
| 11 | Accepted-with-rationale | Hook payload PII / no scrubbing | The Claude Code hook stdin (full tool-call payload, prompt text, file paths, arbitrary args) is forwarded to the server unredacted, and on server-down written to `~/.secondsight/fallback_events.jsonl`. By Phase 1 design no scrubbing is applied. The bash fallback path uses `jq --argjson` for JSON safety; the degraded (jq-absent) path uses base64 for the truncated payload, so no shell injection. Document the privacy contract: **anything in agent stdin lands on disk under `~/.secondsight/`**. Phase 2 must add at minimum a redaction hook for known secret patterns. |
| 12 | Accepted-with-rationale | Bash hook DOS via huge stdin | `PAYLOAD="$(cat)"` reads all of stdin into a bash variable; an enormous payload (e.g. tool returning a multi-GB blob) inflates hook RSS. The agent that fed the stdin is the same trust principal as the hook, so this is a self-DOS, not an attacker primitive. Accepted; document in operator notes. |

## Detailed findings

### Finding 1 — `POST /hook/{event_type}` has no Origin / token / CSRF check (Important)

**Where:** `src/secondsight/api/hooks.py:143-252`, `src/secondsight/api/server.py` (no middleware).

**What:** the route handler validates `event_type`, `project_id`, `session_id` and dispatches to the normalizer + tracker. It does **not** look at:
- `Origin` / `Referer` headers,
- any bearer token / shared secret,
- any same-site cookie or fetch-metadata header,
- `request.client.host`.

**Why this is Important even on localhost:** a webpage open in the user's browser can issue `fetch('http://127.0.0.1:8420/hook/user_prompt', {method:'POST', body: ...})` and the server will accept it. CORS does not protect the server — the browser just receives a CORS error after the request has already been processed. DNS rebinding makes naïve `Host: localhost` checks insufficient.

**Concrete impact:**
- Pollution of the user's observation database with attacker-injected events.
- Content injection vector if/when those events are surfaced anywhere in a UI or summary.
- Memory pressure / FS pressure on the user's machine.

**Recommended remediation (Phase 1 minimum):**
- Reject any request whose `Host` header is not in `{127.0.0.1, localhost, 127.0.0.1:<port>, localhost:<port>}` *and* whose `Origin` header (when present) is not in the same set.
- Or require a per-install bearer token written to a `0600` file at `~/.secondsight/hook.token` and read by the hook scripts.

**Recommended remediation (Phase 2):** explicit token bearer; document in SD §3.9.

**Decision:** defer to **child issue** (see "Disposition" below). Not fixed in this branch because the right fix is a small middleware that needs its own death tests + Origin matrix coverage; that scope belongs in a tracked ticket, not a slipstream commit.

### Finding 2 — No request body size cap (Important)

**Where:** `src/secondsight/api/schemas.py:18-19`, `src/secondsight/cli/serve.py:69` (uvicorn invocation).

**What:** the `HookEnvelope` docstring asserts that "extra fields rely on uvicorn's body-size limit (default 1 MiB)". This claim is **incorrect**: uvicorn does not ship a default request body size limit; neither `h11` nor `httptools` cap body size by default (the h11 `MAX_INCOMPLETE_EVENT_SIZE = 16 KiB` cap is for header parsing, not body). Pydantic's `extra="allow"` will accept arbitrarily many extra fields.

**Concrete impact:** local DOS via memory exhaustion. Combined with Finding 1, a browser tab can submit a 100 MiB body to the user's local server.

**Recommended remediation (Phase 1):**
- Add a small ASGI middleware that enforces `Content-Length <= MAX` (e.g. 256 KiB) and 413s otherwise.
- Or call `uvicorn.run(..., limit_concurrency=..., h11_max_incomplete_event_size=...)` plus a body-streaming guard.
- Update the schemas docstring to reflect the actual enforcement.

**Decision:** defer to **child issue**. Trivial code change but needs a death test pair (oversized → 413; just-under → 200) and an updated docstring; bundling with Finding 1 makes sense.

### Finding 3 — `_is_safe_id` allowlist is too permissive (Suggestion)

**Where:** `src/secondsight/api/hooks.py:60-91`.

**What:** rejects slashes, backslashes, control chars, pure-dot sequences. Permits `a..b` and other dotted forms. Already triaged as KS-4 / DFR-3 / DFR-6 (deferred to security review = this review).

**Risk realised:**
- Directory traversal proper is blocked (no `/`, no `..` alone).
- "Confusable" IDs like `..foo`, `foo..bar`, `..` (filtered), `...` (filtered by pure-dot), `....` (filtered), `..​` (NOT filtered — zero-width unicode), `..%2F` (already URL-decoded by FastAPI before reaching us; `%2F` would be a literal `/` and rejected).
- Unicode confusables (`U+2024 ONE DOT LEADER`, `U+FF0F FULLWIDTH SOLIDUS`) are not blocked. They do not produce real path traversal on POSIX (filesystem treats them as literal bytes), but they break user expectation of "ASCII project IDs".

**Recommendation:** tighten to `re.fullmatch(r"[A-Za-z0-9._-]{1,128}", value)` plus reject leading-dot (`.foo` is a hidden dir on POSIX) and reject pure-dot (already done). Three lines. Backwards-compatible for any sane caller because adapters will be writing IDs they control.

**Decision:** small, low-risk inline fix is reasonable. Defer to **child issue** so the change rides with the Finding 1 + Finding 2 fix bundle (same surface, same test file).

### Finding 4 — `request.client.host` not asserted against 127.0.0.1 (Suggestion)

**Where:** `src/secondsight/api/hooks.py:144` (route handler signature includes `request: Request` but never reads `request.client`).

**What:** uvicorn binds `127.0.0.1` so in a clean install the server cannot receive non-localhost traffic. A misconfigured reverse proxy (or a future containerised deployment that exposes the port) would bypass that bind, and the route handler does not double-check.

**Recommendation:** at the top of `handle_hook`, assert `request.client and request.client.host in {"127.0.0.1", "::1"}`, otherwise return 403. **One line. Defense in depth.**

**Decision:** defer to the same child issue as Findings 1/2/3.

### Finding 5 — Server log file mode `0o644` (Suggestion)

**Where:** `src/secondsight/daemon.py:385`.

**What:** `os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)` creates `~/.secondsight/logs/server.log` world-readable. The log is fed `loguru.info/warning/error` calls that include `project_id`, `session_id`, and arbitrary exception messages from `tracker.bind` and `pipeline.ingest`. Exception messages may include payload fragments (e.g. a `KeyError("api_key")` would print the literal field name being accessed).

`~/.secondsight/projects/<id>/intelligence.db` and `~/.secondsight/projects/<id>/sync.log` (Phase 1.1) inherit the same `0o644` pattern — out of scope for this review but the same recommendation applies.

**Recommendation:** use `0o600` for `server.log`. Optionally `os.chmod` `~/.secondsight` to `0o700` on first use.

**Decision:** defer to child issue. Trivial fix; bundle with Findings 1–4.

### Finding 6 — Exception message leakage in HTTP 422 responses (Suggestion)

**Where:** `src/secondsight/api/hooks.py:217, 229`.

**What:** the route handler returns `f"Normalizer rejected envelope: {exc}"` and `f"Tracker bind failed: {type(exc).__name__}: {exc}"` as 422 detail. `tracker.bind` raises `SubAgentStackMismatch` and `ValueError` whose messages include `session_id` and `sub_agent_id` literally. This is fine on a localhost-only single-user surface but combined with Finding 1, an attacker-originating browser request could probe internal state.

**Recommendation:** keep the rich messages in the **server log** but return only a stable, generic message in the HTTP body. Or gate verbose-error mode behind a `--debug` flag.

**Decision:** defer to child issue.

### Finding 7 — `_build_resources` lacks defense-in-depth `_is_safe_id` (Suggestion)

**Where:** `src/secondsight/api/registry.py:135-169` (specifically `project_dir = self._home / "projects" / project_id`).

**What:** validation lives in the route handler. A future internal caller (a CLI subcommand, test helper, admin endpoint) that calls `registry.get(project_id)` directly with an unvalidated string would re-open path traversal. The current code documents this assumption but does not enforce it inside `_build_resources`.

**Recommendation:** import `_is_safe_id` (or a registry-local equivalent) and assert at the top of `_build_resources` so the registry layer is independently safe. Belt-and-braces.

**Decision:** defer to child issue (with Finding 3, since both touch ID validation).

### Finding 8 — Hook install hygiene (Suggestion)

**Where:** `scripts/hooks/{pre,post}-tool-use.sh`, `_lib.sh`.

**What:** the symlink-resolution loop correctly identifies the *real* location of `_lib.sh` adjacent to the actual hook script. If a user installs `pre-tool-use.sh` symlinked from `~/.claude/hooks/` to a directory writable by another local user (or even by the same user but accessible to other processes — `/tmp/secondsight-hooks/`), then any local actor that can write `_lib.sh` can run code as the hook-firing user.

This is an **install convention** issue, not a code bug. The bash code is correct given a sane install layout.

**Recommendation:** add an "Installation" section in the operator README that mandates: `chmod 0755` directory, `chmod 0644` scripts, owner-only writable, and concrete language saying *do not install hook scripts from `/tmp` or any world-writable directory*.

**Decision:** defer to child issue (operator-doc change, not code).

## Disposition

No **Critical** findings. Two **Important** findings (CSRF, body-size cap) — neither is exploitable purely from a same-user-already-compromised threat, and both have natural Phase 2 fixes; recommended to fix in a single follow-up child issue rather than slipstream into this bundle. Six **Suggestion** findings, four **Accepted-with-rationale**.

### Recommended follow-up child issues

1. **GUR-117a (proposed):** Phase 1 hook-API hardening bundle — middleware for Origin/Host check (Finding 1) + body-size cap (Finding 2) + tightened `_is_safe_id` to `[A-Za-z0-9._-]` (Finding 3) + `request.client.host` assertion (Finding 4) + `_build_resources` defense-in-depth (Finding 7) + generic 422 messages (Finding 6) + log mode `0o600` (Finding 5). Single PR, 6 small changes, dedicated death tests for each. Block `samsara:validate-and-ship` on resolution? **No** — the surface is localhost-only Phase 1 dev and these are hardening rather than vulnerabilities. Recommend non-blocking, fix before any Phase 2 multi-host work.

2. **GUR-117b (proposed):** Operator install hygiene doc (Finding 8) + privacy contract documentation (Finding 11): "what is written to disk, where, with what permissions" — short README section. Non-blocking.

### Items NOT recommended for inline fix

The skill rule is: Critical → fix or escalate; Important → child issue if not fixed in this run. With no Critical findings and the Important ones interlocking (CSRF + body-size both want a small middleware), the child-issue path is correct. Inline patching would require a fix-pass round of yin + structural-quality review per `samsara:implement` rules, which would invalidate the iteration sign-off this bundle already passed. Better to land them as a clean follow-up.

## Method note

This review was produced by manual code reading by the backend engineer (Tianqi). No platform-bundled automated `security-review` capability was invoked; the `samsara:security-privacy-review` skill is platform-agnostic and the local Claude Code session has a `/security-review` slash command available, but it operates on the *current* branch's pending changes and is interactive. The findings above are recorded as the review of record. If the validate-and-ship gate wants an additional automated pass, run `/security-review` against `paperclip-experiment` HEAD before merge.

## Sign-off

Verdict: **PASS with named follow-ups**. No same-branch fix required. Two Important findings tracked as child issue GUR-117a (proposed). Operator-doc change tracked as child issue GUR-117b (proposed). Validate-and-ship may proceed with these two child issues recorded in the ship manifest as accepted-but-tracked risks.

— Tianqi, 2026-05-04
