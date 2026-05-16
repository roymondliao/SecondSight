# Problem Autopsy: analysis-output-recovery-phase2

## reframed_statement

Phase 2 不是在「補 SDK 的 retry」，而是在收斂一個已經被證明有價值的 recovery contract，避免 CLI 與 SDK 各自長出不相容的 failure semantics。

## translation_delta

```yaml
translation_delta:
  - original: "SDK 也要有同樣的機制"
    reframed: "SDK 需要遵守與 CLI 相同的 recovery contract，但執行器不同"
    delta: "shared policy 與 shared executor 不是同一件事"

  - original: "需要錯誤分類跟不同的 feedback"
    reframed: "shared failure taxonomy 必須覆蓋 output / transport / fatal classes，且 mode-specific executor 只負責提供原始 failure evidence"
    delta: "feedback builder 應位於共享層，而不是散落在各 dispatcher"
```

## observable_done_state

同一個 failure class 在 CLI 與 SDK 上都能回答同樣的問題：

- 這是什麼類型的錯誤
- 應不應重試
- 重試次數是多少
- 回饋給模型的是什麼
- `error_details` 與 logs 如何表達這件事
