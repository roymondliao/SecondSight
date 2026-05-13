# Task 3: 接上 runtime.py + analyze.py：以 load_project_config() 取代 hardcoded GlobalAnalysisConfig()

## Context

Read: overview.md

本 task 是整個 config unification 的「接線」任務——讓 model selection 真正從 TOML 讀取，而非永遠使用 built-in defaults。

**現狀（要修掉的 code）**：

`src/secondsight/analysis/runtime.py:_build_analysis_agent()` 第 76-80 行：
```python
global_cfg = GlobalAnalysisConfig()   # hardcoded，不讀 TOML
project_cfg = ProjectAnalysisConfig() # hardcoded，不讀 TOML
primary, fallbacks = select_model(
    project_id=project_id,
    project_config=SimpleNamespace(analysis=project_cfg),
    global_config=SimpleNamespace(analysis=global_cfg),
    ...
)
```

`src/secondsight/cli/analyze.py:_build_orchestrator()` 第 456-458 行：
```python
global_cfg = GlobalAnalysisConfig()   # 同樣問題
project_cfg = ProjectAnalysisConfig()
```

**目標**：呼叫 `load_project_config(home, project_id)` 取代 hardcode，讓 operator 的 config.toml 設定真正生效。

**依賴 task-2**：`load_project_config()` 必須先存在。

## Files

- Modify: `src/secondsight/analysis/runtime.py:60-93`（`_build_analysis_agent` 函式）
- Modify: `src/secondsight/cli/analyze.py:399-489`（`_build_orchestrator` 函式）
- Modify: `src/secondsight/cli/analyze.py:355-396`（`_build_in_process_trigger`，傳入 home path 給 runtime）
- Create: `tests/config/test_runtime_wiring.py`

## Death Test Requirements

在實作前必須先寫並跑失敗的 death tests：

- **DT-wire-1**: per-project config.toml 有 `model = "claude-sonnet-4-6"`，執行 `_build_analysis_agent()` 後，`agent.router.config.primary.name` 必須是 `"claude-sonnet-4-6"`（不是 built-in default `"claude-haiku-4-5-20251001"`）。**這個 test 在修改前必然 fail，修改後必然 pass。**
- **DT-wire-2**: `SECONDSIGHT_ANALYSIS_MODEL=claude-opus-4-7` 設於 env，config.toml 無設定 → `agent.router.config.primary.name == "claude-opus-4-7"`
- **DT-wire-3**: config.toml 完全不存在 → `_build_analysis_agent()` 不 raise，使用 built-in default `"claude-haiku-4-5-20251001"`

## Implementation Steps

- [ ] Step 1: 寫 death tests（test_runtime_wiring.py）— 全部 fail
- [ ] Step 2: 跑 death tests — 確認 fail
- [ ] Step 3: 修改 `runtime.py:_build_analysis_agent()`
  - 函式簽名加入 `secondsight_home: Path`
  - 呼叫 `load_project_config(secondsight_home, project_id)` 取得 `cfg`
  - `primary, fallbacks = select_model(project_id=project_id, project_config=cfg, global_config=cfg, events_repo=events_repository)`
  - 注意：`select_model` 的 `project_config` 和 `global_config` 兩個參數現在都由同一個 `SecondSightConfig` 提供（它同時含有 global 和 project 層）
- [ ] Step 4: 修改 `runtime.py:build_project_analysis_runtime()`
  - 加入 `secondsight_home: Path` 參數
  - 傳入 `_build_analysis_agent()`
- [ ] Step 5: 修改 `cli/analyze.py:_build_in_process_trigger()`
  - 已有 `secondsight_home: Path` 參數，傳入 `build_project_analysis_runtime()`
- [ ] Step 6: 修改 `cli/analyze.py:_build_orchestrator()`（舊的 helper）
  - 同樣呼叫 `load_project_config()` 取代 hardcode
  - 注意：`_build_orchestrator` 和 `_build_in_process_trigger` 存在功能重複，本 task 不合併（task scope 邊界），記錄在 scar
- [ ] Step 7: 跑所有 tests — 確認 pass，0 regression
- [ ] Step 8: 寫 scar report

## Expected Scar Report Items

- **Scar-1**: `runtime.py:_build_analysis_agent()` 和 `cli/analyze.py:_build_orchestrator()` 有部分重複的 orchestrator 建構 logic（兩者都 build LLMRouter + AnalysisTools + Orchestrator + Trigger）。這是 pre-existing 的 duplication。本 task 不合併（refactor scope 太大），記錄在 scar 供後續 iteration 處理。
- **Scar-2**: `select_model()` 的 `project_config` 和 `global_config` 的型別是 `object`（structural typing），它讀 `.analysis.model` 和 `.analysis.default_agent`。`SecondSightConfig` 有 `project_analysis: ProjectAnalysisConfig` 和 `analysis: GlobalAnalysisConfig`。需要確認 `select_model()` 的 attribute access path 與 `SecondSightConfig` 的 field names 一致，否則 fallthrough silently。
- **Scar-3**: `load_project_config()` 在 `_build_analysis_agent()` 裡每次被呼叫（每個 analysis run 都呼叫），會重複讀 TOML 檔案。DC-4 接受 config hot-reload 不實作，但不必要的重複 IO 是可接受的 v1 scar（file read 代價很低）。

## Acceptance Criteria

- Covers: "per-project model 覆蓋 global default"
- Covers: "env var SECONDSIGHT_ANALYSIS_MODEL 覆蓋所有 TOML 層"
- Covers: "global config.toml 不存在 → fallback 到 built-in defaults"（wiring 層的驗證）
