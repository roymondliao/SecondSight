# Kickoff: phase1-adapters

**Issue:** GUR-97 — Phase 1: Agent Adapters — Adapter Interface + Claude Code Adapter
**Plan tasks:** P1-9 (adapter base), P1-10 (Claude Code adapter)
**SD refs:** §3.7.4, §4.1, §4.2, §4.3
**Predecessor:** GUR-96 (Phase 1.2 API server) — shipped commit `59abedd`. Provided `Normalizer` Protocol + `NormalizerRegistry` + `IdentityNormalizer` wired into `POST /hook/{type}`. **GUR-97 must promote this scaffold into the SD §4.2 `AgentAdapter` contract, not duplicate alongside it.**

## Problem Statement

The hook endpoints from GUR-96 currently accept events from a single stub agent (`agent="test"` via `IdentityNormalizer`) — there is no real agent producing observable behavior data. We need a stable, abstract `AgentAdapter` interface (SD §4.2) that covers both **observation** (raw hook payload → SecondSight `Event`) and **feedback** (`inject_convention` + reserved `inject_hint`), then implement the first concrete adapter for Claude Code so Phase 1 has at least one production-grade observation source. The interface must be cross-cutting, the Claude Code adapter must reproduce real Claude Code hook payload shapes verbatim, and the existing `Normalizer` Protocol must be migrated under the new contract — not left as parallel scaffolding.

## Evidence

- **GUR-96 explicitly defers real adapters** (`api/normalizer.py:5`): *"Real adapters (P1-9..P1-11) will land here; for Phase 1 we ship only IdentityNormalizer for agent='test'."* IdentityNormalizer raises `NoNormalizerError` for any non-`test` agent — the only thing standing between the hook server and "0 events ever from a real agent" is GUR-97.
- **SD §4.2 specifies a 4-method `AgentAdapter` ABC** (`normalize`, `inject_convention`, `inject_hint`, `supported_event_types`). The current `Normalizer` Protocol only covers a subset (`supports`, `normalize`) and returns `PartialEvent`, not `Event`.
- **SD §4.3 Phase-0 investigation note for Claude Code:** *"hook payload 格式、session context"* still listed as待調查. We must read the actual hook payload format (open-source Claude Code hook reference is in `reference_opensoure/claude-code-langfuse-template/`) before writing the normalizer body — assumptions are how silent corruption gets baked in.
- **Downstream blockers:** GUR-100 (BehaviorFlag schema; vocabulary listed in MEMORY) and GUR-104 (Directive lifecycle / analysis) both depend on real Claude Code events arriving in the storage layer. Without GUR-97, Phase 1 ends with a server that talks only to itself.

## Risk of Inaction

- **Silent rot of the GUR-96 scaffold.** `Normalizer` Protocol exists with a `# Real adapters land in P1-9..P1-11` TODO; if GUR-97 ships under a different name (`adapters/base.py` *alongside* `api/normalizer.py`), the hook endpoints continue calling the registry; the new ABC is unreachable; the comment lies; the API server keeps shipping `agent="test"` only. That is the single most expensive failure mode here.
- **Codex/OpenCode adapters arrive without a tested base.** P1-11 is right behind P1-10. If the base interface ships unverified, every subsequent adapter inherits the unverified shape — and the `Convention` / `Hint` injection paths (Phase 2/3) will need cross-adapter rework to bolt on later.
- **Phase 2 `Convention Injection` path has nowhere to land.** SD §4.2 puts `inject_convention` on the same interface as `normalize` deliberately — the SessionStart hook is bidirectional. A Phase-1 adapter that omits `inject_convention` from the abstract base means Phase 2 must either retrofit every adapter or invent a parallel `FeedbackAdapter` interface. Both are bad.

## Scope

### Must-Have (with death conditions)

- **`adapters/base.py` — `AgentAdapter` ABC matching SD §4.2 verbatim** (`normalize`, `inject_convention`, `inject_hint`, `supported_event_types`).
  - Death condition: if a downstream consumer (GUR-100/104/106) requires a 5th method or a non-trivial signature change within 60 days of merge, we treat the abstract contract as wrong and rewrite — abstraction earns its keep by surviving downstream code.
- **Migrate existing `Normalizer` Protocol → `AgentAdapter` ABC** (single source of truth). Delete `api/normalizer.py` `Normalizer` Protocol (or convert it to a re-export shim that disappears in a follow-up). The `NormalizerRegistry` becomes `AdapterRegistry`. `IdentityNormalizer` becomes `IdentityAdapter` in `adapters/identity.py`.
  - Death condition: if migration leaves any production caller still importing `secondsight.api.normalizer.Normalizer` after the migration commit, the migration is incomplete and must be reverted — parallel scaffolding is the failure mode this whole change is preventing.
- **`adapters/claude_code.py` — `ClaudeCodeAdapter`** with verified hook payload mapping for at minimum `session_start`, `user_prompt`, `tool_use_start`, `tool_use_end`, `session_end`. Mapping verified against an actual recorded Claude Code hook payload, not invented.
  - Death condition: if a recorded Claude Code hook payload contains a field we cannot account for (silently dropped or reshaped), treat the adapter as unverified and block ship until either (a) field is mapped or (b) explicit drop is documented in `tasks/<id>.md` with rationale.
