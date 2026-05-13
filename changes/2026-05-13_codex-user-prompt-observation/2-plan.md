# Plan: Codex User Prompt Observation

**Inputs:** `1-kickoff.md`, `problem-autopsy.md`, `2-fixture-audit.md`, `2-pre-thinking.md`.
**Status of pre-thinking gate:** `proceed` — no unresolved research gaps remain after the 2026-05-13 real hook stdin capture.

## 1. Feature description

Bring Codex observation fully back onto the intended hook-first path:

- verified hook payload is the single source of truth;
- `CodexAdapter` normalizes that payload into the same ingress-facing shape used by Claude Code where applicable;
- ingress and persistence tests prove the normalized fields survive the real observation path;
- installer output matches the verified working Codex hook registration shape so tool hooks actually fire.

This plan intentionally stays inside the Codex observation slice. It does not touch rollout parsing, config unification, CLI config loading, or DB schema.

## 2. Ratified decisions

- **D1. Hook payload wins.** Codex hook stdin, not rollout JSONL, is the authoritative source for `user_prompt`, `tool_use_start`, `tool_use_end`, `session_start`, and `session_end`.
- **D2. Real shape over inferred shape.** The 2026-05-13 captured payloads replace older inferred fixture shapes. PascalCase `hook_event_name` values are part of the contract.
- **D3. Prompt text is stored completely.** `UserPromptSubmit.prompt` must normalize to `data.action_metadata.prompt_text` with no truncation, no length-only surrogate, and no rollout backfill.
- **D4. Raw tool/assistant content stays dropped.** `tool_input.command`, `tool_response`, and `last_assistant_message` remain raw-input privacy fields and must not appear in `Event.data`.
- **D5. Tool hooks are first-class Codex observation events.** `PreToolUse` and `PostToolUse` are supported Codex hook events and must be registered, tested, and normalized directly.
- **D6. Installer output should match the verified working registration shape.** `PreToolUse` and `PostToolUse` entries should include matcher-aware registration consistent with the captured local setup, rather than relying on undocumented defaults.
- **D7. Duplicate Stop behavior is preserved, not redesigned.** If Codex emits multiple `Stop` hooks for one session, this feature treats that as upstream truth and only ensures the payload is handled safely.

## 3. Tech spec — I/O with `unknown`

### 3.1 `CodexAdapter.normalize()`

```
INPUT:  IngressEnvelope(payload=<real Codex hook payload>), event_type=<SecondSight EventType string>
OUTPUT:
  success -> PartialEvent whose data matches the verified Codex fixture contract
  failure -> ValueError for unsupported event_type, missing required fields, or route/payload mismatch
  unknown -> never. Adapter boundaries must fail loudly; they must not synthesize fallback data.
```

### 3.2 Thin ingress route `/hook/codex/{event_type}`

```
INPUT:  HookEnvelope body carrying real Codex hook payload
OUTPUT:
  success -> HTTP 200 plus persisted ingress record + Event row carrying normalized data
  failure -> HTTP 422 for adapter rejection / tracker bind rejection
  unknown -> never. Unknown payload states must surface as explicit rejection, not degraded success.
```

### 3.3 `CodexHooksPatcher.apply()`

```
INPUT:  hook_dir: Path, existing ~/.codex/hooks.json contents
OUTPUT:
  success -> hooks.json contains all required SecondSight Codex hook registrations in the verified shape
  failure -> InvalidSettingsError on malformed JSON or wrong-typed hooks sections
  unknown -> never. Installer must not silently write a partial/ambiguous hook config.
```

## 4. Death cases

- **DC-1: Prompt appears observed but intent is lost.**
  Trigger: `UserPromptSubmit` is normalized to `cwd` only, `prompt_length`, or empty string.
  Lie: Codex user prompt ingestion "works" because events are stored.
  Truth: downstream analysis cannot compare behavior to the user request.
  Detect: adapter + ingress tests assert exact `prompt_text` equality from captured payload.

- **DC-2: Fixture contract drifts back to invented shapes.**
  Trigger: fixture refresh reintroduces lower-case hook names or nested `hook_event` wrappers not present in real stdin.
  Lie: unit tests keep passing because fixtures agree with the bad assumption.
  Truth: production Codex payloads would reject or normalize incorrectly.
  Detect: fixture contract tests pin verified hook names, capture provenance, and privacy canary placement.

- **DC-3: Raw tool/assistant output leaks into Event.data.**
  Trigger: adapter starts copying `tool_response` or `last_assistant_message`.
  Lie: richer metadata appears available for analysis.
  Truth: privacy boundary is violated and the tests no longer reflect the intended drop contract.
  Detect: drop-list canaries on `PostToolUse` and `Stop` fixtures plus end-to-end ingress assertions.

- **DC-4: Tool hooks are "installed" but never fire.**
  Trigger: installer omits `PreToolUse` or writes tool-hook entries in a non-working shape.
  Lie: local setup appears patched successfully.
  Truth: observation misses `tool_use_start` / `tool_use_end` for Codex sessions.
  Detect: installer tests assert all five events and the verified tool-hook registration structure.

- **DC-5: Adapter correctness does not survive ingress.**
  Trigger: adapter unit tests pass, but `/hook/codex/user_prompt` or `/hook/codex/post_tool_use` loses fields during bind/persist.
  Lie: observation contract is fixed.
  Truth: API ingress path still corrupts or drops Codex data.
  Detect: thin-ingress tests verify stored event rows and event JSON contents from real captured payloads.

## 5. File map

- `src/secondsight/adapters/codex.py` — Codex hook normalization contract and drop rules.
- `src/secondsight/installer/codex_hooks.py` — verified Codex hook registration shape.
- `tests/fixtures/codex/*.json` — verified hook payload truth set.
- `tests/fixtures/codex/_README.md` — provenance, drift policy, and capture-refresh rules.
- `tests/adapters/test_codex.py` — adapter death tests and round-trip assertions.
- `tests/adapters/test_codex_fixtures.py` — fixture-authorship contract tests.
- `tests/api/test_ingress_contract.py` or `tests/api/test_ingress_codex.py` — thin ingress Codex persistence verification.
- `tests/installer/test_codex_hooks.py` — installer death tests for full Codex registration.

## 6. Task headlines

- **MH-1 / task-1:** Lock the verified Codex adapter + fixture contract so prompt/tool/session payloads match real stdin and privacy canaries protect the raw fields.
- **MH-2 / task-2:** Add Codex thin-ingress tests that prove the verified hook payload survives hook -> adapter -> tracker -> persisted event.
- **MH-3 / task-3:** Harden Codex installer output to the verified working hook registration shape and document the fixture regeneration rules.

