# Task 4: Fallback Hook Script + JSONL Append (P1-8)

## Context

Read: overview.md (esp. "The hook's only durability promise is 'either the server got it or the JSONL file got it'")

This task ships the bash hook script that coding agents (Claude Code first; others adopt the same shape) call on every event. The script is a *thin HTTP client* — and a fallback writer when the server is unavailable. Failure modes are subtle:

- A `set -e` in a parent script combined with `curl` returning non-zero would crash the agent's tool call. **Hook is forbidden to fail loudly.**
- Bash subshell semantics can cause the JSONL append to be lost if stdin is closed.
- Two hooks firing in parallel may both append to the same JSONL → POSIX guarantees `O_APPEND` is atomic *for writes ≤ PIPE_BUF*, which is 4KB on Linux but **512 bytes** on macOS for some filesystems. Long payloads can interleave.

**Plan ref:** P1-8
**SD refs:** §3.9.2 (fallback design)

**Dependencies:** task-3 (the server endpoint must exist for the live-server path)

## Files

- Create: `scripts/hooks/_lib.sh` — shared helpers: `secondsight_post()`, `secondsight_fallback_append()`
- Create: `scripts/hooks/pre-tool-use.sh` — example for `PreToolUse`; demonstrates the pattern for adapters
- Create: `scripts/hooks/post-tool-use.sh`
- Create: `scripts/hooks/session-start.sh`
- Create: `scripts/hooks/session-end.sh`
- Create: `scripts/hooks/user-prompt.sh`
- Create: `tests/scripts/__init__.py`
- Create: `tests/scripts/test_hook_fallback.py` — Python tests that drive the bash scripts via `subprocess`

## Public Contract

```bash
# scripts/hooks/_lib.sh
#
# Source this from a per-event script. Provides:
#
#   secondsight_post EVENT_TYPE PAYLOAD_JSON
#     - POSTs to http://127.0.0.1:8420/hook/{EVENT_TYPE} with --connect-timeout 0.1
#     - On any non-zero exit (curl failure, server down, timeout):
#         calls secondsight_fallback_append; returns 0 ALWAYS.
#     - Honors $SECONDSIGHT_HOME (default: $HOME/.secondsight)
#
#   secondsight_fallback_append EVENT_TYPE PAYLOAD_JSON
#     - Builds an envelope wrapper: {agent, event_type, timestamp, payload}
#     - Atomic-appends one line to $SECONDSIGHT_HOME/fallback_events.jsonl
#     - Uses flock(1) where available; degrades to plain >> elsewhere.
#
# Each per-event script is structurally:
#
#   #!/usr/bin/env bash
#   set -u   # NOT set -e — we never want to crash the agent
#   . "$(dirname "$0")/_lib.sh"
#   PAYLOAD=$(cat)        # hook payload from stdin
#   secondsight_post "pre-tool-use" "$PAYLOAD"
#   exit 0
```

The fallback envelope on disk:

```json
{"agent":"claude-code","event_type":"pre-tool-use","timestamp":"2026-05-04T...","payload":{...raw hook input...},"hook_script_version":"phase-1.2"}
```

This shape is what P1-13 (`secondsight sync`) will consume to backfill.

## Death Test Requirements (write and verify red BEFORE production code)

