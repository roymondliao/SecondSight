# Config Unification — Technical Plan

## Problem Statement

SecondSight 的 config 系統有三個相互關聯的問題：

1. **Schema 存在，loader 不存在**：`analysis/config.py` 定義了 `GlobalAnalysisConfig` / `ProjectAnalysisConfig`，但 `runtime.py` 和 `analyze.py` 直接 `GlobalAnalysisConfig()` hardcode 建構，從不讀 TOML。Model selection 無論如何設定 config.toml 都不生效。

2. **Config 散落在兩個 module**：`analysis/config.py`（model selection schema）和 `storage/retention.py`（retention TTL loading）各自擁有 TOML parsing logic，第三個 config section 會產生第三份複製的 parsing code。

3. **Operator 無法驗證設定是否生效**：沒有 `secondsight config show`，沒有 init 產生的 config.toml template，operator 不知道設定有沒有被讀到。

## Source of Truth Decision (D-ST)

`~/.secondsight/config.toml` 是 global config 的 single source of truth。`secondsight init` 在首次執行時生成這個檔案（含所有 built-in defaults），後續 operator 修改此檔案。

若此檔案已存在，`init` **不覆蓋**，改為 diff 輸出（新增哪些 key）供 operator 手動合併。

## Architecture

### Priority Chain

```
env var (os.environ, 含從 .env 預載的值)
    ↓
per-project config.toml  (~/.secondsight/projects/<pid>/config.toml)
    ↓
global config.toml       (~/.secondsight/config.toml)
    ↓
built-in defaults
```

### .env 載入與 ${VAR} Interpolation

**載入順序**：`load_global_config()` 在讀取 TOML 之前，先嘗試讀取 `~/.secondsight/.env`。若存在，將其中的 key-value pairs 載入到 `os.environ`（non-overwriting：若 key 已存在於 os.environ 則不覆蓋）。

**效果**：pydantic-ai 從 `os.environ` 讀取 `ANTHROPIC_API_KEY` 等 secret，而這些 key 的值可以來自 `~/.secondsight/.env` 檔案，不需要 `export` 到 shell session。

**`${VAR}` Interpolation**：TOML parser 讀完後，loader 掃描所有 string value。符合 `${VAR_NAME}` pattern 的值從 `os.environ` 取替換值：
- 有值 → 替換
- 無值（var 不存在或為空字串）→ raise `SecondSightConfigError`（不 silently 留下 literal `${VAR_NAME}`）

**`.env` file 管理**：`.env` 由 operator 自行管理，`secondsight init` **不生成** `.env`。Config.toml template 內有 comment 說明如何搭配 `.env` 使用。

**`.env` parser 使用 `python-dotenv`**（已加入依賴）：
```python
from dotenv import load_dotenv
load_dotenv(dotenv_path=home / ".env", override=False)
# override=False → os.environ 已有的 key 不被 .env 覆蓋
```
`python-dotenv` 處理所有 edge cases（quoting、空格、comments、multi-line）。DC-7 由 library 保護。

### Package Structure（新建）

```
src/secondsight/config/
  __init__.py            # re-exports SecondSightConfig, load_config
  schema.py              # 所有 config dataclass 定義（從 analysis/config.py + storage/retention.py 遷移）
  loader.py              # SecondSightConfig 統一 loader：TOML merge + env var overlay
  env.py                 # env var 名稱常數 + extraction helpers
  template.py            # config.toml template 生成（供 init 使用）
```

### Env Var Scope（flat，單底線）

| Env Var | 對應 TOML key | Layer | 說明 |
|---------|--------------|-------|------|
| `SECONDSIGHT_HOME` | — | 已有 | — |
| `SECONDSIGHT_SERVER_URL` | — | 已有 | — |
| `SECONDSIGHT_ANALYSIS_MODEL` | `[analysis] model` | per-project | explicit override |
| `SECONDSIGHT_DEFAULT_AGENT` | `[analysis] default_agent` | global | explicit override |
| `ANTHROPIC_API_KEY` | — | os.environ | 由 .env 預載 → pydantic-ai 自動讀取 |
| `OPENAI_API_KEY` | — | os.environ | 同上 |
| `GEMINI_API_KEY` | — | os.environ | 同上 |

