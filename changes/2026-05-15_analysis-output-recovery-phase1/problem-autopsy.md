# Problem Autopsy: analysis-output-recovery-phase1

## original_statement

> analysis prompt 雖然要求 output 是 JSON，也有 schema validate，但 model 不一定照做。
> CLI mode 需要 retry、feedback、retry count 限制，而且 SDK 之後也應該有類似機制。
> 希望做成共享機制，但一次改太大可能容易出錯。

## reframed_statement

問題不是「prompt 不夠嚴厲」，而是 **dispatch layer 尚未把 output failures 分類處理**：

- 可本地修復的格式噪音，應先 normalization
- 真正的 schema / JSON 失敗，才應進入 output-repair retry
- retry 次數與 feedback 大小應由 config/contract 管控，而非散落在 dispatcher 裡硬編碼

因此 Phase 1 的正確切法不是「再加更多 prompt instruction」，而是把 CLI path 做成：

`raw output -> normalize -> classify -> validate -> feedback -> bounded retry`

## translation_delta

```yaml
translation_delta:
  - original: "在 prompt 內寫入 python 驗證 code 是否會提高穩定度"
    reframed: "應把 validator 規則轉成 dispatcher-side normalization + structured feedback，而不是把 Python code 貼進 prompt"
    delta: "model 不會執行 prompt 中的 Python；長 validator code 多半只會增加噪音"

  - original: "retry 還要給 feedback，讓 CLI 知道錯誤在哪"
    reframed: "retry 必須以 failure classification 為前提，不同 failure class 需要不同 feedback"
    delta: "不是所有錯誤都該附同一份 feedback；transport/fatal 類錯誤甚至不該走 output-repair retry"

  - original: "這可能會是一個共享機制"
    reframed: "共享機制應分兩階段：Phase 1 先落地最小 contract，Phase 2 再跨 CLI/SDK 收斂成 shared recovery layer"
    delta: "直接一步到位會同時碰 CLI、SDK、config、contract、metrics，風險過高"
```

## kill_conditions

```yaml
kill_conditions:
  - condition: "Phase 1 需要修改 SDK dispatcher 才能交付"
    rationale: "代表 phase boundary 切錯了；Phase 1 必須能獨立 ship"

  - condition: "retry policy 無法在不破壞現有 analysis_outputs / AnalysisOutput contract 的情況下演進"
    rationale: "若 contract 演進路徑不成立，就不應只做局部修補"

  - condition: "normalization 規則需要 agent-specific heuristics 到無法抽成共享 helper"
    rationale: "若連最小 normalizer 都無法共享，Phase 2 的平台化前提不足"
```

## observable_done_state

CLI mode 在遇到以下輸出時，不再直接失敗或白白重試：

- ```` ```json { ... } ``` ````
- JSON 前面多一句說明
- JSON 後面多一小段結語

而真正的 malformed JSON / schema mismatch 仍然會：

- 被分類
- 產生結構化 feedback
- 受 config 限制地重試
- 在 `AnalysisOutput.retry_count` 與 `error_details` 中留下可觀測痕跡
