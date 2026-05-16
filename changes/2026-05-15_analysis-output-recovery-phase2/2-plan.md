# Plan: analysis-output-recovery-phase2

## 1. Architecture

Phase 2 把 Phase 1 的 helper 提升成 shared recovery layer：

```
executor (CLI subprocess / SDK call)
  -> raw failure evidence
  -> shared classification
  -> shared retry policy
  -> shared feedback builder
  -> executor-specific retry
  -> shared attempt accounting + shared error_details taxonomy
```

## 2. Shared Components

### 2.1 Failure Taxonomy Expansion

Phase 2 在 Phase 1 基礎上擴充為：

- `normalizable_format_error`
- `json_decode`
- `schema_mismatch`
- `transport_timeout`
- `transport_rate_limit`
- `transport_api_error`
- `fatal_auth_or_config`
- `fatal_execution_error`

### 2.2 Shared Models

- `RecoveryAttempt`
- `ClassifiedFailure`
- `RetryDecision`
- `RecoveryTrace`

這些型別不直接替代 `AnalysisOutput`，而是作為 dispatcher 內部共享語言。

## 3. SDK Adoption

SDK dispatcher 對齊 Phase 1 / 2 contract：

- structured output validation 失敗時走 shared output-repair retry
- provider/transport failures 走 shared classifier + retry decision
- fatal auth/config failures fail fast

重點：SDK 與 CLI 共用 policy 與語義，但不共用 executor。

## 4. Observability Contract

在不增加 dashboard scope 的前提下，Phase 2 至少統一：

- `error_details["reason"]`
- `error_details["failure_class"]`
- `error_details["attempts"]`
- `error_details["retry_exhausted"]`

並要求 CLI / SDK 都留下可比較的 log/forensics。

## 5. Death Cases

### DC1

Trigger:
CLI 與 SDK 對同一個 schema mismatch 採不同 retry policy

The lie:
兩個 mode 都支援 recovery

The truth:
使用者無法預測 mode 切換後的結果與成本

### DC2

Trigger:
transport error 被誤判成 output-repair retry

The lie:
模型會透過看到 feedback 修正問題

The truth:
根因不在輸出內容，而在 provider/transport；重試 feedback 毫無意義

### DC3

Trigger:
shared recovery layer 將 mode-specific executor 細節抽得太乾淨，失去必要 forensics

The lie:
抽象完成

The truth:
除錯資訊被吞掉，平台化以可觀測性為代價

## 6. File Map

- Modify: `src/secondsight/analysis/output_recovery.py`
- Modify: `src/secondsight/analysis/cli_dispatcher.py`
- Modify: `src/secondsight/analysis/sdk_dispatcher.py`
- Test: `tests/analysis/test_output_recovery.py`
- Modify: `tests/analysis/test_cli_dispatcher.py`
- Modify: `tests/analysis/test_sdk_dispatcher.py`

## 7. Exit Condition

Phase 2 完成時，CLI 與 SDK 對 output-repair failures 的：

- classification
- retry decision
- feedback shape
- attempt accounting
- `error_details` taxonomy

都必須一致；僅 transport/provider-specific evidence 允許 mode 差異。
