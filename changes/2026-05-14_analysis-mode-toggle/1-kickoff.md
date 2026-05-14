# Kickoff: analysis-mode-toggle

## Problem Statement

SecondSight 的 analysis layer 在 system design (SD §2 / §5.7.3 / §8.5.1) 承諾**兩條 dispatch 路徑**：(a) SDK 模式（PydanticAI + 直接呼叫 LLM provider API）、(b) CLI 模式（spawn 使用者本機的 coding agent CLI，如 `claude` / `codex` / `opencode`，把 analysis prompts + project folder + traces 餵進去讓 coding agent 自行 loop）。使用者透過 `[general] mode = "cli" | "sdk"` 選擇路徑。

當前實作（GUR-102 → GUR-103 → 2026-05-13 server-wiring）只交付了 (a)，且 SD 設計中的 mode toggle 欄位 `[general] mode` 完全缺席，`[analysis] default_agent` 則被收斂成 SDK 模式下的 model alias selector，偏離 SD §5.7.3 原意。其副作用是：(1) 使用者無法選 CLI 模式；(2) SD §8.5.4 規定的啟動 pre-check 沒做，導致 SDK 模式在 `ANTHROPIC_API_KEY` 缺漏時於 session_end 觸發時才以 `RouterTerminalError` 爆炸；(3) `default_agent` 這個欄位的命名讓設計者自己都誤以為它是 mode toggle，是潛在的 dishonest-naming rot。

## Evidence

| 觀察 | 證據 | SD 對照 |
|---|---|---|
| 當前只有 SDK 一條路 | `src/secondsight/analysis/runtime.py:20-22,97,119-125` 全部走 `PydanticAIAnalysisAgent` → `LLMRouter`，無 subprocess fork 路徑 | SD §5.7.3 要求 CLI 模式可選 |
| `[general] mode` 不存在於 schema | `src/secondsight/config/schema.py` grep `general\|mode\|cli\|sdk` 無匹配；無 `GeneralConfig` dataclass | SD §8.5.1 line 1359 規定此欄位 |
| `default_agent` 語意偏離 SD | `config/schema.py:120-133` docstring 寫「Which agent type to use by default」，但實際只用於查 `[analysis.models]` table；無 dispatch 影響 | SD §5.7.3 規定 CLI 模式下決定生哪個 coding agent subprocess |
| GUR-103 明寫 CLI mode 延後 | `changes/2026-05-07_gur-103_phase2-analysis-agent-integration/1-kickoff.md:164-175`：「SDK mode only this issue. CLI mode is a follow-up.」 | — |
| Pre-check 缺席 | runtime 在第一次 LLM call 時才發現 missing key → `UserError`；無啟動期驗證 | SD §8.5.4 line 1452 規定 SDK mode 啟動需驗證 `[providers]` 至少一組可用 |
| Production error 證據 | `orphan_tool_use_start ... exc=Set the ANTHROPIC_API_KEY environment variable ...`（user-reported, 2026-05-14） | — |

## Risk of Inaction

- **PRD 承諾**：SD 把雙 mode 寫進設計版本 v1.3 (2026-04-25 revision)、ADR-004。不交付 CLI mode = 產品與設計脫鉤、competitive positioning（「使用者不用付額外 API 費用、直接借用 coding agent 已有的 subscription」）失效。
- **End user friction**：CLI 模式是 SD §2.2 表格中明寫的「一般使用者」入口。沒有 CLI 模式 = 強制 end user 申請 LLM API key、設定 provider → 把產品推回開發者向。
- **Silent rot 持續擴大**：`default_agent` 欄位每多被一個 caller 使用一次，未來重命名 / 重新對齊語意的 blast radius 就增大一格。
- **Production bug 未修**：ANTHROPIC_API_KEY 缺漏會讓任何 session_end 觸發的 analysis 全部失敗，且錯誤發生在 background，user 不一定看得到 → silent failure。

## Scope

### Locked Decisions（research → planning 契約 / v2 after config review 2026-05-14）

