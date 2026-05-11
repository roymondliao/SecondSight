# Problem Autopsy: phase1-adapters

## original_statement

> Define the adapter contract and implement the Claude Code adapter.
>
> **Tasks (P1-9 to P1-10):**
> - P1-9: Adapter interface — `adapters/base.py`: abstract `normalize` + `inject_convention` + `inject_hint` (reserved) + `supported_event_types`
> - P1-10: Claude Code adapter — `adapters/claude_code.py`: Claude Code hook payload → SecondSight Event conversion
>
> **Exit criteria:**
> - Adapter interface defined and stable
> - Claude Code events correctly normalized to SecondSight Event schema
>
> **Ref:** SD 4.2, 4.3, 3.7.4

## reframed_statement

Promote the existing `Normalizer` Protocol from `api/normalizer.py` (shipped under GUR-96) into the canonical `AgentAdapter` ABC defined by SD §4.2 — one cross-cutting interface that owns both observation (`normalize`) and feedback (`inject_convention`, reserved `inject_hint`) plus introspection (`supported_event_types`). Migrate the registry and the existing `IdentityNormalizer` test stub under the new interface so there is exactly one adapter contract in the codebase, not two. Then implement `ClaudeCodeAdapter` against an actual recorded Claude Code hook payload (verified, not invented), covering at minimum the five P1-floor event types (`session_start`, `user_prompt`, `tool_use_start`, `tool_use_end`, `session_end`). All work backed by death tests for unknown agent, malformed payload, and the reserved `inject_hint` blowing up loudly.

## translation_delta

```yaml
translation_delta:
  - original: "P1-9: Adapter interface — adapters/base.py"
    reframed: "Promote api/normalizer.py:Normalizer Protocol → adapters/base.py:AgentAdapter ABC; delete or re-export the old symbol; migrate NormalizerRegistry → AdapterRegistry."
    delta: "Original phrasing reads as 'create a new file alongside existing code'. The actual yin-side risk is parallel scaffolding: api/normalizer.py already exists and is wired into the hook endpoints. If we 'create' adapters/base.py without consolidating, the new ABC is unreachable; the old Protocol keeps serving traffic; the spec lies. Reframe makes consolidation explicit so it cannot be silently skipped."

  - original: "abstract normalize"
    reframed: "abstract normalize(hook_type: str, raw_payload: dict) -> Event per SD §4.2 — but runtime currently needs PartialEvent because SessionTracker assigns sequence_number and segment_index after normalization."
    delta: "SD §4.2 says `-> Event`. Existing GUR-96 code says `-> PartialEvent` because tracker.bind() is what fills sequence/segment. These are genuinely different abstraction levels — Event is the storage shape; PartialEvent is the pre-bind shape. Resolution options must be evaluated in planning: (a) change SD §4.2 to PartialEvent; (b) adapter returns Event with placeholder sequence/segment, registry/tracker overwrite; (c) two-step normalize_payload() + build_partial(). Each has different rot profiles; deferring the choice to planning, not silently picking one here."

  - original: "inject_convention"
    reframed: "inject_convention method shape on the ABC; concrete Convention model + runtime wiring is GUR-104, not GUR-97."
    delta: "Easy mistake: thinking the GUR-97 adapter must accept a real Convention model and emit real SessionStart hook output. SD §4.2 only requires the *interface* to land. The body can be `raise NotImplementedError` for Claude Code's first cut, but the signature must be precise enough that GUR-104 doesn't have to break the ABC. Forward-reference Convention via TYPE_CHECKING."

  - original: "inject_hint (reserved)"
    reframed: "inject_hint must raise NotImplementedError('Phase 0 reserved; see SD §4.2'), not return empty string or pass."
    delta: "Reserved interfaces die when their default body is silent. A loud failure is the only thing that prevents Phase 2/3 code from accidentally calling it and getting an empty injection."

  - original: "supported_event_types"
    reframed: "supported_event_types() -> set[str] for adapter introspection. Not the same as Normalizer.supports(agent, event_type). The latter is dispatch (does this normalizer handle this pair). The former is capability publication (what this adapter can produce)."
    delta: "Original wording invites collapsing the two methods. They serve different consumers: dispatch is for the registry; capability is for downstream (dashboards, analysis layer). Keep both — the registry's first-match-wins logic still wants supports(); the SD §4.2 contract wants supported_event_types() as a set."

  - original: "Claude Code hook payload → SecondSight Event conversion"
    reframed: "Verified field-by-field mapping from a real captured Claude Code hook payload (not invented) to Event, with explicit drop_list for fields we deliberately don't store per SD §3.7.4 (action+target+metadata, no input/output content)."
    delta: "Original could be satisfied by inventing a plausible-looking dict and writing a normalizer against it. Per SD §3.7.4 the granularity rule is strict: tool_use stores tool_name + action_target + action_metadata + success + error_type + output_size, NOT input/output content. The adapter is where this rule is enforced; an unverified mapping is a privacy and storage-bloat liability."
```

