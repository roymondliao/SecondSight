# Security & Privacy Review — GUR-97 Phase 1.3 Agent Adapters

**Verdict:** PASS — No findings
**Reviewed commit:** `aa41a18` (Phase 1.3 Agent Adapters: AgentAdapter ABC + ClaudeCodeAdapter + Normalizer migration)
**Review date:** 2026-05-04
**Reviewer:** `samsara:security-privacy-review` skill via Claude Code platform `security-review` capability (parallel sub-agent identification + filtering pipeline)
**Predecessor:** `553a739` (GUR-117 — Security & privacy review of GUR-96 Phase 1.2 API server)

## Scope

- **Primary review target:** commit `aa41a18` only — Phase 1.3 work covering 38 files, +4664 / −315.
- **Branch-wide diff vs `main`:** 116 files, +17289 / −3 (8 commits ahead). Prior commits (GUR-93, GUR-96, GUR-111, GUR-112, GUR-116, GUR-117) were individually reviewed in their respective gates; re-flagging issues that originated there would be informational and is not blocking for this gate.
- **Methodology:** Phase-1 vulnerability identification sub-agent + Phase-2 false-positive filtering sub-agents (parallel) + confidence threshold ≥ 8/10 per `security-review` skill spec. Inputs include both the diff text and direct file reads of every listed source path.

### Files inspected (full source, not only diff)

```
src/secondsight/api/{server,hooks,schemas,registry}.py
src/secondsight/adapters/{base,claude_code,identity}.py
src/secondsight/storage/{raw_trace_store,events_repository,db_engine,sync_log,events_table}.py
src/secondsight/{daemon,event,__main__}.py
src/secondsight/cli/serve.py
src/secondsight/observation/{tracker,pipeline}.py
scripts/hooks/{_lib,session-start,session-end,user-prompt,pre-tool-use,post-tool-use}.sh
.github/workflows/{ci,install-smoke}.yml
```

## Findings

**0 HIGH-severity findings.**
**0 MEDIUM-severity findings.**
**0 risks accepted by board.**

Phase-1 sub-agent returned `NO_FINDINGS`. No vulnerabilities surfaced to Phase-2 false-positive filtering. No fix rounds were required.

## Categories examined

| Category | Reviewed surface | Verdict / evidence |
|---|---|---|
| Path traversal | `storage/raw_trace_store.py`, `api/hooks.py`, `api/registry.py` | Two-layer defense: `_is_safe_id` in hooks blocks `/`, `\`, control chars, and pure-dot ids; storage `_SAFE_SESSION_ID` regex `^[A-Za-z0-9_\-:.]+$` is stricter; `events_dir.resolve()` cross-checked against `project_root.resolve()`. Edge cases (`.`, `..`, `..foo`, leading/trailing dots) tested — all reject or resolve safely inside `project_root`. |
| SQL injection | `storage/events_repository.py`, `storage/events_table.py` | All queries use SQLAlchemy 2.0 Core parameterized statements. No string-interpolated user data in any `.execute()` call. Idempotent INSERT uses `ON CONFLICT DO NOTHING` with bound params. |
| Command injection | `daemon.py` | `subprocess.run(["ps", "-p", str(pid), ...])` uses argv form (no `shell=True`). `pid` parsed via `int()` before formatting. `cmdline_match` check before SIGTERM/SIGKILL prevents stale-PID kills against unrelated processes. |
| Shell injection | `scripts/hooks/{_lib,*}.sh` | Payload passed to `curl --data "$payload_json"` (argv, not shell-evaluated). `jq` invoked with `--argjson` (safe parse). jq-absent fallback writes only byte counts + base64; raw payload bytes never enter the JSON string body. |
| Unsafe deserialization | All Python source | Only `json.loads` and Pydantic `model_validate`. No use of unsafe Python serialization formats (object-graph deserializers, YAML full-loaders), no `eval`, no `exec`. |
| Auth / endpoint exposure | `api/server.py` | FastAPI binds to `127.0.0.1` only, single uvicorn worker, single-user model documented in SD §8.3 and confirmed in GUR-117. No authentication on `/hook/{event_type}` is by design (out-of-scope for single-user local observability per project threat model). |
| Pydantic envelope (`extra="allow"`) | `api/schemas.py` | Bounded by uvicorn default body-size limit. Extras do NOT flow into `payload` dict and are not written to disk via the adapter. |
| **Privacy: SD §3.7.4 drop_list** | `adapters/claude_code.py` | `DROP_LIST` (frozenset) excludes `tool_input` content, `tool_response` content, raw prompts, command bodies. Only metadata (`action_target`, sizes, `error_type`) flows into `Event.data`. |
| **Privacy canary** | `tests/fixtures/claude_code/*.json`, `tests/adapters/test_claude_code.py` | `PRIVACY_CANARY_DO_NOT_STORE` placed in drop-listed fields per fixture. `test_privacy_canary` asserts the canary string never appears in serialized `Event.data`. AC-6 green. |
| CI workflow security | `.github/workflows/{ci,install-smoke}.yml` | `permissions: contents: read`. No `pull_request_target`. No `script` injection vectors. No untrusted code executed against secrets. |

## Items considered and rejected as below the confidence/severity bar

| Item | Why not flagged |
|---|---|
| Daemon log file mode `0o644` | Server doesn't log payload bodies — only `event_id` strings and exception types. Excluded by review-skill category 2 ("secrets or sensitive data stored on disk are handled by other processes"). |
| `~/.secondsight/fallback_events.jsonl` writes raw payloads with default umask | Explicitly excluded by skill category 2. Fallback path is documented as user-local; not a privilege boundary crossing. |
| Local cross-user event injection on multi-user hosts via `127.0.0.1` | Documented single-user / single-process design (SD §8.3). No privilege boundary actually crossed beyond writing into the running user's own `~/.secondsight`. Same posture confirmed by GUR-117. |

## Comparison to predecessor (GUR-117)

| Aspect | GUR-117 (Phase 1.2 API server) | This review (Phase 1.3 Adapters) |
|---|---|---|
| Verdict | PASS with named follow-ups | PASS — no findings |
| Important findings | 2 (CSRF/Origin, body-size cap) | 0 |
| Suggestions | 6 | 0 |
| Accepted with rationale | 4 | 0 |
| Surface added | New endpoints, daemon control, hook ingestion | Adapter normalization layer (no new endpoints, no new auth surface) |

The clean result reflects that GUR-97's surface is genuinely narrower — it normalizes payloads passing through the existing `POST /hook/{event_type}` endpoint and enforces a declarative drop-list. No new attack surface was introduced; the privacy gates added (DROP_LIST + canary) are *positive* security additions verified to work.

## Carry-forward (informational, not security)

- Pre-existing test-isolation flakiness in `tests/scripts/test_hook_fallback.py::test_dt2_parallel_writes_no_truncation` (passes 3/3 isolated; inherited from GUR-96 task-4). Not a security concern.
- 22 pre-existing mypy errors in `tests/poc/test_storage.py` + 1 in `tests/observation/test_pipeline.py`. Type correctness, not security.

## Transition

Per skill protocol: review passed → invoke `samsara:validate-and-ship`. No risks accepted; nothing to carry into the ship-manifest beyond the GUR-97 carry-forward items already named in `index.yaml.open_carryforward`.
