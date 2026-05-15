# Kickoff: analysis-output-recovery-phase1

## Problem Statement

SecondSight 的 analysis dispatch 已經支援 CLI / SDK 兩條路，但目前只有 CLI path 有「輸出失敗後重試」的雛形，而且它仍有三個明顯缺口：

1. 常見格式噪音（例如 fenced JSON、前後夾雜說明文字）直接被當成 `json_decode` failure，浪費一次 LLM retry。
2. retry 次數被 hardcode，且 `AnalysisOutput.retry_count` contract 綁死在 `<= 2`，無法透過 config 調整。
3. feedback 是把原始 validation error 直接附回 prompt，沒有先做錯誤分類，也沒有結構化裁剪。

最近實際失敗案例已證明：Claude CLI 明明回了可用內容，卻因為包了 ```` ```json ... ``` ```` 而整體失敗。這代表目前系統把「本地可修復格式錯誤」與「真的需要模型重出一次」混在一起處理，造成成本與穩定性都不理想。

## Goal

Phase 1 只做一件事：**把 output recovery 建成可 ship 的最小閉環**，先穩定 CLI mode，並為 Phase 2 的 shared recovery layer 預留乾淨的 contract。

## Scope

### Must-Have

- CLI output normalization（code fence、前後噪音、第一個 top-level JSON object 抽取）
- 最小共享 failure taxonomy（只涵蓋 output/validation path）
- 結構化 feedback builder（只處理 output-repair 類錯誤）
- config 化的 output-repair retry policy
- `AnalysisOutput.retry_count` contract 調整，使其不再硬綁 `<= 2`
- CLI dispatcher 改接上述 policy / classifier / normalizer
- 完整測試與 migration 說明

### Explicitly Out of Scope

- SDK dispatcher 的 output-repair retry
- transport retry / backoff
- provider-specific retry policies
- telemetry dashboard / metrics aggregation
- adaptive retry policy

## Why This Is A Separate Change

這個 phase 的目標不是「把 recovery 平台一次做完」，而是把目前最常見、最便宜可修的 failure surface 先關掉：

- fenced JSON 不應該消耗一次 LLM retry
- retry policy 不應該再是 hardcode
- feedback 不應該再是原始 exception 全量灌回 prompt

如果把 SDK alignment、transport retry、shared engine 抽象一起做，這個 change 會同時碰 config、contract、CLI、SDK、metrics，失敗面過大，不利於安全 ship。

## North Star

```yaml
metric:
  name: "cli_output_recovery_success_rate"
  definition: "% of CLI analysis attempts that end with a valid AnalysisOutput after local normalization + bounded output-repair retry"
  current: "unknown; fenced-JSON real failure proves current rate is below desired reliability"
  target: ">= 99% for normalizable format noise; >= 95% overall for output-parse path"
  corruption_signature: "normalizable outputs still consume LLM retries, or retry_count grows while failure reason remains format-only"
```
