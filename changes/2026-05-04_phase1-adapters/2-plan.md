# Plan: phase1-adapters (GUR-97)

**Status:** Draft pending board confirmation
**Predecessor research:** `1-kickoff.md`, `problem-autopsy.md`
**Death-test discipline:** all tasks death-test-first per project standard

## 1. Translation-delta resolutions (locked by this plan)

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | `AgentAdapter.normalize() -> PartialEvent` (not `Event`). | Runtime authoritative. GUR-96's `Normalizer.normalize()` already returns `PartialEvent`; tracker.bind() owns sequence/segment/depth assignment. SD §4.2's `-> Event` is documentation drift. **Action: file an SD §4.2 erratum note in `changes/.../sd-errata.md`.** No runtime change. |
| 2 | Single-PR migration. No re-export shim. `Normalizer` → `AgentAdapter` (ABC, not Protocol). `NormalizerRegistry` → `AdapterRegistry`. `IdentityNormalizer` → `IdentityAdapter`. `api/normalizer.py` deleted. | Re-export shims are parallel scaffolding. Existing GUR-96 callers (`api/server.py`, `api/hooks.py`, tests) are migrated in the same change. Death-condition from kickoff: "if migration leaves any production caller still importing `secondsight.api.normalizer.Normalizer` after the migration commit, the migration is incomplete." |
| 3 | `inject_convention(self, convention: "Convention") -> str` — `Convention` forward-referenced via `TYPE_CHECKING`. ABC body raises `NotImplementedError("Phase 2 — see GUR-104")`. `IdentityAdapter` and `ClaudeCodeAdapter` inherit the loud-failure default. | Holds the seam without depending on Phase-2 model. Loud failure prevents Phase-2 code from accidentally calling and getting `""`. |
| 4 | `inject_hint(self, hint: "Hint") -> str` — same pattern, raises `NotImplementedError("Phase 0 reserved; see SD §4.2")`. No `pass`, no `return ""`. | Reserved interfaces die when their default is silent. |
| 5 | Two methods, distinct concerns. `supports(agent, event_type) -> bool` is *dispatch* (kept; registry uses it). `supported_event_types() -> set[str]` is *capability publication* (new; for dashboards/analysis). | Different consumers. Collapsing them was the original wording's invitation. |
| 6 | Verified mapping required. Use captured rtk-rewrite.sh payload (PreToolUse Bash, available locally) plus Claude Code documented hook schema for the other event types. Each fixture stored under `tests/fixtures/claude_code/<event_name>.json` with `_source` field documenting capture origin (verified | documented). Unverified types (e.g. `thinking`, `sub_agent_*`) explicitly excluded from Phase-1 floor and noted in scar. | Per kickoff death condition: "if a recorded Claude Code hook payload contains a field we cannot account for (silently dropped or reshaped), treat the adapter as unverified and block ship." Documented-only types are gated to next phase. |

### SD §4.2 erratum (resolution 1, recorded inline)

```text
SD §4.2 specifies AgentAdapter.normalize(...) -> Event.
Runtime contract (since GUR-96, ratified by GUR-97): -> PartialEvent.
Reason: SessionTracker.bind() is the authoritative step that assigns
sequence_number, segment_index, sub_agent_id, depth. Adapter does not
have that state; tracker does. Returning Event from the adapter would
require either (a) placeholder values overwritten downstream (silent
state mutation) or (b) the adapter calling into the tracker (cyclic
dependency). Neither is acceptable.

Action: SD §4.2 to be updated in a follow-up doc-only patch. This plan
does NOT block on SD update; runtime is the source of truth.
```

## 2. Wave structure

```
Wave 1 (parallelizable, no deps):
  task-1  P1-9-base   adapters/base.py — AgentAdapter ABC + AdapterRegistry + NoAdapterError
  task-2  P1-9-fixt   tests/fixtures/claude_code/*.json — capture/document real payloads

Wave 2 (depends on Wave 1):
  task-3  P1-9-mig    Migrate Normalizer→AgentAdapter, NormalizerRegistry→AdapterRegistry,
                       IdentityNormalizer→IdentityAdapter. Update api/server.py + api/hooks.py.
                       Delete api/normalizer.py. (cannot start until task-1's ABC exists)
  task-4  P1-10       adapters/claude_code.py — ClaudeCodeAdapter against task-2 fixtures.
                       Drop_list per SD §3.7.4. (cannot start until task-1's ABC exists)

Wave 3 (validation):
  task-5  P1-9-int    Integration test: hook → ClaudeCodeAdapter → tracker → pipeline → DB.
                       Asserts north-star fidelity = 1.0 against task-2 fixtures.
                       (depends on task-3, task-4)
```

## 3. File map (planned)