- **Decision A — bug fix 不拆**：ANTHROPIC_API_KEY pre-check 是 mode-conditional（SDK 模式才檢查，CLI 模式不檢查），因此邏輯本身依賴 mode toggle 存在。bug fix 與 toggle effort 必須同一個 plan、且 pre-check 從 day-1 就 mode-aware。Default mode=cli (Decision D) 後，全新安裝的 user 因 default 行為改變而**不再踩到此 bug**；只有顯式設定 `mode=sdk` 的 user 才會走 SDK 路徑、觸發 pre-check。短期 SDK 受害者必須等整個 toggle 落地才得救 —— 但 planning 階段應把「pre-check + key 注入」拆成最早可 ship 的 task（讓 toggle PR 內部仍可以分階段交付）。

- **Decision B — `default_agent` 重新設計 → B4（B2 refined）**：拆 `[analysis]` 為兩個並列子節 `[analysis.cli]` 與 `[analysis.sdk]`，外加 flat 層的共用設定。語意由 TOML 結構承載而非 docstring：
  - `[analysis]` flat — 兩 mode 共用設定（`timeout_seconds`）
  - `[analysis.cli]` — `default_agent` 決定派發給哪個 coding agent；mode=sdk 時整段被 loader 忽略
  - `[analysis.cli.models].{claude_code,codex,opencode}` — model 名透過 `--model` 參數傳給 coding agent CLI + 同時記為 metadata
  - `[analysis.sdk]` — `primary_model` 單一字串；mode=cli 時整段被 loader 忽略
  - `[analysis.sdk].fallback_model` — 單一字串（不是 list；Q-B 簡化決策）

- **Decision C — CLI mode 範圍**：Claude Code + Codex。OpenCode 在此 effort out of scope，但 schema 保留 `[analysis.cli.models].opencode` 欄位以利後續加入；遇 `default_agent = "opencode"` 應於 pre-check 階段以 actionable error 拒絕。

- **Decision D — `[general].mode` default = `"cli"`**：對齊 SD，且符合產品定位。Rationale（user-provided 2026-05-14）：Anthropic 已調整 Claude Agent SDK 的計費方式（見 https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan），CLI 模式借用 user 既有 Claude subscription 而非另計 API token 費，對 end user 友善；Codex 計費尚未變動。default cli 同時也讓「SDK 模式 ANTHROPIC_API_KEY 未設」這個當前 production bug 在 default 安裝下自然消失（user 升級無痛）。

- **Decision E — Config 機制 SD deviations（明寫）**：
  - **E1**：`${VAR}` 是唯一的 env 注入機制；empty 字串 = 真的沒設，不再 fall-through 到 `$ENV_VAR`。**Deviation from SD §8.5.3**（原寫「config > env > 不可用」改為「config（可含 `${VAR}`）> 不可用」）
  - **E2**：`[analysis]` 改為巢狀 cli/sdk 結構。**Deviation from SD §8.5.1**（原 flat）
  - **E3**：SDK fallback 從 list 收斂為單一字串。**Deviation from SD §5.7.4 / §8.5.1**（原 `fallback_models = [...]`）
  - **E4**：Retention built-in default 改 `raw_traces_ttl_days = 30` / `analysis_ttl_days = 60`。**Deviation from SD §3.10 / §8.5.1**（原 90/365）。Rationale：agent 進步速度 > 歷史資料價值衰減，長 retention 是 sunk asset
  - **E5（superseded by E7）**：原規劃「三個 `[analysis.cli.models]` 皆 empty 即 raise」已被取代。新規則：empty 表示「let coding agent use its own default model」，三個皆 empty 不是錯誤，是「不干涉」。SecondSight 不再代理 model 選擇權；source of truth 移交給 coding agent 自己的設定。
  - **E6**：Per-project config 層**整層 defer**（不只 mode override）。此 effort 期間只有 `~/.secondsight/config.toml` 一份 global config。Per-project 既有欄位（`[analysis].model` / `[retention]`）行為 frozen as-is，不新增、不重構。詳見 `TODO.md` 「Per-Project Config Override: Deferred」section（kill conditions 寫在那邊）。
  - **E7（新）**：`default_agent = "auto"` 解析語意 = 「resolves to agent selected at `secondsight init` time」。**Deviation from SD §5.7.3**（原寫「從 observation 推導」）。Rationale: (i) deterministic — config + init state 完全決定解析結果，不需要重播 observation；(ii) cold-start friendly — first-run 也能用；(iii) 少一條動態判斷路徑 = 少一個 silent failure 來源。Init state 儲存位置（`state.json` / DB / 環境）留待 planning lock。

