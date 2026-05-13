# Problem Autopsy: codex-user-prompt-observation

## original_statement

「正確來說 hook 觸發，在 codex 應該可以獲取 user prompt 的資訊，而 observation layer 的規劃是
1. hook 獲取的資料是 single source of truth
2. 對 hook 取得資料進行 parse 跟處理，對應 ingress 需要的欄位做處理
3. 後續的處理....

以上描述就是目前 claude code 的處理路徑，請確認」

「$research 先把剛剛討論的內容，整理一下，針對 codex 的資料處理狀況。」

## reframed_statement

Codex observation should mirror the Claude Code data path: the hook payload is the authoritative input, the Codex adapter parses that payload into normalized `PartialEvent.data`, and the rest of observation persists it without re-deriving prompt content from secondary sources. The current Codex implementation violates that expected path by assuming `user_prompt_submit` has no prompt content and by persisting only `cwd` metadata. The immediate research question is not how to parse rollout JSONL, but whether Codex hook payload fields are being ignored or mis-modeled in adapter tests and fixtures.

## translation_delta

```yaml
translation_delta:
  - original: "hook 觸發，在 codex 應該可以獲取 user prompt 的資訊"
    reframed: "Codex hook payload should contain a stable prompt field that the adapter must persist"
    delta: "The original states an expectation; the reframing turns it into a verifiable adapter contract."
  - original: "hook 獲取的資料是 single source of truth"
    reframed: "Do not use rollout JSONL as the primary fix for Codex user prompt persistence"
    delta: "Earlier discussion considered rollout enrichment; this reframing rejects that as the primary observation path."
  - original: "對 hook 取得資料進行 parse 跟處理"
    reframed: "CodexAdapter._normalize_user_prompt_submit() must parse prompt fields into action_metadata.prompt_text"
    delta: "The general parse step is localized to the adapter function that currently drops prompt content."
  - original: "以上描述就是目前 claude code 的處理路徑"
    reframed: "Claude Code is the reference implementation for Codex data shape"
    delta: "This converts an architectural observation into the compatibility target: Codex should normalize to the same prompt_text shape."
```

## kill_conditions

```yaml
kill_conditions:
  - condition: "A real Codex user_prompt_submit hook payload is captured and it contains no prompt content in any stable field."
    rationale: "The hook-only single-source-of-truth requirement would be impossible for prompt text; the correct work would become a product decision about degraded Codex support or a separate official data source."
  - condition: "Codex upstream changes hook payload semantics so prompt text is intentionally unavailable for privacy or security reasons."
    rationale: "Persisting prompt_text would fight the upstream contract and create a brittle or policy-violating integration."
  - condition: "SecondSight decides full prompt storage is not allowed for Codex privacy posture."
    rationale: "The analysis model would need a new design that does not depend on raw user prompt text."
  - condition: "The only available prompt source is rollout JSONL rather than hook payload."
    rationale: "That is a different feature with different consistency, latency, and source-of-truth properties."
```

## damage_recipients

```yaml
damage_recipients:
  - who: "Privacy/security reviewers"
    cost: "Must explicitly accept that Codex user prompts are persisted completely in Event.data.action_metadata.prompt_text."
  - who: "Adapter maintainers"
    cost: "Must keep Codex fixtures synchronized with the real hook payload shape and update tests when upstream fields change."
  - who: "Analysis maintainers"
    cost: "Must handle historical Codex sessions that already lack prompt_text unless backfill/migration is later introduced."
  - who: "Concurrent config-unification agent"
    cost: "Could suffer merge conflicts if this work unnecessarily edits shared runtime, CLI, registry, or config files."
```

## observable_done_state

Given a Codex `user_prompt_submit` hook payload containing a prompt, `CodexAdapter.normalize()` returns a `PartialEvent` whose `data.action_metadata.prompt_text` exactly equals the original prompt. The focused Codex tests fail if the adapter stores only `cwd`, stores `prompt_length`, truncates the prompt, or omits the field. The change touches Codex adapter/fixtures/tests only and does not modify config-unification files.