**Create:**
- `src/secondsight/adapters/__init__.py` — barrel export
- `src/secondsight/adapters/base.py` — `AgentAdapter` ABC, `AdapterRegistry`, `NoAdapterError`
- `src/secondsight/adapters/identity.py` — `IdentityAdapter` (renamed from `IdentityNormalizer`)
- `src/secondsight/adapters/claude_code.py` — `ClaudeCodeAdapter`
- `tests/adapters/test_base.py` — ABC + registry tests
- `tests/adapters/test_identity.py` — IdentityAdapter migration tests
- `tests/adapters/test_claude_code.py` — ClaudeCodeAdapter unit + death tests
- `tests/adapters/test_integration_claude_code.py` — end-to-end (Wave 3)
- `tests/fixtures/claude_code/pre_tool_use_bash.json` — verified
- `tests/fixtures/claude_code/user_prompt_submit.json` — documented
- `tests/fixtures/claude_code/session_start.json` — documented
- `tests/fixtures/claude_code/session_end.json` — documented
- `tests/fixtures/claude_code/post_tool_use.json` — documented (mapped to tool_use_end)

**Modify:**
- `src/secondsight/api/server.py` — import path: `secondsight.api.normalizer` → `secondsight.adapters`
- `src/secondsight/api/hooks.py` — same
- `src/secondsight/api/__init__.py` — drop normalizer export
- `tests/api/test_*.py` (whichever import the old symbols) — update imports

**Delete:**
- `src/secondsight/api/normalizer.py`

## 4. Death cases (must be tested red→green)

Per task-by-task, but the bundle-level death cases this plan must instrument:

1. **Migration leaves stale import.** Death test: `grep -r 'from secondsight.api.normalizer' src/` returns zero hits after task-3 lands. Implemented as a pytest collect-time assertion in `tests/adapters/test_base.py`.
2. **`inject_hint` silent default.** Death test: `ClaudeCodeAdapter().inject_hint(...)` raises `NotImplementedError`, message contains "Phase 0 reserved" and "SD §4.2".
3. **Unknown agent.** Death test: `AdapterRegistry.for_("nonexistent", "user_prompt")` raises `NoAdapterError` whose message names the missing pair.
4. **Malformed Claude Code payload — missing `session_id`.** Death test: `ClaudeCodeAdapter().normalize(envelope_without_session_id, "user_prompt")` raises `ValueError`, not `KeyError`, not silent default to `""`.
5. **Privacy regression — tool_input content stored.** Death test: a `PostToolUse`-derived `tool_use_end` event's `data` dict MUST NOT contain raw `tool_input.command` for Bash, raw file content for Write, or any field listed in the adapter's drop_list. Asserted by sentinel value in fixture (`"PRIVACY_CANARY_DO_NOT_STORE"`) — if the canary appears in `Event.data`, the test fails.
6. **`supports()` vs `supported_event_types()` skew.** Death test: for every `et` in `ClaudeCodeAdapter().supported_event_types()`, `ClaudeCodeAdapter().supports("claude_code", et) is True`. Catches the case where one is updated and the other isn't.
7. **AgentAdapter ABC instantiation.** Death test: `AgentAdapter()` raises `TypeError` (ABC). Sub-class with missing `normalize` raises `TypeError` at construction.

## 5. Drop_list (SD §3.7.4 compliance, ClaudeCodeAdapter)

Per SD §3.7.4 the `data` field stores: `tool_name`, `action_target`, `action_metadata`, `success`, `error_type`, `output_size`. **NOT** `tool_input` content, **NOT** `tool_response` content, **NOT** raw `prompt` text.

Drop list (these fields are read for `data` derivation but the raw values do not flow through):

| Source field | Treatment |
|--------------|-----------|
| `tool_input.command` (Bash) | Compute `len(...)` → `data.action_metadata.command_length`. Raw string DROPPED. |
| `tool_input.file_path` (Read/Write/Edit) | Stored as `data.action_target` (path is metadata, not content). |
| `tool_input.content` (Write) | Compute `len(...)` → `data.action_metadata.content_size`. Raw DROPPED. |
| `tool_input.old_string` / `new_string` (Edit) | Compute `len(...)` for each → `data.action_metadata.{old_size,new_size}`. Raw DROPPED. |
| `tool_response.output` (PostToolUse all tools) | Compute `len(str(...))` → `data.output_size`. Raw DROPPED. |
| `tool_response.error` (PostToolUse) | Type only → `data.error_type`. Raw message DROPPED. |
| `prompt` (UserPromptSubmit) | Length only → `data.action_metadata.prompt_length`. Raw text DROPPED. |
| `transcript_path` | Stored as `data.action_metadata.transcript_path` (file path, not content). |
| `cwd` | Stored as `data.action_metadata.cwd`. |