### Final Config Schema (locked)

Final locked TOML lives in `changes/2026-05-14_analysis-mode-toggle/config.example.toml` (sibling file in this directory — kept as evidence artifact). Skeleton:

```toml
# ~/.secondsight/config.toml — single global config (E6: no per-project layer)

[general]
mode = "cli"                          # "cli" | "sdk"; default cli per Decision D
log_level = "info"

[providers.anthropic]
ANTHROPIC_API_KEY = ""                # empty = unset; "${VAR}" for explicit env injection (E1)
[providers.openai]
OPENAI_API_KEY = ""
[providers.custom]
API_KEY = ""
base_url = ""

[analysis]
timeout_seconds = 300                 # shared across both modes

[analysis.cli]                        # read only when mode == "cli"
default_agent = "auto"                # "auto" resolves to init-time agent selection (E7)

[analysis.cli.models]                 # empty = let coding agent use its own default model
claude_code = ""
codex = ""
opencode = ""                         # CLI dispatch out of scope (Decision C); schema slot preserved

[analysis.sdk]                        # read only when mode == "sdk"
primary_model = "claude-haiku-4-5-20251001"
fallback_model = "gpt-4o-mini"        # single fallback per Decision B/E3

[feedback]
convention_injection_budget = 2000
convention_top_n = 15

[retention]
raw_traces_ttl_days = 30              # E4: was 90 in SD
analysis_ttl_days = 60                # E4: was 365 in SD

[storage.sqlite]
cache_size_mb = 64

[server]
host = "127.0.0.1"
port = 8420
auto_start = true

[observation]
session_timeout_minutes = 30
```

### Validation Rules (Mode-Aware Pre-Check, per Decision A)

| Condition | Required check |
|---|---|
| Always | `[general].mode ∈ {"cli", "sdk"}`; `[analysis].timeout_seconds > 0` |
| `mode == "cli"` + `default_agent == "auto"` | Init-time agent selection state exists (planning to lock storage location) |
| `mode == "cli"` + `default_agent` 為具體 agent 名 | 該 CLI binary 在 PATH 中可執行 |
| `mode == "cli"` + `default_agent == "opencode"` | Reject — out of scope (Decision C) |
| `mode == "cli"` + `[analysis.cli.models].{x}` 非空 | 該 model 字串會以 `--model <x>` 傳給 coding agent CLI（passthrough; SecondSight 不驗證 model 名合法性） |
| `mode == "cli"` + `[analysis.cli.models]` 三個皆 empty | OK — coding agent 用自己的 default model（E5 superseded by E7） |
| `mode == "sdk"` | `[analysis.sdk].primary_model` 非空；≥ 1 `[providers.*]` 解析後（含 `${VAR}` 內插）有值 |

Pre-check 失敗於 `secondsight init` / server startup 階段以 actionable error 中止，不再讓 background analysis 於 session_end 時靜默失敗。

### Must-Have (with death conditions)

- **`[general] mode` config field（schema + loader + per-project override）** — Death: 若 GA 後 6 個月內 telemetry 顯示 ≥ 95% 安裝皆使用同一個 mode，移除欄位、把贏家寫死。
- **CLI mode dispatcher（spawn `claude` / `codex` subprocess with analysis prompts + project mount）— Claude Code + Codex only**（per Decision C）— Death: 若 PoC 階段證明任一 coding agent 的 headless 接口無法穩定回傳 structured output（schema mismatch 率 > 20%），該 adapter 降級為 experimental flag、不寫進 default config；若兩家皆失敗則重新評估整個 CLI mode。
- **Mode-conditional pre-check validation per SD §8.5.4**（per Decision A）—
  - `mode == "sdk"` → 檢查 `[providers]` 至少一組可用（含 env fallback per SD §8.5.3）+ `[analysis.models]` 對應 agent 有值
  - `mode == "cli"` → 只檢查 `[analysis].cli_agent`（或繼任欄位 per Decision B）對應的 coding agent CLI 在 PATH 中且可執行
  - Death: 若 config 系統未來改為 dynamic reload，pre-check 概念過時，改為 first-call lazy validation。
