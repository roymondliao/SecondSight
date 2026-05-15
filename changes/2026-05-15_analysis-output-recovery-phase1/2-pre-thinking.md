## Planning Pre-thinking: Information Assumptions

To write this plan, I am assuming:

- `AnalysisOutput` 是兩個 mode 共用 contract，因此 Phase 1 對 `retry_count` 的調整不能只修 CLI caller，必須同步修 contract 與測試。
- CLI dispatcher 已有 retry 流程與 prompt augmentation，可作為 Phase 1 的切入點，不需重寫整個 dispatch path。
- Phase 2 將重用 Phase 1 的 failure taxonomy / feedback builder，因此 Phase 1 不能把設計寫死在 Claude-only 特例。
- 使用者接受先讓 CLI path 吃到 shared helper，而 SDK path 留到下一個 change。

Gaps I cannot resolve from Research:

- `retry_count` 的最終全域上限要用固定 hard cap 還是完全跟 config 同步，既有文件尚未鎖定。
- `error_details` 是否需要在 Phase 1 就改成 typed sub-model，還是維持 `dict[str, Any]` 到 Phase 2 再演進。

Uncertainties:

- 若 Codex CLI 未來輸出雜訊形式與 Claude 明顯不同，Phase 1 shared normalizer 的責任邊界可能需要再切細。

Accepted undocumented assumptions carried into this plan:

- `retry_count` contract 在 Phase 1 可從 `<= 2` 放寬為「受 config 驗證 + 全域 hard cap 保護」。
- `error_details` 在 Phase 1 維持 dict 形狀，但會補齊分類與 normalization forensics 欄位。