**Privacy canary test fixture:** every fixture has at least one drop-listed field set to the literal string `"PRIVACY_CANARY_DO_NOT_STORE"`. Test asserts that string never appears in `Event.data`'s JSON serialization. If the adapter's drop logic ever regresses, the canary surfaces it.

## 6. Acceptance criteria (binding)

- [ ] AC-1: `from secondsight.api.normalizer import` returns ImportError after task-3 (file deleted).
- [ ] AC-2: `AgentAdapter` is an ABC; sub-class missing `normalize` fails to instantiate.
- [ ] AC-3: `IdentityAdapter` passes all behavioral tests that `IdentityNormalizer` did, plus the new ABC structural tests.
- [ ] AC-4: `ClaudeCodeAdapter().supported_event_types() >= {SESSION_START, USER_PROMPT, TOOL_USE_START, TOOL_USE_END, SESSION_END}`.
- [ ] AC-5: For each fixture in `tests/fixtures/claude_code/`, `ClaudeCodeAdapter().normalize(envelope_from_fixture, fixture.event_type)` produces a `PartialEvent` whose `data` dict contains all fields listed in §5 column "Treatment" with non-default values, AND does NOT contain any drop-listed raw value.
- [ ] AC-6: Privacy canary test (death case #5) passes for every fixture.
- [ ] AC-7: `inject_hint` and `inject_convention` raise `NotImplementedError` with required message strings (death cases #2 and equivalent for `inject_convention`).
- [ ] AC-8: `mypy` clean across all new/modified files.
- [ ] AC-9: Full test suite passes (baseline 351 + new tests; expected ≥ 380).
- [ ] AC-10: `grep -r 'from secondsight.api.normalizer' src/` returns zero hits in production code (tests may keep one assertion-only import).

## 7. Carried-forward assumptions

- **G1:** Claude Code hook event names map deterministically to `EventType`. Mapping table: `PreToolUse → tool_use_start`, `PostToolUse → tool_use_end`, `UserPromptSubmit → user_prompt`, `SessionStart → session_start`, `SessionEnd → session_end`. Other Claude Code hooks (`Stop`, `SubagentStop`, `Notification`, `PreCompact`) are out of P1 scope; `Stop` may map to `session_end` in a follow-up.
- **G2:** Claude Code hook payloads are JSON via stdin. Schema includes `session_id`, `transcript_path`, `cwd`, `hook_event_name`, plus event-specific fields. This is documented and stable for Claude Code v1.x.
- **G3:** `agent="claude_code"` is the registered identifier (snake_case). Header `X-SecondSight-Agent: claude_code` and body `agent: "claude_code"` both accepted; body wins per existing GUR-96 rule.
- **G4:** Migration commit is atomic with task-3 — single PR, single review pass. We do not ship a transitional state where both `api/normalizer.py` and `adapters/base.py` coexist in production code.

## 8. Non-goals (explicit)

- Codex / OpenCode adapters (P1-11, GUR-109).
- `Convention` model + Phase-2 directive injection runtime (GUR-104, GUR-105).
- `Hint` model — reserved only.
- Backfill of existing fallback JSONL events.
- DB-watch ingestion path (SD §4.1 — Phase 2+).
- `thinking`, `sub_agent_*`, `task_*` event mapping — deferred (no verified Claude Code source).

## 9. Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| Documented-only fixtures (non-PreToolUse) drift from real Claude Code v1.x emission. | Medium | Each fixture's `_source: "documented"` field flagged in scar; integration test (task-5) re-runnable against a live capture in next phase. |
| Migration leaves a test-side import we miss. | Low | grep-based AC-10 + collect-time assertion in death case #1. |
| Privacy drop_list incomplete (a Claude Code field we didn't anticipate carries content). | High | Privacy canary in every fixture (death case #5) catches direct regressions. Anything not in the explicit drop_list is rejected by the adapter (raise on unknown top-level keys would over-fit; instead, allow-list the keys we map and let unknowns flow only into `_unmapped` for the backfill story — see scar). |
| SD §4.2 erratum never lands (drift forever). | Low | Erratum note created in this branch; tracked in scar. |
| Org budget cap stops a subagent dispatch mid-implementation. | Medium | Tasks scoped small enough to be completable in a single heartbeat without subagent dispatch. Direct implementation path is the default. |

## 10. Next action (after board confirmation)

1. Mark plan accepted in this document (set `Status: Approved`).
2. Create implementation subtasks under GUR-97 — one Paperclip child issue per task in §2 (5 children, dependencies set via `blockedByIssueIds`).
3. Begin Wave 1 (task-1 + task-2 in parallel) via the `samsara:implement` flow, death-tests-first.
4. Tasks ship as a single bundle commit after Wave 3 passes (matches task-1 of GUR-96 commit cadence).