- **SDK mode key 注入路徑修復**：pydantic-ai 的 `AnthropicProvider` 從 `[providers.anthropic].ANTHROPIC_API_KEY` 或 env 取得 key，當前 implicit env 行為要明確化（per SD §8.5.3 優先順序：config > env > 不可用）。Death: 無。
- **`default_agent` 語意問題解決**（per Decision B；具體選擇延後到 planning）— Death: 一旦選定方案、文件對齊，此 must-have 視為 closed；若 telemetry 顯示新欄位仍被誤用（誤填 model 名 / 誤填 mode 值），rotate 為 hard-validated enum。

### Nice-to-Have

- `secondsight analyze --mode <cli|sdk>` CLI flag 一次性覆寫
- 每筆 analysis row 在 storage layer 多帶一個 `dispatched_via: cli|sdk` 欄位（給未來 telemetry 用）
- Config migration helper：偵測到舊 config（沒有 `[general] mode`）時補 default + 提示

### Explicitly Out of Scope

- 重新設計 SD §5.7.x — 只是補實作，不改設計
- 修改 PydanticAI agent loop 內部（tool 設計、prompt 結構）
- 重做 `[analysis.models.fallback]` LLM router — 已實作且與 mode 正交
- LLM Router 在 CLI 模式的行為（SD §5.7.4 明寫 CLI 不需 router）
- Observation / hook 機制
- Directive lifecycle / feedback injection
- **OpenCode CLI 模式實作**（per Decision C）— schema 保留 `opencode` 欄位以利後續加入，但 dispatcher / adapter / pre-check / E2E test 皆不涵蓋；遇到 `mode == "cli"` + `cli_agent == "opencode"` 應在 pre-check 階段以 actionable error 拒絕（"OpenCode CLI mode not supported in this release, set cli_agent to claude_code or codex"）

## North Star

```yaml
metric:
  name: "analysis_dispatch_success_rate"
  definition: "% of analyses (session_end + manual + sweeper-triggered) that produce a valid analysis row in intelligence.db within timeout, broken down by [general].mode"
  current: "unknown for CLI (path absent); SDK fails ≥ 1 known production case (ANTHROPIC_API_KEY)"
  target: "≥ 95% per mode under normal conditions; ≥ 99% for the actively-selected mode in a given install"
  invalidation_condition: "If user-perceived value comes from directive quality (not dispatch success), success rate is the wrong proxy; pivot north star to 'directive acceptance rate'"
  corruption_signature: "Auto-mode-derivation silently falls back to SDK when CLI fails — numbers look healthy but CLI path is never exercised in production"

sub_metrics:
  - name: "mode_selection_distribution"
    current: "100% SDK (no choice exists)"
    target: "non-degenerate distribution; at least one non-default config in field"
    proxy_confidence: medium
    decoupling_detection: "If ≥ 99% installs use default mode, the toggle is decorative — flag for removal review"

  - name: "preflight_failure_caught_ratio"
    current: "0% (pre-check absent)"
    target: "100% of missing-credential cases caught at startup, not at first dispatch"
    proxy_confidence: high
    decoupling_detection: "Pre-check passes but first dispatch fails for same reason → pre-check is faking it"
```

## Stakeholders

- **Decision maker:** user (project owner / designer)
- **Impacted teams:** SecondSight maintainer（雙 code path 維護成本）；end user（多一個 config 概念）
- **Damage recipients:**
  - Maintainer：dual dispatch path 永久維護、每個 analysis-related change 要 cover 兩條路徑、test matrix 翻倍
  - User onboarding：`[general] mode` 加 FAQ「我該選哪個？」
  - Phase 4 install-smoke / CI：必須測 CLI 模式 happy path（需要 mock 或 真的 spawn coding agent CLI）
  - Future debugger：dispatch 多一個分支 → root cause 排查路徑變多
  - 短期 SDK 用戶：在 toggle effort 完成前繼續看 cryptic ANTHROPIC_API_KEY error（建議把「修 SDK pre-check + key 注入」當 day-1 子任務先 ship，不要等整個 toggle 落地）
