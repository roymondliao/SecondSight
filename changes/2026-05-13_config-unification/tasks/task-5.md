# Task 5: 新增 secondsight config subcommand：show（含來源標示）、validate（含 model format check）

## Context

Read: overview.md

本 task 建立 `secondsight config` CLI subcommand group，提供兩個 subcommand：

- `secondsight config show [--project PROJECT_ID]`：顯示 effective config（所有層 merge 後的結果），每個欄位標示來源（`env_var` / `per_project_config` / `global_config` / `builtin_default`）
- `secondsight config validate [--project PROJECT_ID]`：驗證所有 config.toml 檔案（格式 + 型別 + model name format），exit 0 on success，exit 1 on error

這兩個 subcommand 是 DC-1（env var 覆蓋 TOML 但 operator 不知道）的主要 detection mechanism。

**依賴 task-2**：`load_global_config()` 和 `load_project_config()` 必須先存在。

## Files

- Create: `src/secondsight/cli/config_cmd.py`
- Modify: `src/secondsight/cli/__init__.py` 或主 CLI entry（加入 `config` subgroup）
- Create: `tests/cli/test_config_cmd.py`

## Death Test Requirements

在實作前必須先寫並跑失敗的 death tests：

- **DT-show-1**: `SECONDSIGHT_ANALYSIS_MODEL=claude-opus-4-7`，config.toml 有 `model = "claude-haiku-4-5-20251001"` → `secondsight config show` 輸出中 model 欄位顯示 `claude-opus-4-7 [env_var]`（env_var 來源標示）
- **DT-show-2**: 無任何 config.toml，無 env var → `secondsight config show` 所有欄位顯示 `[builtin_default]`，exit 0（不是 error）
- **DT-show-3**: config.toml 有 `model = "${MY_MODEL}"`，`MY_MODEL=claude-sonnet-4-6` → show 顯示 `claude-sonnet-4-6 [env_var interpolation]`（區分 direct env_var 和 interpolation 來源）
- **DT-validate-1**: config.toml 有 `model = "claude-haiku-4-5"`（缺 date suffix）→ `secondsight config validate` exit 1，印出 model format error
- **DT-validate-2**: config.toml 有 `${MISSING_VAR}` → validate exit 1，印出哪個 key 缺哪個 env var
- **DT-validate-3**: 所有 config 合法 → validate exit 0，印出 "N config file(s) validated, 0 errors"

## Implementation Steps

- [ ] Step 1: 寫 death tests（test_config_cmd.py）— 全部 fail
- [ ] Step 2: 跑 death tests — 確認 fail
- [ ] Step 3: 定義 `SourcedValue` dataclass（供 show 使用）
  ```python
  @dataclass
  class SourcedValue:
      value: Any
      source: Literal["env_var", "env_var_interpolation", "per_project_config", "global_config", "builtin_default"]
  ```
- [ ] Step 4: 實作 loader 的 source tracking 版本（`load_project_config_with_sources(home, project_id) -> dict[str, SourcedValue]`）
  - 為每個 config key 記錄它的值和來源
  - 這是 `load_project_config()` 的 introspection 版本，供 `config show` 使用
- [ ] Step 5: 實作 `secondsight config show` subcommand
  - 格式：`model = claude-opus-4-7  [env_var]`
  - 若 value 含 `${VAR}` 已展開，標示 `[env_var interpolation: MY_MODEL]`
  - 若 .env 有此 key（而非 shell env），標示 `[.env]`
  - Group by section 輸出（`[retention]`, `[analysis]`, etc.）
  - 最後一行印出：`Config last loaded at: <ISO 8601 timestamp>`（DC-4 detection）
- [ ] Step 6: 實作 model name format validator
  - Valid patterns：`claude-*-YYYYMMDD`（e.g., `claude-haiku-4-5-20251001`）、`gpt-4*`、`gpt-o*`、`gemini-*`
  - 純 prefix match（`claude-`、`gpt-`、`gemini-`）without version → warn，不 error（保守：未來有新 model 不應 block）
  - 完全不符任何 known prefix → warn（不 error，operator 可能用 private model）
  - Empty string → error（[analysis] model = "" 是 "not set"，但 validate 時如果出現空字串 model 要警告）
- [ ] Step 7: 實作 `secondsight config validate` subcommand
  - 讀取 global config.toml（若存在）
  - 讀取 per-project config.toml（若 --project 有指定）
  - 嘗試 `load_global_config()` / `load_project_config()`，捕捉 `SecondSightConfigError`
  - 額外做 model format check
  - 列出所有 warnings 和 errors
  - exit 1 if any error，exit 0 if only warnings or clean
- [ ] Step 8: 在主 CLI 加入 `config` subgroup
  - 找到 `src/secondsight/cli/__init__.py` 或 `src/secondsight/__main__.py` 的 `app.add_typer()` 位置
  - 加入 `from secondsight.cli.config_cmd import app as config_app`
  - `app.add_typer(config_app, name="config")`
- [ ] Step 9: 跑所有 tests — 確認 pass
- [ ] Step 10: 寫 scar report

## Expected Scar Report Items

- **Scar-1**: `load_project_config_with_sources()` 和 `load_project_config()` 會有 logic 重複。理想做法是 `load_project_config()` 回傳 sources 作為 optional output，但這會改變 public API。v1 接受兩個獨立函式，記錄在 scar。
- **Scar-2**: `[.env]` 來源標示需要知道某個 env var 是從 `.env` 載入還是從 shell env 來的。`python-dotenv` 的 `load_dotenv()` 不回傳它載入了哪些 key。需要在 `_load_dotenv_if_exists()` 裡先讀 .env keys，再比對 os.environ，才能判斷來源。如果不值得實作，改為標示 `[env]`（不區分 .env 和 shell），記錄在 scar。
- **Scar-3**: `secondsight config show` 的輸出格式沒有 machine-readable option（JSON flag）。未來 operator tooling 可能需要。v1 接受 human-readable only，記錄在 scar。

## Acceptance Criteria

- Covers: "Env var 覆蓋 TOML 但 config show 不顯示來源"（DC-1 detection mechanism）
- Covers: "TOML model typo 在 config load 時不報錯"（DC-5 validate 提前發現）
- Covers: "${VAR} 指向不存在的 env var → 明確 error"（validate 偵測）
- Covers: "secondsight config validate 在所有設定合法時 exit 0"