**API key 管理方式**：放在 `~/.secondsight/.env`，由 loader 在 startup 時載入到 `os.environ`，pydantic-ai 自然讀取，不需要 operator 手動 `export`。

### SecondSightConfig（統一 root）

```python
@dataclass(frozen=True)
class SecondSightConfig:
    retention: RetentionConfig
    analysis: GlobalAnalysisConfig
    project_analysis: ProjectAnalysisConfig  # per-project 覆蓋，只有 load_project_config 填入
```

`load_global_config(home) -> SecondSightConfig`：讀 `~/.secondsight/config.toml` + env var overlay
`load_project_config(home, project_id) -> SecondSightConfig`：同上，再疊 per-project TOML

## I/O Specification

### load_global_config(home: Path) -> SecondSightConfig

- **success**：返回 SecondSightConfig，所有欄位有值（可能是 builtin default）
- **failure**：TOML 解析錯誤 → raise `SecondSightConfigError` with 檔案位置 + 欄位名稱
- **unknown**：env var 值無效（例如 `SECONDSIGHT_DEFAULT_AGENT=invalid_agent`）→ raise `SecondSightConfigError`（不 silently fallback）

### secondsight init（新行為）

- **success**：config.toml 不存在 → 生成含 defaults 的 config.toml，印出路徑
- **success + merge-needed**：config.toml 已存在，無新增 key → 印出 "config.toml already up-to-date"
- **degradation**：config.toml 已存在，有新增 key（升級後新 feature）→ 印出 diff，提示 operator 手動合併，**不自動寫入**
- **failure**：config.toml 存在但 malformed → 印出錯誤，建議 `secondsight config validate`

### secondsight config show

- **success**：依序印出每層設定來源（env var / per-project / global / builtin）
- **failure**：TOML malformed → 印出錯誤行號
- **unknown**：TOML absent + env var 未設 → 顯示 "using all built-in defaults"（不是錯誤）

### secondsight config validate

- **success**：所有 TOML 檔案可解析，所有 key 型別正確，model name format 合法
- **failure**：TOML 解析失敗 / key 型別錯誤 / model name 未知 → 逐條列出，exit 1
- **unknown**：`default_agent = "auto"` 且無 session → 印出 warning（不是 error，runtime 才會 fail）

## I/O Specification — .env 相關

### load_global_config(home: Path) -> SecondSightConfig（更新）

- **success（.env 存在且合法）**：.env 載入 os.environ，TOML 讀取，`${VAR}` 展開，回傳 SecondSightConfig
- **success（.env 不存在）**：略過 .env 步驟，繼續讀 TOML（fresh install 合法路徑）
- **failure（.env 格式錯誤）**：raise `SecondSightConfigError` with 行號 + 說明
- **failure（`${VAR}` 指向不存在的 env var）**：raise `SecondSightConfigError`，說明哪個 key 缺哪個 env var，**不 silently 留 literal**
- **unknown（os.environ 有值但 .env 也有同 key）**：os.environ 優先（non-overwriting），.env 值被忽略。`secondsight config show` 需標示 "env_var (os)" vs "env_var (.env)" 來源

## Death Cases

### DC-1：Operator 設了 TOML 但 env var 覆蓋，調試時看 TOML 卻找不到問題

**Trigger**：`SECONDSIGHT_ANALYSIS_MODEL` 已設，operator 改了 config.toml 的 `model` 欄位
**Lie**：operator 以為改了 model，但 env var 優先
**Truth**：env var 未清，config.toml 的改動無效
**Detection**：`secondsight config show` 必須在每個欄位旁標示來源（`env_var` / `per_project_config` / `global_config` / `builtin_default`）

### DC-2：`model = ""` 空字串被當作「有設定」，從不 fallthrough

