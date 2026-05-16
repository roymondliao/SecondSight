# Kickoff: analysis-output-recovery-phase2

## Problem Statement

Phase 1 只解掉 CLI output-parse path 的最小閉環。之後仍存在更大的系統性問題：

- SDK 與 CLI 的 retry / recovery 行為不一致
- output failures、transport failures、fatal failures 的分類策略分散
- `error_details` 與 observability 缺少統一語言，難以比較不同 mode / model 的失敗率

如果停在 Phase 1，系統會有「一半共享、一半 mode-specific」的中間態。Phase 2 的目標就是把這層提升成真正的 shared recovery layer。

## Goal

建立 CLI / SDK 共用的 analysis recovery contract，讓兩條 dispatch path 在 failure classification、retry policy、feedback、attempt accounting、telemetry 上對齊。

## Scope

### Must-Have

- SDK output-repair retry 對齊 Phase 1 contract
- transport / fatal / output failure 統一分類
- 共享 `RetryPolicy` / `FailureClassifier` / `RetryFeedbackBuilder`
- mode-agnostic attempt accounting
- 統一 observability fields / error_details taxonomy

### Explicitly Out of Scope

- adaptive retry policy
- per-provider dynamic tuning
- dashboard UI
- automatic model/provider blacklisting

## North Star

```yaml
metric:
  name: "analysis_recovery_consistency"
  definition: "CLI 與 SDK 在相同 failure class 下呈現相同 retry/no-retry 行為與可比較的 telemetry"
  current: "inconsistent by design"
  target: "shared policy for all output-repair paths; explicit divergence only where transport/provider semantics differ"
```
