# Planning Pre-thinking: Information Assumptions

To write this plan, I am assuming:
- [assumption]: The captured hook stdin from 2026-05-13 is the authoritative Codex 0.130.0 contract for `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, and `Stop`.
- [assumption]: The observation source of truth remains the hook payload; rollout JSONL is out of scope for this feature.
- [assumption]: `tool_response` and `last_assistant_message` are privacy-sensitive raw fields that must not flow into `Event.data` under the current Codex observation contract.
- [assumption]: Multiple `Stop` hooks for one session are valid upstream behavior; this feature preserves payload fidelity and does not add dedup semantics.
- [assumption]: The hook shell scripts are already generic enough for Codex; the remaining work is adapter, ingress verification, installer registration shape, and fixture governance.

Gaps I cannot resolve from Research:
- [gap]: none

Uncertainties (I cannot determine if more information is needed):
- [uncertainty]: none

## Gate Outcome

`proceed` — verified local captures closed the earlier research gaps, so planning can continue without carrying undocumented gaps.