**Trigger**：operator 在 per-project config.toml 設 `model = ""`（空字串）
**Lie**：看起來是「清除設定，回到 global default」
**Truth**：如果 loader 把空字串當作有效值，它會覆蓋 global config 並回到 built-in default——但不是經由 global config 這條路
**Detection**：loader 必須明確 reject 空字串（raise `SecondSightConfigError`）或 treat 空字串為 "not set"（fallthrough）。必須測試兩個 behaviour 哪個是預期的，並在 schema 裡寫死。

### DC-3：`secondsight init` 在 config.toml 已存在時 silently 覆蓋

**Trigger**：operator 已設定 config.toml，upgrade 後重跑 `secondsight init`
**Lie**：init 完成，沒有 error
**Truth**：operator 的設定被清除，換回 defaults
**Detection**：init 必須在寫入前 check file existence，存在時走 diff path 而非 overwrite

### DC-4：Config 在 server startup 時讀入，TOML 檔案中途被修改

**Trigger**：operator 在 `secondsight serve` 執行中修改 config.toml
**Lie**：config 已更新
**Truth**：server 持有的是 startup 時讀入的舊 config，新設定直到 restart 才生效
**Detection**：`secondsight config show` 必須加入 "last loaded at" timestamp，README 需要說明 restart 才生效

### DC-6：`${VAR}` 解析後的值是空字串（env var 存在但值為 ""）

**Trigger**：`.env` 有 `ANTHROPIC_API_KEY=`（有 key 但沒有值），config.toml 有 `${ANTHROPIC_API_KEY}`
**Lie**：loader 展開成功，回傳空字串
**Truth**：空字串的 API key 傳給 pydantic-ai 會讓 LLM call 在 runtime 失敗，不在 config 載入時失敗
**Detection**：loader 對 `${VAR}` 展開後的值做 non-empty check；空字串 = 視同 VAR 不存在 → raise `SecondSightConfigError`

### DC-5：TOML 的 `model` 欄位 typo（`claude-haiku` 不含 date suffix）

**Trigger**：operator 設 `model = "claude-haiku-4-5"` 而非 `"claude-haiku-4-5-20251001"`
**Lie**：config 載入成功，analysis 等到 LLM call 才報錯
**Truth**：`validate` 時應該就能 flag unknown model format
**Detection**：`secondsight config validate` 需要 model name format check（regex 或 known-models allowlist）

## Assumptions（已接受的 undocumented gaps）

- **Model name validation scope**：validate 只做 format check（`claude-*`, `gpt-*`, `gemini-*`），不做 API 存活性 check（需要網路，太重）。Invalid format → error；Valid format 但 model 已停用 → 留給 LLM call 時發現。
- **Config hot-reload**：DC-4 接受為 known limitation，不實作 file watcher。
- **`secondsight config init` vs `secondsight init`**：init 生成 config.toml 是 `secondsight init` 的一部分，不是獨立的 subcommand。`secondsight config` subcommand 只有 `show` 和 `validate`。

## Files to Create

```
src/secondsight/config/
  __init__.py
  schema.py
  loader.py
  env.py
  template.py

tests/config/
  test_loader.py
  test_env.py
  test_template.py
  test_schema.py
```

## Files to Modify

```
src/secondsight/analysis/config.py        → schema classes 遷移到 config/schema.py 後，此檔案改為 re-export（backward compat）
src/secondsight/storage/retention.py      → RetentionConfig 遷移，此檔案改為 re-export
src/secondsight/analysis/runtime.py       → load_project_config() 取代 hardcoded GlobalAnalysisConfig()
src/secondsight/cli/analyze.py            → 同上
src/secondsight/cli/__init__.py or main   → 加入 `config` subcommand group
src/secondsight/installer/installer.py    → init 時生成 config.toml
```

## Implementation Order

Task 1 → Task 2 → Task 3 → Task 4 → Task 5

Task 3 依賴 Task 2（loader 需要先存在）。Task 4 和 Task 5 可平行。
