# Plan: analysis-output-recovery-phase1

## 1. Architecture

Phase 1 引入最小 recovery pipeline，僅套用在 CLI dispatch path：

```
raw stdout
  -> envelope extract
  -> output normalization
  -> failure classification
  -> AnalysisOutput validation
  -> structured feedback
  -> bounded output-repair retry
```

其中只有 `output normalization` 與 `failure classification / feedback builder / retry policy` 是新共享元件；subprocess lifecycle 仍留在 CLI dispatcher。

## 2. New Shared Components

### 2.1 `analysis/output_recovery.py`

新增共享 helper module，承載：

- `FailureClass`
  - `normalizable_format_error`
  - `json_decode`
  - `schema_mismatch`
  - `fatal_execution_error`
- `OutputRecoveryPolicy`
  - `enabled: bool`
  - `output_repair_max_attempts: int`
  - `feedback_max_chars: int`
  - `global_max_attempts_cap: int`
- `normalize_llm_json_text(raw: str) -> NormalizationResult`
- `build_retry_feedback(failure: ClassifiedFailure) -> str`

### 2.2 Config

在 `[analysis]` 下加入新子節：

```toml
[analysis.retry]
enabled = true
output_repair_max_attempts = 2
feedback_max_chars = 1200
```

規則：

- Phase 1 只允許 output-repair retry
- `output_repair_max_attempts` 必須 `>= 0`
- 另有程式內 hard cap，例如 `<= 5`，避免配置失控

## 3. AnalysisOutput Contract

`AnalysisOutput.retry_count` 不再 hard 綁 `<= 2`。Phase 1 改為：

- `ge = 0`
- `le = GLOBAL_MAX_RETRY_COUNT_CAP`

理由：

- retry count 已經要受 config policy 控制，contract 若仍寫死 `2`，會與 policy 衝突
- 仍保留 hard cap，避免 DB / output contract 無上界膨脹

## 4. CLI Dispatcher Changes

CLI dispatcher 的 retry loop 改為：

1. 執行 subprocess，取得 raw stdout/stderr
2. 先做 Claude envelope extract
3. 做 normalization
4. 若 normalization 後可直接 validate 成功，視為成功且不消耗 retry
5. 若 validate / parse 失敗，分類為 `json_decode` 或 `schema_mismatch`
6. 依 policy 決定是否重試
7. retry prompt 僅附結構化 feedback，不直接拼接整段原始 exception

### Normalization Scope

Phase 1 只處理這些本地可修復 case：

- fenced JSON block
- JSON 前置雜訊
- JSON 後置雜訊
- 提取第一個 top-level `{...}`

Phase 1 明確不做：

- 自動補逗號
- 修正引號
- 修改欄位名

那些屬於真正 malformed JSON / schema mismatch，應進入 retry 而不是靜默修復。

## 5. Death Cases

### DC1

Trigger:
normalizable fenced JSON 仍被當成 `json_decode` failure

The lie:
retry 機制正常運作

The truth:
系統把本地可修復噪音拿去消耗 LLM call

Detection:
存在 fenced JSON 測試，但 `retry_count > 0`

### DC2

Trigger:
feedback 直接攜帶完整原始 exception / raw output，長度不受控

The lie:
模型拿到更多資訊比較容易修正

The truth:
prompt 被錯誤訊息污染，穩定性反而下降

Detection:
feedback builder 未套 `feedback_max_chars`

### DC3

Trigger:
policy config 設成 3，但 `AnalysisOutput` 仍只接受 `retry_count <= 2`

The lie:
config 生效

The truth:
dispatcher 自己在成功或失敗出包時被 contract 打回

Detection:
新增 config 驗證與 output contract boundary tests

## 6. File Map

- Create: `src/secondsight/analysis/output_recovery.py`
- Modify: `src/secondsight/config/schema.py`
- Modify: `src/secondsight/config/loader.py`
- Modify: `src/secondsight/analysis/output.py`
- Modify: `src/secondsight/analysis/cli_dispatcher.py`
- Test: `tests/analysis/test_output_recovery.py`
- Modify: `tests/analysis/test_cli_dispatcher.py`
- Modify: `tests/analysis/test_output_contract.py`
- Modify: `tests/config/test_*` for retry config loading

## 7. Phase Boundary

Phase 1 結束時，系統應具備：

- CLI output normalization
- config-driven output-repair retry
- 結構化 feedback
- 可被 SDK 重用的最小 recovery helpers

但不包含：

- SDK dispatcher adoption
- transport retry
- shared orchestration layer
