# Overview: config-unification

## Goal

建立統一的 `src/secondsight/config/` package，讓所有 config sections（retention、analysis、model selection）透過單一 loader 從 TOML + env var 讀取，並接上目前 hardcoded 的 runtime/analyze 路徑，同時提供 operator UX（init 生成模板、config show/validate）。

## Architecture

Priority chain：env var > per-project config.toml > global config.toml > built-in default。新建 `src/secondsight/config/` package 集中 schema、loader、env var helpers 和 template 生成。`SecondSightConfig` 是 root dataclass，由 `load_global_config(home)` 和 `load_project_config(home, project_id)` 建構。`runtime.py` 和 `analyze.py` 改為呼叫這兩個函式，不再 hardcode `GlobalAnalysisConfig()`。

## Tech Stack

- `tomllib`（stdlib Python 3.11+）— TOML parsing（已用於 retention.py 和 analysis/config.py）
- `python-dotenv`（已加入依賴）— `~/.secondsight/.env` 載入 os.environ
- `os.environ`（stdlib）— env var reading + `${VAR}` interpolation source
- `dataclasses`（stdlib）— schema 定義（frozen=True）
- `typer`（已有）— `secondsight config` subcommand
- `pytest`（已有）— 所有 tests

## .env + ${VAR} 機制

`load_global_config()` 在讀 TOML 前，先用 `python-dotenv` 把 `~/.secondsight/.env` 載入 `os.environ`（non-overwriting）。TOML 讀完後，loader 掃描所有 string leaf value，`${VAR_NAME}` pattern 從 `os.environ` 展開。展開後空字串或 var 不存在 → raise。`.env` 由 operator 自行管理（init 不生成）。

## Key Decisions

- **Empty string = not set**：`model = ""` 在 TOML 裡視為「未設定」，loader fallthrough 到下一層。空字串是合法的 TOML，但語意上代表「清除 override」而非「設為空 model」。
- **Env var scope 最小化**：只有 `SECONDSIGHT_ANALYSIS_MODEL` 和 `SECONDSIGHT_DEFAULT_AGENT` 開放 env var override；複雜 nested config（retention TTL、denylist）只走 TOML。
- **Init 不覆蓋**：`secondsight init` 偵測到 config.toml 已存在時走 diff path，不 overwrite。
- **Config hot-reload 不實作**：server 在 startup 時讀 config，中途修改 TOML 需 restart 才生效（DC-4 accepted limitation）。
- **Backward compat via re-export**：`analysis/config.py` 和 `storage/retention.py` 改為從 `config/schema.py` re-export，外部 caller 不需改動 import path。

## Death Cases Summary

1. **DC-1**：env var 覆蓋 TOML 但 operator 不知道 → `secondsight config show` 標示每欄來源
2. **DC-3**：`secondsight init` 覆蓋 operator 已有 config.toml → check existence，走 diff path
3. **DC-6**：`${VAR}` 指向不存在或空值的 env var → loader raise，不 silently 留 literal

## File Map

**新建**
- `src/secondsight/config/__init__.py` — re-exports SecondSightConfig, load_global_config, load_project_config
- `src/secondsight/config/template.py` — config.toml template 生成（含 ${VAR} 使用說明 comment）
- `src/secondsight/config/schema.py` — 所有 config dataclass 定義（遷移自 analysis/config.py + retention schema）
- `src/secondsight/config/loader.py` — 統一 TOML loader + env var overlay
- `src/secondsight/config/env.py` — env var 名稱常數 + extraction helpers
- `src/secondsight/config/template.py` — config.toml template 生成
- `tests/config/test_loader.py`
- `tests/config/test_env.py`
- `tests/config/test_template.py`
- `tests/config/test_schema.py`

**修改**
- `src/secondsight/analysis/config.py` — 改為 re-export from config/schema.py
- `src/secondsight/storage/retention.py` — RetentionConfig 遷移後 re-export
- `src/secondsight/analysis/runtime.py` — hardcoded GlobalAnalysisConfig() 換成 load_project_config()
- `src/secondsight/cli/analyze.py` — 同上
- `src/secondsight/cli/` — 新增 config subcommand（show, validate）
- `src/secondsight/installer/installer.py` — init 時生成 config.toml