1. **Hook exits non-zero on fallback.** Run the script with `SECONDSIGHT_PORT=1` (nothing listening). Assert: exit code is 0 (NOT 1, NOT non-zero). A non-zero exit from a Claude Code PreToolUse hook would *cancel* the tool call — observation must never be destructive.
2. **JSONL line truncated by parallel writes.** Spawn 50 hook script invocations in parallel, each with a 1KB payload, against a port with no listener. Assert: the JSONL has exactly 50 lines, each parses as valid JSON, and `payload` field is exactly the input. (Detects PIPE_BUF interleaving if not using flock.)
3. **JSONL append silently fails if directory missing.** Delete `~/.secondsight/` before running the script. Assert: directory is auto-created OR the script exits 0 with a stderr warning AND the loss is recorded somewhere observable. Silent loss → fail.
4. **Server returns 5xx → script falls through to fallback.** Stand up a fake server returning 500. Assert: script appends to fallback JSONL (treats 5xx as "server didn't get it"). Without this, a degraded server eats events while looking healthy.
5. **Curl absent → fallback.** Mock `PATH` to omit curl. Assert: script detects the missing dependency, appends to fallback (or logs a structured warning), and exits 0.
6. **Hook payload contains shell metacharacters.** Pass a payload with `'`, `"`, `$`, backticks, newlines. Assert: it round-trips through JSONL without corruption (jq parse confirms equality).
7. **set -e in parent process.** Wrap the hook in a parent shell with `set -e`; force a fallback. Assert: parent does NOT abort. (Since we explicitly don't `set -e` and always `exit 0`, this should hold — but the test pins it.)
8. **Concurrent fallback + truncation race.** While one process is appending a 4KB line, run another process that calls `secondsight sync` (placeholder; in this change just simulate by truncating the file mid-append). Assert: the JSONL never ends up corrupted such that `jq -c .` over each line fails.

## Unit Test Requirements (Python-driven shell tests)

- Live-server happy path: stand up a real `create_app()` server on a random port, run hook → assert one event in events table + one file in raw trace store.
- No-server path: kill the server; run hook → assert one line appended to fallback JSONL.
- 5xx path: fake server returns 500 → assert fallback JSONL has a line.
- Concurrent live-server: run 100 hooks in parallel → 100 rows in DB, 0 lines in JSONL.
- Concurrent no-server: 100 hooks in parallel against dead port → 100 lines in JSONL, all valid JSON.
- Shellcheck-clean: `shellcheck scripts/hooks/*.sh` produces zero findings (run as a CI gate).

## Implementation Steps

- [ ] Step 1: STEP 0 — answer the four prerequisite questions
- [ ] Step 2: Write death tests (Python subprocess harness driving bash)
- [ ] Step 3: Run death tests — red
- [ ] Step 4: Write unit tests
- [ ] Step 5: Run unit tests — red
- [ ] Step 6: Implement `_lib.sh` with `secondsight_post` and `secondsight_fallback_append` (use `flock -x` on the JSONL where flock is available; jq for envelope construction)
- [ ] Step 7: Implement the per-event scripts (each is ~5 lines, sourcing `_lib.sh`)
- [ ] Step 8: Add a `tests/scripts/conftest.py` fixture that spawns a uvicorn instance on a random port (or fakes it)
- [ ] Step 9: Run all tests — green
- [ ] Step 10: Write scar report
- [ ] Step 11: Self-iteration (Level 1)
- [ ] Step 12: Re-run tests — no regression

## Expected Scar Report Items

- Potential silent failure: macOS doesn't ship `flock(1)`; we degrade to plain `>>` and accept the documented PIPE_BUF interleaving risk for >512 byte writes. Document and consider a Python-based fallback for large payloads in P1-13.
- Potential silent failure: `--connect-timeout 0.1` may be too aggressive on slow systems (cold-start from sleep) and produce false fallbacks. Acceptable in Phase 1 — fallback is loss-free.
- Assumption to verify: hook payload is on stdin (Claude Code) vs. argv (other agents). For Phase 1 we assume stdin; P1-9 (Claude Code adapter) will confirm.
- Potential shortcut: `jq` is a hard dependency. We could implement envelope construction in pure bash, but jq's correctness for shell-metacharacter-laden payloads is worth the dep. Document.
- Boundary issue: `$SECONDSIGHT_HOME` resolution — if user sets it to a relative path, behavior is unspecified. Validate at top of `_lib.sh`.

## Acceptance Criteria

- All death tests pass
- All unit tests pass
- `shellcheck` clean
- Hook script exits 0 in **every** failure mode tested
- Fallback JSONL line is parseable by `jq` and matches the documented envelope shape
- No bare `set -e` in any hook script (we manage exit explicitly)
