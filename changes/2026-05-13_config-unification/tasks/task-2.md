# Task 2: 實作統一 loader：SecondSightConfig, load_global_config, load_project_config（含 .env 載入 + ${VAR} interpolation）

## Context

Read: overview.md

本 task 實作 `src/secondsight/config/loader.py`，這是整個 config unification 的核心。

**依賴 task-1**：schema classes（`SecondSightConfig`, `GlobalAnalysisConfig`, `ProjectAnalysisConfig`, `RetentionConfig`, `SecondSightConfigError`）和 env helpers（`get_env_analysis_model`, `get_env_default_agent`）必須先存在。

**Priority chain（高→低）**：
1. Env var（`SECONDSIGHT_ANALYSIS_MODEL`, `SECONDSIGHT_DEFAULT_AGENT`）
2. Per-project config.toml（`~/.secondsight/projects/<pid>/config.toml`）
3. Global config.toml（`~/.secondsight/config.toml`）
4. Built-in defaults

**`.env` 載入**：`load_global_config()` 在讀取 TOML 前，先用 `python-dotenv` 把 `~/.secondsight/.env` 載入 `os.environ`（`override=False`）。

**`${VAR}` interpolation**：TOML 載入後，掃描所有 string leaf value，符合 `${VAR_NAME}` pattern 的從 `os.environ` 展開。VAR 不存在或展開後為空字串 → raise `SecondSightConfigError`。

## Files

- Create: `src/secondsight/config/loader.py`
- Create: `tests/config/test_loader.py`

## Death Test Requirements

在實作前必須先寫並跑失敗的 death tests：

- **DT-loader-1**: `${MISSING_KEY}` 在 TOML string value 中，os.environ 沒有此 key → `load_global_config()` raise `SecondSightConfigError`，不回傳含 literal `${MISSING_KEY}` 的 config
- **DT-loader-2**: `${EMPTY_KEY}` 展開後是空字串（`os.environ["EMPTY_KEY"] = ""`）→ raise `SecondSightConfigError`
- **DT-loader-3**: `.env` 存在且有 `ANTHROPIC_API_KEY=sk-test`，`os.environ` 沒有此 key → `load_global_config()` 後 `os.environ.get("ANTHROPIC_API_KEY") == "sk-test"`
- **DT-loader-4**: `.env` 有 `ANTHROPIC_API_KEY=from_dotenv`，`os.environ` 也有 `ANTHROPIC_API_KEY=from_env`（不同值）→ `load_global_config()` 後 `os.environ["ANTHROPIC_API_KEY"]` == `"from_env"`（os.environ 優先，.env 不覆蓋）
- **DT-loader-5**: per-project config 有 `model = "claude-sonnet-4-6"`，global config 有 `default_agent = "codex"` → `load_project_config()` 的 `project_analysis.model == "claude-sonnet-4-6"`（per-project 優先）
- **DT-loader-6**: global config 不存在 → `load_global_config()` 回傳 built-in defaults，不 raise

## Implementation Steps

- [ ] Step 1: 寫所有 DT（test_loader.py）— 全部 fail
- [ ] Step 2: 跑 death tests — 確認 fail
- [ ] Step 3: 實作 `_interpolate_vars(value: str, env: dict) -> str`
  - regex: `\$\{([A-Z_][A-Z0-9_]*)\}`
  - 找到 match → os.environ 查找
  - 無此 key 或值為空字串 → raise `SecondSightConfigError(f"config value '${{{var_name}}}' references missing or empty env var {var_name!r}")`
  - 有值 → 替換
- [ ] Step 4: 實作 `_interpolate_dict(doc: dict) -> dict`
  - 遞迴掃描所有 string leaf value，對每個呼叫 `_interpolate_vars`
  - 非 string value 原樣保留
- [ ] Step 5: 實作 `_load_dotenv_if_exists(dotenv_path: Path) -> None`
  ```python
  from dotenv import load_dotenv
  if dotenv_path.is_file():
      load_dotenv(dotenv_path=dotenv_path, override=False)
  ```
- [ ] Step 6: 實作 `_parse_toml(path: Path) -> dict | None`
  - 不存在 → return None（fresh install 合法路徑）
  - 存在但 malformed → raise `SecondSightConfigError` with 路徑 + exc
  - 成功 → 呼叫 `_interpolate_dict()` 後 return
- [ ] Step 7: 實作 `_build_global_analysis_config(doc: dict) -> GlobalAnalysisConfig`
  - 讀 `[analysis]` section，合入 env var overlay（`SECONDSIGHT_DEFAULT_AGENT`）
  - 讀 `[analysis.models]` 和 `[analysis.models.fallback]`
- [ ] Step 8: 實作 `_build_retention_config(global_doc, project_doc) -> RetentionConfig`
  - 複用 `storage/retention.py` 現有的 `_resolve_ttl_field` logic（不複製，直接 import）
- [ ] Step 9: 實作 `load_global_config(home: Path) -> SecondSightConfig`
  ```python
  def load_global_config(home: Path) -> SecondSightConfig:
      _load_dotenv_if_exists(home / ".env")
      global_doc = _parse_toml(home / "config.toml") or {}
      return SecondSightConfig(
          retention=_build_retention_config(global_doc, {}),
          analysis=_build_global_analysis_config(global_doc),
          project_analysis=ProjectAnalysisConfig(),  # no project override at global level
      )
  ```
- [ ] Step 10: 實作 `load_project_config(home: Path, project_id: str) -> SecondSightConfig`
  - 先呼叫 `load_global_config()` 取得 base config（.env 在這裡載入）
  - 再讀 per-project TOML，overlay project_analysis（特別是 `model` 欄位）
  - 再套 env var overlay（`SECONDSIGHT_ANALYSIS_MODEL` 最高優先）
- [ ] Step 11: 跑所有 tests — 確認 pass
- [ ] Step 12: 寫 scar report

## Expected Scar Report Items

- **Scar-1**: `_load_dotenv_if_exists()` 的副作用是修改 `os.environ`（全 process 共享）。在 test 環境裡，每個 test 需要確保 `os.environ` cleanup（test fixture 用 `monkeypatch.setenv` 或 `monkeypatch.delenv`），否則 test 間互相污染。
- **Scar-2**: `_interpolate_vars` 的 regex 只匹配 `${UPPER_CASE}` pattern（`[A-Z_][A-Z0-9_]*`）。小寫 var name（`${my_var}`）不展開（留作 literal）。這是刻意限制：POSIX convention 是 env var 用大寫。若有 operator 用小寫 var name，會 silently 不展開而非 raise。應在 docstring 明確說明。
- **Scar-3**: `load_project_config()` 內部呼叫 `load_global_config()`，後者執行 `_load_dotenv_if_exists()`。若 operator 連續呼叫 `load_project_config()` 多次（例如 server 為不同 project 建立 runtime），`.env` 的 `load_dotenv()` 會被呼叫多次。`override=False` 保護 os.environ 不被重複覆蓋，但 `python-dotenv` 的 file IO 會重複發生。可接受（v1 不實作 cache），記錄在 scar。

## Acceptance Criteria

- Covers: "${VAR} 指向不存在的 env var → 明確 error 而非 silently 留 literal"
- Covers: "${VAR} 展開後是空字串"
- Covers: ".env 成功載入，pydantic-ai 可讀取 API key"
- Covers: "config.toml 中 ${VAR} 成功展開"
- Covers: "per-project model 覆蓋 global default"
- Covers: "env var SECONDSIGHT_ANALYSIS_MODEL 覆蓋所有 TOML 層"
- Covers: "global config.toml 不存在 → fallback 到 built-in defaults"
