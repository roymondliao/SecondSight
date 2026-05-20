## Planning Pre-thinking: Information Assumptions

To write this plan, I am assuming:

- Convention injection and hit-based guidance are two separate runtime paths with different transport semantics:
  - SessionStart / convention = session-scoped guidance
  - UserPromptSubmit / hit guidance = event-scoped guidance
- The configured runtime mode in `config.toml` (`cli` or `sdk`) remains the single execution-mode source of truth for the hit evaluator, just as it already does for analysis runtime.
- `scripts/hooks/_lib.sh` and analysis CLI `_filter_env()` already establish the recursion guard pattern with `SECONDSIGHT_DISABLE_HOOKS=1`; the hit evaluator should reuse that contract rather than invent a second guard.
- The current adapter layer is the right ownership boundary for final hook output rendering; shell scripts should remain thin transports.
- `feedback.convention_injection_budget` is intended to be runtime-effective config, not documentation-only template text.

Gaps I cannot resolve from Research:

- Codex `UserPromptSubmit` output contract does not yet have the same level of transcript evidence as Claude Code. We have strong local evidence that Codex supports event-scoped hook output semantics, but not a production transcript proving the exact final payload shape for hit injection.
- The exact storage location and data model for the agent-scoped bypass registry is not yet fixed in the codebase.
- There is no existing resolved `feedback` section in `SecondSightConfig`; adding one is necessary for the agreed budget contract, but this is a new config surface rather than a pre-existing owned seam.

Accepted undocumented assumptions:

- For v1, Codex hit injection will use event-scoped hook output rendering owned by the adapter, not session-scoped `systemMessage`. Exact contract assertions will be backed by adapter-level tests and hook-shape fixtures until transcript evidence is added.
- A new dedicated injection API module may replace the current `/hook/session-start` injection implementation instead of retrofitting the old route in place.
- The hit evaluator may use a dedicated lightweight runtime path that respects `config.general.mode` without reusing the full analysis orchestrator surface.
