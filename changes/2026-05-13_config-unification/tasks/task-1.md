# Task 1: 建立 src/secondsight/config/ package：schema + env + 遷移舊 config classes

## Context

Read: overview.md

本 task 建立 `src/secondsight/config/` package 的骨架，並將散落在兩處的 config schema 遷移過來：
- `src/secondsight/analysis/config.py` 有：`AnalysisConfig`, `GlobalAnalysisConfig`, `ProjectAnalysisConfig`, `ModelsConfig`, `FallbackModelsConfig`
- `src/secondsight/storage/retention.py` 有：`RetentionConfig`（schema 部分，不含 purge logic）

遷移後，兩個原始檔案改為 re-export（backward compatibility），外部 caller 不需修改 import path。

本 task **不實作 loader**（loader 在 task-2），只負責 schema 定義和 env var 常數。

## Files

- Create: `src/secondsight/config/__init__.py`
- Create: `src/secondsight/config/schema.py`
- Create: `src/secondsight/config/env.py`
- Modify: `src/secondsight/analysis/config.py` — 改為 re-export from config/schema.py
- Modify: `src/secondsight/storage/retention.py` — RetentionConfig re-export（保留 purge logic 原地）
- Create: `tests/config/__init__.py`
- Create: `tests/config/test_schema.py`
- Create: `tests/config/test_env.py`

## Death Test Requirements

在實作前必須先寫並跑失敗的 death tests：

- **DT-schema-1**: `model = ""` 空字串傳入 `ProjectAnalysisConfig` 時，loader 在後續 task 不能把它當 non-empty 值使用。本 task 在 schema 層面明確標示空字串語意（docstring 說明），並寫 test 驗證 `ProjectAnalysisConfig(model="").model == ""`（schema 本身不 reject，rejection 在 loader 層）。
- **DT-env-1**: `SECONDSIGHT_ANALYSIS_MODEL` 讀到空字串時（`export SECONDSIGHT_ANALYSIS_MODEL=""`），env.py 的 helper 必須回傳 `None`，而非空字串（空字串 env var = not set）。

## Implementation Steps

- [ ] Step 1: 寫 death tests（test_schema.py + test_env.py 的 DT 測項）
- [ ] Step 2: 跑 death tests — 確認它們 fail（因為還沒實作）
- [ ] Step 3: 建立 `src/secondsight/config/__init__.py`（空的，僅 package marker）
- [ ] Step 4: 建立 `src/secondsight/config/schema.py`
  - 複製 `AnalysisConfig`, `GlobalAnalysisConfig`, `ProjectAnalysisConfig`, `ModelsConfig`, `FallbackModelsConfig` 從 `analysis/config.py`
  - 複製 `RetentionConfig` dataclass 定義（不含 `_resolve_ttl_field` 等 loading helpers，那些留在 `retention.py`）
  - 新增 `SecondSightConfig` root dataclass（欄位：`retention: RetentionConfig`, `analysis: GlobalAnalysisConfig`, `project_analysis: ProjectAnalysisConfig`）
  - 新增 `SecondSightConfigError`（統一的 config error class）
- [ ] Step 5: 建立 `src/secondsight/config/env.py`
  - 常數：`ENV_ANALYSIS_MODEL = "SECONDSIGHT_ANALYSIS_MODEL"`、`ENV_DEFAULT_AGENT = "SECONDSIGHT_DEFAULT_AGENT"`
  - `get_env_analysis_model() -> str | None`：讀 env var，空字串回傳 None
  - `get_env_default_agent() -> str | None`：同上
- [ ] Step 6: 修改 `analysis/config.py` — 在原有 class 前加 import + 讓原有名稱指向 schema.py 的版本（`from secondsight.config.schema import ...`）。保留原有 `__all__`。
- [ ] Step 7: 修改 `storage/retention.py` — `RetentionConfig` 改為 from schema.py import；`BUILTIN_DEFAULT_TTL_DAYS` 等常數保留在 retention.py（它們是 retention-specific 的）
- [ ] Step 8: 跑所有 tests — 確認 0 regression
- [ ] Step 9: 寫 scar report

## Expected Scar Report Items

- **Scar-1**: `RetentionConfig` 在 `storage/retention.py` 同時承擔 schema 和 purge orchestration。遷移 schema 時要注意不要連 purge logic（`enumerate_expired_sessions`, `RawTracesPurger`）也移走——那些屬於 storage layer，不屬於 config layer。
- **Scar-2**: `analysis/config.py` 目前有 `AnalysisConfig.load()` 這個 classmethod，它讀的是 `[analysis.read_project_file]` section。這個 load 邏輯在 task-2 的 loader 裡會被整合。本 task 先**保留** `AnalysisConfig.load()` 不動，task-2 再決定整合策略。
- **Scar-3**: `_verify_adapter_registry_consistency()` 在 `sdk/model_selection.py` import 時執行。遷移 `ModelsConfig` 到 `config/schema.py` 後，這個驗證的 import chain 需要確認不會產生 circular import。

## Acceptance Criteria

- Covers: "model = '' 空字串被 silently 視為有效值"（schema 層的語意標示）
- Covers: "env var SECONDSIGHT_ANALYSIS_MODEL 覆蓋所有 TOML 層"（env.py 的空字串 = None 行為）