- **Death tests for both base and Claude Code adapter.** Per project death-test-first standard. Minimum: unknown agent → `NoAdapterError`; malformed Claude Code payload (missing required hook field) → typed validation error, not silent default; `inject_hint` on Claude Code adapter → `NotImplementedError` (not a `pass`).
  - Death condition: if a death test passes against a stub that does not actually exercise the failure path, the death test is decoration — rewrite or delete.
- **Reserved-but-not-implemented `inject_hint`.** Implements as `raise NotImplementedError("Phase 0 reserved; see SD §4.2")`. Not a `pass`, not a `return ""`. Silent no-ops are how reserved interfaces die.
  - Death condition: if any caller invokes `inject_hint` in production before the explicit Phase ≥2 lift, it must blow up loudly — that is the contract.

### Nice-to-Have

- Adapter-level introspection helper (`AdapterRegistry.list_supported_event_types(agent: str) -> set[str]`) for Phase 3 dashboard "agents observed" view (GUR-106).
- Per-adapter structured-logging context (logger child named `secondsight.adapter.<agent_name>`) so adapter-level errors are filterable in production logs.
- Conversion utility from `Event` → `PartialEvent` if the SD §4.2 signature (`-> Event`) is preserved literally; see Translation Delta in problem-autopsy.

### Explicitly Out of Scope

- **Codex / OpenCode adapters** (P1-11, separate issue/phase).
- **`Convention` model + `inject_convention` runtime wiring.** Method signature lands in base; concrete `Convention` schema and the Phase-2 directive injection flow live in GUR-104.
- **`Hint` model.** `inject_hint` is reserved-only — typing imported as `TYPE_CHECKING` forward reference, no runtime model required for GUR-97.
- **Backfill of historical fallback JSONL events through the new adapter.** GUR-96 ships fallback writes; replay/backfill is its own design problem (deferred to Phase 2+).
- **DB-watch ingestion path** (OpenCode, SD §4.1). GUR-97 is hook path only.

## North Star

```yaml
metric:
  name: "claude_code_event_normalization_fidelity"
  definition: "Fraction of fields in a recorded real Claude Code hook payload that round-trip to Event.data without silent loss or invention. Measured by replaying ≥1 captured payload per supported event_type through ClaudeCodeAdapter.normalize() and diffing the resulting Event against the source payload."
  current: 0.0  # No real adapter exists yet; IdentityNormalizer is agent='test' only
  target: 1.0   # Every field in the captured payload is either mapped to an Event field or explicitly listed in the adapter's drop_list with rationale
  invalidation_condition: "If Claude Code's hook payload format diverges by >1 breaking field per minor release in the 6 months following ship, treat 1:1 fidelity as the wrong target — pivot to a versioned mapping table instead of a single normalize() body."
  corruption_signature: "Fidelity = 1.0 but downstream Event.data is empty {} — means the adapter is mapping field NAMES but not VALUES, or silently coercing dicts to empty defaults. Detection: assert len(Event.data) >= 1 for every non-trivial event_type in the death tests."

sub_metrics:
  - name: "adapter_interface_callsite_consolidation"
    current: 2  # api/normalizer.py + (after this change) adapters/base.py
    target: 1   # only adapters/base.py
    proxy_confidence: high
    decoupling_detection: "grep -r 'from secondsight.api.normalizer import' src/ — must return zero hits in production code (tests may keep import for migration assertions)."

  - name: "death_tests_against_unknown_agent_and_malformed_payload"
    current: 0
    target: 4  # 1 unknown-agent + 3 malformed Claude Code payload variants (missing session_id, missing event_type, malformed timestamp)
    proxy_confidence: high
    decoupling_detection: "Death-test count rises but every test asserts on a stub raising — not on production code path. Mitigation: each death test must invoke through AdapterRegistry, not the adapter class directly."

  - name: "supported_event_types_coverage_claude_code"
    current: 0
    target: 5  # session_start, user_prompt, tool_use_start, tool_use_end, session_end (P1 floor; thinking/sub_agent/task may slip to a follow-up)
    proxy_confidence: medium
    decoupling_detection: "Coverage count rises but normalize() body for some types is `return PartialEvent(..., data={})` — i.e., shape-only. Mitigation: per-event-type unit test with a recorded payload fixture."
```

## Stakeholders

- **Decision maker:** Tianqi (backend-engineer agent / human board) — interface shape and migration scope.
- **Impacted teams:**
  - GUR-96 surface (already shipped) — `api/server.py:200-201` registry wiring will move; hooks endpoints stay stable.
  - GUR-100 (BehaviorFlag schema, GUR-104 (Directive lifecycle), GUR-106 (Dashboard) — all consume `Event` and must continue working unchanged.
- **Damage recipients:**
  - **Future adapter authors (P1-11 Codex, P1-12 OpenCode):** must inherit from a possibly-too-narrow base. Mitigated by fixing the base now to SD §4.2 verbatim rather than the GUR-96 subset.
  - **Test suite maintainers:** existing GUR-96 tests reference `Normalizer` / `NormalizerRegistry` / `IdentityNormalizer` symbols. Migration touches their imports. Mitigated by either (a) keeping class names backwards-compat by re-export, or (b) batch-renaming in the same PR — choice deferred to planning.
  - **Storage layer (`tracker.PartialEvent`):** signature mismatch between SD §4.2 (`normalize(...) -> Event`) and runtime need (`PartialEvent` so tracker can assign sequence/segment) is a real translation gap. Whoever owns that resolution bears the cost. Documented as **translation_delta #1** in problem-autopsy.