## kill_conditions

```yaml
kill_conditions:
  - condition: "If Claude Code introduces a v2 hook protocol (rich JSON-RPC envelope, native typed events) within Phase 1's ship window."
    rationale: "Investing in a normalizer for the v1 bash-curl hook payload becomes throwaway work. Better to wait one cycle than ship a normalizer obsoleted at merge."
  - condition: "If the Storage layer surfaces a fundamental Event schema change (e.g., column-shape break in §3.7.5) before this work merges."
    rationale: "Adapter normalize() output targets Event. If Event is moving, normalize is sand. Pause until storage settles, otherwise we double-pay the migration."
  - condition: "If the GUR-96 Normalizer Protocol turns out to have downstream consumers we did not anticipate (e.g., a third-party plugin imports it directly)."
    rationale: "Migration cost spikes from 'rename in our own code' to 'coordinate with external consumers'. At that point either keep both interfaces with deprecation warnings (paying parallel-scaffolding cost) or block ship — neither is what GUR-97's exit criteria assume. Re-scope before continuing."
  - condition: "If we cannot obtain a real Claude Code hook payload capture during the work (e.g., reference repo unavailable, no test environment with a real Claude Code session)."
    rationale: "The whole point of GUR-97 is to replace IdentityNormalizer with a verified adapter. Inventing a payload shape and calling that 'verified' is exactly the failure mode this work is trying to prevent. Better to block ship than to ship a fiction."
```

## damage_recipients

```yaml
damage_recipients:
  - who: "Future adapter authors (P1-11 Codex, P1-12 OpenCode)"
    cost: "Inherit AgentAdapter ABC. If we get the signature wrong, every subsequent adapter pays migration cost. Mitigated by faithful SD §4.2 implementation rather than convenient shortcuts."

  - who: "Test suite maintainers"
    cost: "GUR-96 tests import Normalizer / NormalizerRegistry / IdentityNormalizer. Migration either renames in-place (one PR diff but bigger blast radius) or keeps re-export shims (smaller diff but technical debt). Cost lands on whoever maintains tests next."

  - who: "Storage / SessionTracker owner (currently same backend-engineer)"
    cost: "Translation gap between SD §4.2 (`-> Event`) and tracker reality (`-> PartialEvent`) requires a design call. Whichever resolution we pick, somebody owns either an SD update or a runtime adapter shim. Documented in translation_delta #1."

  - who: "Operators reading server logs"
    cost: "Per-adapter logger child names will multiply log namespaces (`secondsight.adapter.claude_code`, ...). Filter rules and dashboards need updating. Optional sub-metric, but if shipped it adds operational surface."

  - who: "Privacy / compliance reviewers"
    cost: "Claude Code hook payloads contain user prompts and tool inputs/outputs. The adapter is the gatekeeper that enforces SD §3.7.4's drop rules. If the adapter ships an incomplete drop_list, sensitive content lands in storage. Cost ultimately lands on the security review (samsara:security-privacy-review skill, GUR-117-style follow-up)."
```

## observable_done_state

A real Claude Code session running locally produces events that arrive in the per-project SQLite via `POST /hook/{type}` through `ClaudeCodeAdapter`, and `IdentityNormalizer`-as-`agent="test"` continues to work for tests — both visible by querying `events.event_type, events.event_id, json_extract(data, '$.tool_name')` after a session and seeing real Claude Code values, not test fixtures. Symbol `secondsight.api.normalizer.Normalizer` no longer exists in production callsites (verified by `grep -r 'from secondsight.api.normalizer'` returning zero hits outside migration tests). Calling `ClaudeCodeAdapter().inject_hint(...)` raises `NotImplementedError` loudly with the SD §4.2 reference in the message — silent no-op is the failure mode we're explicitly preventing.
