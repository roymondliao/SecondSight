# TODO

## Agent-Specific Hook Injection Protocol: Deferred Design Decision

### Why

`session-start.sh` currently outputs plain text to stdout for convention injection.
Claude Code reads this as system prompt content. Codex does **not** — Codex requires
`{"systemMessage": "..."}` JSON output (confirmed from `.codex/hooks/samsara-session-start.sh`).

This means Codex convention injection (Layer 1) silently does nothing today.

A fix is straightforward (~8 lines in `session-start.sh`), but the right abstraction
layer is deferred because:

1. **Codex Layer 2 protocol unknown**: `UserPromptSubmit` hook return format for Codex
   is not yet verified. If it also diverges (different field name than `additionalContext`),
   any lib function built now will need its API changed again.
2. **Only 2 agents**: Building a multi-agent injection lib for 2 agents is premature
   abstraction. The correct time is when a 3rd agent (e.g., OpenCode) is added and a
   third case appears.

### Kill conditions for deferral

- If Codex Layer 2 (`UserPromptSubmit` return format) is confirmed, implement the
  `case` branch in `session-start.sh` immediately without a lib abstraction.
- If a 3rd agent is added, extract a `secondsight_inject_context` function into
  `_lib.sh` at that point (three similar cases justify abstraction).

### What needs to happen first

1. Verify Codex `UserPromptSubmit` hook return format (same `additionalContext` as
   Claude Code, or a different field?)
2. Confirm Codex actually reads `SessionStart` hook stdout (the `systemMessage` path
   is from Samsara, not SecondSight — needs end-to-end smoke test)

### Candidate solutions (ranked by current preference)

1. **`case` branch in `session-start.sh`** (minimal, no abstraction):
   ```bash
   case "${SECONDSIGHT_AGENT:-claude_code}" in
       codex) printf '{"systemMessage":%s}\n' "$(printf '%s' "$conventions" | jq -Rs '.')" ;;
       *)     printf '%s\n' "$conventions" ;;
   esac
   ```
2. **`secondsight_inject_context` in `_lib.sh`** (after Layer 2 + 3rd agent confirmed)
3. **Agent-specific script directories** (`scripts/hooks/claude-code/`, `scripts/hooks/codex/`)
   — rejected: duplicates shared logic (symlink resolution, `secondsight_post`) for minimal gain

---

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

## Per-Project Config Override (`~/.secondsight/projects/<pid>/config.toml`): Deferred

### Why

SecondSight currently allows per-project config override at
`~/.secondsight/projects/<project_id>/config.toml` (SD §8.5.2). Existing
schema supports:

- `[analysis].model` — per-project model override
- `[retention].raw_traces_ttl_days` — per-project TTL override
- (other sections per SD §8.5.2)

The analysis-mode-toggle effort
(`changes/2026-05-14_analysis-mode-toggle/`) considered extending this to
support per-project `mode` selection (e.g., one project uses CLI mode
borrowing the user's Claude subscription, another project uses SDK mode
with its own API key for a different LLM provider).

Locked decision (2026-05-14): per-project override **entirely defers** to
a follow-up effort. Only the global `~/.secondsight/config.toml` is
supported during the analysis-mode-toggle work. The per-project file path
will still be readable by the loader for backward compatibility, but no
new per-project fields are added and the existing
`ProjectAnalysisConfig.model` / per-project retention TTL behavior is
preserved as-is (frozen, not extended).

### Kill conditions for this deferral

- A user explicitly asks for "different mode per project" (e.g., work
  uses CLI, side project uses SDK) → promote to feature ticket.
- A third independent per-project field is requested → the dual-layer
  override complexity is now justified by three callers.
- Multi-tenancy is added to SecondSight (currently single-user) →
  per-project config becomes mandatory.

### What needs to happen first

1. Decide whether per-project `[analysis]` mirrors the global
   `[analysis.cli]` / `[analysis.sdk]` nested split, or stays flat with
   merge-by-precedence semantics.
2. Decide whether per-project can carry its own `[providers.*]`
   credentials (different API key per project).
3. Decide the fate of existing `ProjectAnalysisConfig.model` field
   (`schema.py:136-156`) — deprecate, or repurpose under the new
   nested structure.
4. Decide validation strategy: does per-project `mode = "sdk"` require
   per-project `[providers.*]`, or fall back to global providers?

### Action

- Hold until either kill condition fires.
- When re-opened, start a new `changes/<date>_per-project-config/`
  research with a kickoff that references the analysis-mode-toggle
  kickoff Locked Decision E6.

---

## SecondSight Session Storage: Re-evaluate Small-File Strategy

### Why

SecondSight currently persists session durability artifacts under:

- `~/.secondsight/projects/<project_id>/sessions/<session_id>/events/*.json`
- `~/.secondsight/projects/<project_id>/sessions/<session_id>/ingress/*.json`

This means each logical event currently lands as at least two small files:

- one normalized event file
- one raw ingress file

For the current observed local dataset, this is not yet a scale problem:

- `SecondSight/sessions` is only a few MB
- file counts are still in the hundreds, not the tens or hundreds of thousands
- the hot server paths mostly read SQLite, not the raw session filesystem

But the design question is still valid:

- does one-file-per-event remain the right durability tradeoff as sessions grow?
- would append-based storage reduce inode churn and filesystem metadata cost?
- if append is considered, should it be JSON array, JSONL/NDJSON, or a hybrid?

This needs an explicit engineering decision instead of leaving the current
layout as an unexamined default.

### What

Evaluate whether the current filesystem-first session storage model should
remain:

- `one file per event` for both `events/` and `ingress/`

or evolve toward one of these alternatives:

- `per-session JSONL append` for ingress only
- `per-session JSONL append` for both ingress and normalized events
- `DB-first with reduced raw-file retention`
- `hybrid`: keep raw normalized event files as-is, but compact ingress

The goal is not "reduce file count at any cost".
The goal is to choose the right tradeoff across:

- crash safety / partial-write recovery
- append atomicity guarantees
- backfill and replay complexity
- retention / cleanup behavior
- ingestion latency
- inode and directory growth over long-running use

### How

1. Document the current durability behavior.

- `RawTraceStore` writes one file per event using `tmp + fsync + rename`
- `RawIngressStore` does the same for ingress payloads
- `ObservationPipeline` writes filesystem first, DB second

2. Quantify the growth model.

- estimate files per event
- estimate bytes per event
- estimate directory growth over time under realistic event rates
- identify the thresholds where APFS/local SSD behavior may start to matter

3. Compare candidate storage patterns.

- current one-file-per-event
- append-only JSON array per session
- append-only JSONL per session
- hybrid models

For each option, evaluate:

- atomic write story
- corruption blast radius
- replay/backfill ergonomics
- retention cleanup ergonomics
- implementation complexity

4. Decide whether `ingress/` and `events/` should be treated differently.

There may not be one answer for both:

- ingress is more log-like and may be a better JSONL candidate
- normalized event files may still benefit from one-file-per-event isolation

5. If a design change is justified, write a proper `changes/` plan before implementation.

### Action

- Audit the actual local `~/.secondsight/projects/*/sessions` growth pattern
  again once more projects and longer-running sessions exist.
- Produce a short design note comparing:
  - current one-file-per-event
  - per-session JSONL append
  - hybrid ingress-compaction approach
- Include explicit failure-mode analysis:
  - crash during append
  - truncated tail record
  - replay/backfill after partial corruption
- Decide whether `ingress/` is the first and safest place to compact, rather
  than changing both storage layers at once.
- If the answer is "keep current design", record that decision explicitly with
  the scale assumptions that justify it.
