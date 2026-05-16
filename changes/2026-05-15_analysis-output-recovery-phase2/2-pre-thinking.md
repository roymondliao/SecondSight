## Planning Pre-thinking: Information Assumptions

To write this plan, I am assuming:

- Phase 1 已存在共享 helper，可作為 Phase 2 的基底，而不是從各 mode 現狀重新抽象。
- SDK path 雖然有 structured output，但仍可能因 validation/refusal/provider anomalies 產生 output-repair need。
- observability 的統一不需要在本 phase 就做 dashboard，只要先把 log / error_details / attempt accounting 語言統一。

Gaps I cannot resolve from Research:

- SDK path 哪些 provider/library exceptions 最適合歸類為 transport-retry eligible，現有研究尚未完全鎖定。
- `error_details` 是否要在本 phase 升級成 typed model，還是先維持 schema-stable dict + documented taxonomy。

Accepted undocumented assumptions:

- Phase 2 可以引入 mode-agnostic `RecoveryAttempt` / `ClassifiedFailure` 結構，而不必同步修改 DB schema。
