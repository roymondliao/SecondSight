# TODO

## Codex Fixtures: Promote `tests/fixtures/codex` Toward Real `~/.codex` Data

### Why

`tests/fixtures/claude_code` has already been moved closer to real local agent
data by aligning fixture values against `~/.claude-personal`. That gives the
Claude adapter path a stronger empirical test base and reduces the chance that
the code is accidentally being trained against invented payloads.

`tests/fixtures/codex` is not at the same maturity level yet.

Today, Codex fixture values are only partially grounded in the local
`~/.codex` state:

- real session ids, cwd, and timestamps can be aligned against
  `~/.codex/sessions/YYYY/MM/DD/*.jsonl`
- hook registration can be aligned against `~/.codex/hooks.json`
- but there is no confirmed local archive of raw hook stdin payloads that
  matches the way Claude Code persists transcript-adjacent state

That means the current Codex fixtures are still a hybrid:

- real local session metadata
- plus a reconstructed hook contract shape

This is good enough for basic adapter testing, but it is not yet a strong
"real-world fixture" story. If the Codex hook callback contract drifts, the
codebase could pass tests while still being wrong about the true ingress shape.

### What

The goal is to make `tests/fixtures/codex` as empirically grounded as possible,
while being explicit about what is and is not directly observed from local
Codex artifacts.

The deliverable is not "invent more realistic JSON".
The deliverable is:

- a clearly documented provenance model for Codex fixtures
- a fixture set whose values come from real `~/.codex` session artifacts where
  possible
- an explicit boundary for fields that are inferred from hook contract rather
  than captured from a local raw payload archive
- tests that enforce those provenance expectations so future edits do not blur
  the line between observed data and reconstructed data

### How

1. Establish the real local Codex data sources.

Primary sources already identified:

- `~/.codex/hooks.json`
  - identifies which hook events are actually configured on this machine
- `~/.codex/sessions/YYYY/MM/DD/*.jsonl`
  - contains real rollout/session events such as `session_meta`,
    `turn_context`, `response_item`, `function_call`, and
    `function_call_output`

Important current limitation:

- there is not yet a confirmed local store under `~/.codex` that preserves the
  exact raw hook stdin payload delivered to the hook command

2. Split Codex fixture provenance into two layers.

- `verified`
  - field shape and value were directly observed from local `~/.codex` data
- `documented`
  - field shape depends on current Codex hook contract assumptions, even if the
    concrete values were aligned to local session artifacts

This should mirror the stricter provenance language already added to
`tests/fixtures/claude_code/_README.md`.

3. Define the Codex field mapping boundary explicitly.

Fields that should come from real local rollout/session artifacts where
possible:

- `session_id`
- `cwd`
- event timestamps
- representative tool names
- representative tool inputs
- representative tool outputs
- stop/session-finalization context if present in the session log

Fields that may still need contract reconstruction unless a raw hook capture is
found:

- exact top-level hook payload envelope
- exact hook-specific field names for callback stdin
- exact shape differences between `UserPromptSubmit`, `PostToolUse`, and
  `Stop` callback payloads

4. Raise the fixture docs and tests to match that boundary.

- add a `tests/fixtures/codex/_README.md` that documents provenance rules in
  the same style as the Claude fixture README
- add or tighten fixture death tests so a future maintainer cannot silently
  replace empirically grounded values with invented ones
- ensure adapter tests say clearly whether a failure is about:
  - local-value drift
  - contract-shape drift
  - or adapter normalization drift

5. If needed, add a one-time real hook capture workflow.

If local `~/.codex` artifacts still do not expose raw hook callback stdin, the
only reliable way to close the gap is to capture a real Codex hook invocation.

That capture workflow should:

- use the existing Codex hook registration path
- write the raw stdin payload to a temporary local file
- trigger the relevant hook events from a real Codex session
- sanitize secrets if needed
- then regenerate `tests/fixtures/codex` from that captured ground truth

This should only happen if local durable artifacts remain insufficient.

### Action

- Audit `~/.codex/hooks.json` and `~/.codex/sessions/YYYY/MM/DD/*.jsonl` again
  specifically for fixture-worthy examples of:
  - `SessionStart`
  - `UserPromptSubmit`
  - tool invocation / completion
  - `Stop`
- Write `tests/fixtures/codex/_README.md` with explicit `verified` vs
  `documented` rules.
- Update each file under `tests/fixtures/codex/` so `_capture_origin` and
  `_source` reflect the real provenance boundary instead of implying a stronger
  guarantee than we have.
- Add or extend fixture-validity tests for Codex, parallel to
  `tests/adapters/test_fixtures.py` for Claude.
- Decide whether local session artifacts are sufficient, or whether a real
  hook-stdin capture step is required to finish the Codex fixture story.
- If raw capture is required, add a small temporary capture script and document
  the regeneration workflow before changing the fixtures again.
