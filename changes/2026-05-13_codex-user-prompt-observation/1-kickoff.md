# Kickoff: codex-user-prompt-observation

## Problem Statement

Codex observation currently treats `user_prompt_submit` as a hook event that does not carry prompt text, so `CodexAdapter._normalize_user_prompt_submit()` only persists `cwd` metadata. This diverges from the intended observation contract: hook payload is the single source of truth, adapters parse hook data into ingress/event fields, and downstream tracker/pipeline persists the normalized event. If Codex hook payload can provide the user prompt, the adapter must store the complete prompt text, not a length or an absent placeholder.

## Evidence

- Claude Code follows the intended path: `scripts/hooks/user-prompt.sh` reads stdin, `_lib.sh` wraps the raw hook payload into the ingress envelope, `ClaudeCodeAdapter._normalize_user_prompt_submit()` reads `payload["prompt"]`, and persists `data.action_metadata.prompt_text`.
- Codex currently diverges: `src/secondsight/adapters/codex.py` documents `user_prompt_submit -> user_prompt. No prompt text in hook payload (per P0-2)` and returns only `action_metadata.cwd`.
- `tests/fixtures/codex/user_prompt_submit.json` encodes the same assumption: its expected partial event data contains only `cwd`, with no `prompt_text`.
- Existing Claude tests already define the desired death surface: prompt text must be stored completely and must not regress to `prompt_length`.
- The current correction should not require config-unification files. The minimal scope is Codex adapter behavior, Codex fixtures, and Codex adapter tests.

## Risk of Inaction

Codex sessions will produce segments whose `user_prompt` lacks the core intent signal. Analysis prompts will still render structurally valid JSON, but behavior classification will be under-informed: reads, tool calls, and edits cannot be judged against the user's actual request. The failure is silent because ingestion succeeds, segment indices increment, and tests currently encode the missing prompt as expected behavior.

## Scope

### Must-Have (with death conditions)

- **Persist complete Codex user prompt from hook payload** — Death condition: remove this must-have if verified Codex `user_prompt_submit` payloads do not expose prompt content in any stable hook field.
- **Align Codex data shape with Claude Code** — Death condition: remove this if product explicitly chooses agent-specific prompt schemas and updates all downstream consumers to handle them.
- **Add a Codex death test preventing prompt-length-only storage** — Death condition: remove this only if user prompt content is intentionally excluded for a documented privacy policy and analysis no longer depends on prompt text.
- **Update Codex fixture to represent observed hook payload truth** — Death condition: remove this if the fixture cannot be backed by a real or documented Codex hook payload shape.

### Nice-to-Have

- Accept multiple candidate prompt field names defensively, for example `prompt`, `prompt_text`, or a nested Codex-specific field, while keeping one normalized output: `action_metadata.prompt_text`.
- Add an API-level integration test for `/hook/codex/user_prompt` proving the prompt survives hook -> adapter -> tracker -> raw trace/DB.
- Clean up stale comments in Codex/Claude privacy docs so "prompt text never stored" does not contradict `prompt_text` persistence.

### Explicitly Out of Scope

- Parsing Codex rollout JSONL as a replacement source of truth.
- OpenCode prompt handling.
- Config unification, config loader, model selection, runtime wiring, or CLI config commands.
- Changing hook shell scripts unless real Codex payload inspection proves the shell wrapper drops a prompt field.
- Updating DB schema; prompt text should remain in event `data` JSON.

## North Star

```yaml
metric:
  name: "codex_user_prompt_text_survival"
  definition: "For a Codex user_prompt hook payload containing a user prompt, the persisted Event.data.action_metadata.prompt_text equals the original full prompt string."
  current: 0
  target: 1
  invalidation_condition: "Codex hook payload is verified not to contain prompt text, making hook-only prompt persistence impossible."
  corruption_signature: "Tests pass while persisted data contains prompt_length, part_count, empty string, truncated text, or rollout-derived text instead of the hook payload value."

sub_metrics:
  - name: "adapter_fixture_alignment"
    current: "codex fixture expects cwd only"
    target: "codex fixture expects action_metadata.prompt_text"
    proxy_confidence: high
    decoupling_detection: "Compare fixture expected data with adapter output and a real captured Codex hook payload."
  - name: "analysis_prompt_visibility"
    current: "Codex SegmentData.user_prompt lacks prompt_text"
    target: "Codex SegmentData.user_prompt includes prompt_text before build_segment_prompt()"
    proxy_confidence: medium
    decoupling_detection: "A segmenter test can pass while API/DB persistence still fails, so keep adapter/API tests as the source of truth."
  - name: "no_config_overlap"
    current: "dirty worktree includes config-unification files owned by another agent"
    target: "Codex prompt fix touches no config-unification files"
    proxy_confidence: high
    decoupling_detection: "git diff for the change contains only codex adapter, codex fixture, codex adapter/API tests, and optional docs."
```

## Stakeholders

- **Decision maker:** SecondSight maintainer deciding whether Codex hook payload is the observation source of truth.
- **Impacted teams:** Observation/adapter layer, analysis prompt consumers, test maintainers.
- **Damage recipients:** Privacy reviewers who must accept full prompt storage for Codex; maintainers who must keep fixtures aligned with real Codex hook payloads; the other active agent if this work touches config-unification files.
