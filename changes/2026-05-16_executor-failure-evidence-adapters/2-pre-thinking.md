## Planning Pre-thinking: Information Assumptions

To write this plan, I am assuming:

- The desired boundary is already established by kickoff/autopsy: executor-specific raw text and provider quirks belong in adapter-local evidence extraction, while `output_recovery.py` owns stable taxonomy and retry semantics.
- The current Phase 2 retry policy remains unchanged: output-repair retry is only for JSON/schema/normalizable output failures; transport classes are classified but not repaired by prompt feedback; fatal auth/config does not retry.
- The initial implementation should preserve the existing `AnalysisOutput.error_details` shared envelope while adding evidence source/confidence fields for forensics.
- The current supported CLI agents are `claude_code` and `codex`; opencode remains unsupported and no new provider integration is in scope.
- SDK/router already owns structured exception and attempt trace information, so SDK evidence should be derived from controlled exception types and chains rather than provider message strings where typed data exists.

Gaps I cannot resolve from Research:

- None for the first implementation slice. The kickoff explicitly constrains scope, non-goals, boundary ownership, and acceptance sketch.

Uncertainties:

- Whether evidence should eventually become a persisted typed schema rather than a JSON-safe dict remains intentionally out of scope for this change. This plan treats it as an internal typed contract serialized into the existing `error_details` envelope.
