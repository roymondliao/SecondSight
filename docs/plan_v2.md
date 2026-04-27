# SecondSight Task Breakdown v2

**版本**：2.0
**日期**：2026 年 4 月 27 日
**基於**：System Design v2.1 / PRD v1.3

---

## 概述

本文件基於 system_design.md v2.1 的完整設計決策，重新拆解 SecondSight 的開發任務。相較 plan v1.2 的主要變更：

| 變更項目 | v1.2 | v2.0 |
|---------|------|------|
| Analysis 架構 | Action classification + Span analysis + Pattern detection | 兩層架構：Session-level 行為分析 + Cross-session 彙整 |
| Feedback 機制 | Convention + Hint（PreToolUse pattern match） | Convention-first（system prompt 注入），Hint reserved |
| Hook 架構 | 未定義具體實作 | Thin HTTP client + API Server core component |
| Framework Profile | 獨立設定檔 | 移除，動態推導 |
| Directive 採納 | review_required / auto 兩種模式 | 全 auto，user 從 dashboard 刪除 |
| Durable Queue | Claim-confirm pattern | 移除（ADR-001），filesystem-first |

### Phase 結構

| Phase | 名稱 | 目的 |
|-------|------|------|
| Phase 0 | Exploration & Spike | 驗證技術可行性，建立 project scaffold |
| Phase 1 | Observation Layer | Hook → Normalize → Persist pipeline |
| Phase 2 | Analysis Layer | 兩層行為分析 + Convention 產出 |
| Phase 3A | Feedback & Dashboard MVP | Convention 注入 + Dashboard 可視化 |
| Phase 3B | Lifecycle & Governance | Directive lifecycle + 進階控制 |

**處理順序**：Phase 0 → Phase 1 → Phase 2 → Phase 3A → Phase 3B

---

## Phase 0：Exploration & Spike

Phase 0 的重點是技術可行性驗證和 project 基礎建設。

### 0.1 Agent Hook 調查

| Task ID | Task | 目的 | 產出 | Ref |
|---------|------|------|------|-----|
| P0-1 | **Claude Code hook 機制調查** | 了解 hook lifecycle、payload 格式、session context | 技術筆記：可用 hooks、event 格式、session ID 取得方式 | SD 3.7.4, 3.8 |
| P0-2 | **Codex hook 機制調查** | 了解 Codex 的 hook 機制、sub-agent、task mode | 技術筆記：可用 hooks、event 格式、限制 | SD 4.3 Phase 0 調查項 |
| P0-3 | **OpenCode hook 機制調查** | 了解 OpenCode 的 hook 機制 | 技術筆記：同上 | SD 4.3 Phase 0 調查項 |
| P0-4 | **Claude Code hook subprocess 生命週期調查** | 確認 hook script 回傳 output 後是否可繼續 background work | 可行性報告 | SD 十三 Phase 0 調查項 |

### 0.2 Injection 可行性驗證

| Task ID | Task | 目的 | 產出 | Ref |
|---------|------|------|------|-----|
| P0-5 | **SessionStart convention injection test** | 驗證透過 SessionStart hook 注入 convention 到 system prompt 的可行性 | 可行性報告：inject 方式、output 格式、agent 理解度 | SD 6.3 |
| P0-6 | **Convention comprehension experiment** | 測試 agent 能否理解並遵守注入的行為準則 | 實驗報告 + convention phrasing 建議 | SD 6.3 |

### 0.3 Architecture Spike

| Task ID | Task | 目的 | 產出 | Ref |
|---------|------|------|------|-----|
| P0-7 | **Filesystem + SQLite 雙層儲存 spike** | 驗證 filesystem-first + SQLite WAL mode 的寫入效能與 concurrent access | Prototype + 效能報告 | SD 3.1, 3.4, 3.5 |
| P0-8 | **API Server + thin hook client spike** | 驗證 bash+curl thin client → FastAPI server 的 latency（目標 < 10ms） | Prototype + latency 測量 | SD 3.9, 3.9.1 |
| P0-9 | **Session identity linking spike** | 驗證從 hook payload 取得 session context 的方式 | Identity model 設計 | SD 3.8 |

### 0.4 Project Scaffold

| Task ID | Task | 目的 | 產出 | Ref |
|---------|------|------|------|-----|
| P0-10 | **Project 初始化：repo structure + tooling** | 建立 `src/secondsight/` 目錄結構、pyproject.toml、dev tooling（ruff、pytest、pre-commit） | 可 build 的空 project | SD 2.3 codebase structure |
| P0-11 | **Config module 實作** | config.toml loading、per-project override、priority 邏輯（config > env > default） | `config/` module | SD 8.5 |
| P0-12 | **Schemas module 實作** | Event types（11 種）、BehaviorFlagType enum + FLAG_DEFINITIONS、AnalysisResult、Directive Pydantic models | `schemas/` module | SD 3.7, 5.5.1, 7.3, 7.4 |

### Exit Criteria

- [ ] Claude Code hook 機制調查完成，知道 payload 格式和 session context
- [ ] Codex / OpenCode hook 機制調查完成（或確認不可用的降級策略）
- [ ] Convention injection 可行性已驗證
- [ ] Filesystem + SQLite 雙層儲存效能已驗證
- [ ] API Server thin client latency < 10ms 已驗證
- [ ] Project scaffold 可 build，config / schemas module 已實作

### 預估時間

2-3 週

---

## Phase 1：Observation Layer

建立 Hook → Normalize → Persist 的完整 observation pipeline。

### 1.1 Storage Layer

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P1-1 | **Raw Trace Store 實作** | P0-7 | Filesystem-based trace storage（`{timestamp}_{event_type}.json`） | SD 3.1, 7.2 |
| P1-2 | **SQLite DB engine + PRAGMA 配置** | P0-7, P0-11 | DB engine factory、WAL mode 硬編碼 + cache_size 可配置 | SD 3.5 |
| P1-3 | **Events table 實作** | P1-2, P0-12 | SQLAlchemy Core table definition + repository（INSERT, query by session/segment） | SD 3.7.5 |
| P1-4 | **Observation pipeline 實作** | P1-1, P1-3 | `observation/pipeline.py`：async filesystem write + DB INSERT，失敗記錄 sync log | SD 3.9 |

### 1.2 API Server Core

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P1-5 | **FastAPI server scaffold** | P0-8 | `api/server.py`：startup/shutdown lifecycle、daemon mode（--daemon / --stop） | SD 8.3 |
| P1-6 | **Hook endpoints 實作** | P1-4, P1-5 | `api/hooks.py`：POST /hook/{type}，normalize → session tracker → async ingest → return OK | SD 3.9 |
| P1-7 | **Session tracker 實作** | P0-12 | `observation/tracker.py`：in-memory session state（segment_index increment、sub-agent nesting stack） | SD 3.7.5, 3.9 |
| P1-8 | **Fallback 機制實作** | P1-6 | Hook script with curl --connect-timeout fallback → write to fallback_events.jsonl | SD 3.9.2 |

### 1.3 Agent Adapters

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P1-9 | **Adapter interface 定義** | P0-12 | `adapters/base.py`：abstract normalize + inject_convention + inject_hint（reserved） + supported_event_types | SD 4.2 |
| P1-10 | **Claude Code adapter 實作** | P0-1, P1-9 | `adapters/claude_code.py`：Claude Code hook payload → SecondSight Event 轉換 | SD 3.7.4, 4.3 |
| P1-11 | **Hook script 安裝機制** | P1-10, P1-8 | `secondsight init` 自動安裝 hook scripts 到 `~/.claude/hooks/` | SD 9.2 |

### 1.4 CLI 基礎

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P1-12 | **CLI scaffold（Typer）** | P0-11 | `cli/` module：init、serve、status、sync subcommands | SD 9.1, 9.2 |
| P1-13 | **Backfill 機制實作** | P1-4, P1-3 | `secondsight sync`：從 filesystem 補同步未入庫的 events + fallback_events.jsonl backfill | SD 3.9.2 Error Handling |

### 1.5 Integration Test

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P1-14 | **End-to-end observation test** | P1-6, P1-10, P1-11 | 模擬 Claude Code hook 觸發 → server 收到 → filesystem + DB 寫入成功 | SD 3.9 全流程 |

### Exit Criteria

- [ ] Hook 觸發 → API server 收到 → raw trace 落地 + DB INSERT 完整流程可運作
- [ ] Hook latency < 10ms（測量 bash+curl → server → response）
- [ ] Server 未啟動時 fallback 可運作（事件寫入 fallback file）
- [ ] Backfill 機制可從 fallback file + filesystem 補同步 DB
- [ ] Session tracker 正確維護 segment_index 和 sub-agent nesting
- [ ] `secondsight init` 可安裝 hook scripts
- [ ] `secondsight serve --daemon` / `--stop` / `status` 可運作

### 預估時間

2-3 週

---

## Phase 2：Analysis Layer

建立兩層行為分析系統 + convention 產出。

### 2.1 Analysis Core

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P2-1 | **Behavior flags table 實作** | P1-3, P0-12 | SQLAlchemy table + repository（INSERT, query by session/project/flag_type） | SD 7.3 |
| P2-2 | **Directives table 實作** | P1-3, P0-12 | SQLAlchemy table + repository（INSERT, query active conventions, lifecycle update） | SD 7.4 |
| P2-3 | **Segmenter 實作** | P1-3 | `analysis/segmenter.py`：從 DB 按 segment_index 切分 events，pairing tool_use_start/end | SD 5.3.1, 5.3.3 |
| P2-4 | **Supplementary metrics 計算** | P2-3 | `analysis/metrics.py`：per-segment total_tokens、unique_files、duration、error_count | SD 5.3.1 分析流程 Step 2 |

### 2.2 Analysis Prompts

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P2-5 | **Segment-level analysis prompt 實作** | P0-12, P2-3 | `analysis/prompts/behavior.py`：動態生成 prompt（schema 說明 + FLAG_DEFINITIONS 注入 + segment data + output format） | SD 5.5.2 |
| P2-6 | **Cross-session aggregation prompt 實作** | P0-12, P2-1 | `analysis/prompts/aggregate.py`：per flag_type 語意歸類 + convention 產出 prompt | SD 5.5.3 |
| P2-7 | **Session report summary prompt 實作** | P2-5 | `analysis/prompts/summary.py`：session 行為報告摘要（給 dashboard 用） | SD 5.3.1 |

### 2.3 Analysis Orchestration

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P2-8 | **Analysis orchestrator 實作** | P2-4, P2-5, P2-6, P2-7 | `analysis/orchestrator.py`：session 分析全流程（backfill → segmenter → per-segment LLM → session report → cross-session 彙整） | SD 5.6 |
| P2-9 | **Behavior flag detector 實作** | P2-5 | `analysis/behavior.py`：呼叫 LLM 執行 segment-level prompt，解析回傳的 flags，寫入 DB | SD 5.5.2 |
| P2-10 | **Cross-session aggregator 實作** | P2-6, P2-1 | `analysis/aggregator.py`：Step 1 自動分組 → Step 2 per flag_type LLM 歸類 → Step 3 合併取 top N → 寫入 directives table | SD 5.5.3 |

### 2.4 Analysis Agent Integration

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P2-11 | **Analysis Agent tools 實作** | P2-8, P1-1, P1-3 | `AnalysisTools`：read_traces、read_project_file、query_structured_store、read_historical_flags | SD 5.4 |
| P2-12 | **PydanticAI agent scaffold（SDK 模式）** | P2-11, P2-8 | `sdk/` module：PydanticAI-based agent loop + tools binding | SD 5.2 |
| P2-13 | **LLM Router 實作（SDK 模式）** | P2-12 | Primary model → fallback models routing via LiteLLM | SD 5.7.4 |

### 2.5 Analysis Model 設定

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P2-14 | **Analysis model 選擇邏輯** | P0-11, P2-8 | 從 config + observation 記錄推導 agent_type → 選對應 model | SD 5.7.1, 5.7.3 |
| P2-15 | **Analysis 觸發機制** | P2-8, P1-5 | Session end event → 自動觸發 background analysis；timeout-based fallback；`secondsight analyze` 手動觸發 | SD 5.6 |

### 2.6 CLI & API

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P2-16 | **CLI analyze subcommand** | P2-8, P1-12 | `secondsight analyze [--session ID]`：手動觸發 analysis | SD 9.2 |
| P2-17 | **CLI directive subcommand** | P2-2, P1-12 | `secondsight directive --active --format json`：查詢 active conventions | SD 9.2, 9.3 |
| P2-18 | **Analysis API endpoints** | P2-8, P1-5 | GET /api/analysis/summary, /sessions, /sessions/{id}, /sessions/{id}/flags, /trends, /aggregation | SD 10.4 |
| P2-19 | **Directives API endpoints** | P2-2, P1-5 | GET/PATCH /api/directives | SD 10.4 |

### Exit Criteria

- [ ] Session 結束後自動觸發 analysis（background，不阻塞使用者）
- [ ] Segment-level 行為分析可產出 behavior flags（6 種 flag types）
- [ ] Cross-session 彙整可歸類行為模式 + 產出 conventions
- [ ] Convention 寫入 directives table，可透過 CLI / API 查詢
- [ ] Session report 可透過 API 查詢（dashboard 用）
- [ ] SDK 模式 agent loop 可運作（PydanticAI + LLM Router）

### 預估時間

3-4 週

---

## Phase 3A：Feedback & Dashboard MVP

建立 convention 自動注入 + dashboard 可視化。

### 3A.1 Convention Injection

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P3A-1 | **Convention selection + budget enforcement** | P2-2 | `feedback/convention.py`：query active conventions、按頻率排序、token budget 截斷（≤ 2000 tokens） | SD 5.8.3, 6.3 |
| P3A-2 | **Convention injection via adapter** | P3A-1, P1-9, P1-10 | SessionStart hook → adapter.inject_convention() → system prompt 注入 | SD 6.3, 4.2 |
| P3A-3 | **SessionStart hook endpoint** | P3A-2, P1-6 | POST /hook/session-start → query conventions → format → return for injection | SD 3.9, 6.3 |

### 3A.2 Directive Lifecycle（基礎）

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P3A-4 | **Lifecycle state machine** | P2-2 | `feedback/lifecycle.py`：active → obsolete → re-activated、superseded、expired 狀態轉換 | SD 5.9.1 |
| P3A-5 | **Effectiveness tracking** | P3A-4, P2-10 | 每次彙整時計算 convention 對應 flag 的頻率變化 → 判斷 effective / ineffective | SD 5.9.4 |
| P3A-6 | **Convention 刪除（dashboard 用）** | P3A-4 | PATCH /api/directives/{id} status=disabled | SD 6.3, 10.4 |

### 3A.3 Dashboard Frontend

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P3A-7 | **Frontend scaffold（React + Vite + shadcn/ui）** | P1-5 | 基礎 layout、routing、TanStack Query 設定 | SD 8.2 |
| P3A-8 | **Observation views 實作** | P3A-7, P1-6 | Session list → Segment list → Event timeline（hierarchical drill-down） | SD 10.2 |
| P3A-9 | **Analysis views 實作** | P3A-7, P2-18 | Project summary、session analysis list、per-segment behavior flags、flag 趨勢圖 | SD 10.3 |
| P3A-10 | **Directive management view** | P3A-7, P2-19 | Active conventions 列表、lifecycle 狀態、source flag 溯源、刪除操作 | SD 10.3 |

### 3A.4 Data Retention

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P3A-11 | **TTL cleanup 實作** | P1-1, P1-3 | raw_traces_ttl_days、analysis_ttl_days 配置 → analysis 完成後觸發清理 | SD 3.10 |
| P3A-12 | **CLI cleanup subcommand** | P3A-11, P1-12 | `secondsight cleanup [--dry-run]`：手動觸發 retention cleanup | SD 9.2 |

### 3A.5 Observation API

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P3A-13 | **Observation API endpoints** | P1-3, P1-5 | GET /api/sessions, /sessions/{id}, /sessions/{id}/segments, /sessions/{id}/segments/{idx} | SD 10.4 |

### Exit Criteria

- [ ] SessionStart 時 conventions 自動注入到 agent 的 system prompt
- [ ] Convention token budget 控制可運作（≤ 2000 tokens）
- [ ] Dashboard 可瀏覽 observation（session → segment → event drill-down）
- [ ] Dashboard 可瀏覽 analysis（behavior flags、趨勢、convention list）
- [ ] User 可從 dashboard 刪除不適合的 convention
- [ ] Data retention TTL cleanup 可運作
- [ ] Agent 行為出現可觀察的改變（convention 生效）

### 預估時間

3-4 週

---

## Phase 3B：Lifecycle & Governance

建立完整的 directive lifecycle 管理、進階控制與 hint 機制預留。

### 3B.1 Advanced Lifecycle

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P3B-1 | **Convention 語意去重** | P3A-4, P2-10 | 新增前比對 active conventions：語意重複 → 不新增、語意相似更精確 → supersede、全新 → 新增 | SD 5.9.2 |
| P3B-2 | **Expiry enforcement** | P3A-4 | 自動偵測超過 TTL 的 conventions → expired → 重新評估 | SD 5.9.3 |
| P3B-3 | **Re-activation 機制** | P3A-4 | Obsolete convention 對應的 flag 頻率回升 → 自動 re-activate | SD 5.9.3 |

### 3B.2 Hint 機制預留（介面實作）

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P3B-4 | **Hint module 介面定義** | P1-9, P0-12 | `feedback/hint.py`：空實作 class，保留 match / inject 介面 | SD 6.4 |
| P3B-5 | **Adapter inject_hint 空實作** | P3B-4, P1-10 | Claude Code adapter 的 inject_hint 方法（pass through） | SD 4.2 |

### 3B.3 Error Handling 完善

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P3B-6 | **Analysis failure handling** | P2-8 | LLM call 失敗 → 標記 session analysis 為 failed → 可手動 re-analyze | SD 十一 |
| P3B-7 | **DB rebuild from filesystem** | P1-1, P1-3 | `secondsight sync --rebuild`：從 filesystem 全量重建 DB | SD 十一 |

### 3B.4 跨 Agent 擴展

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P3B-8 | **Codex adapter 實作** | P0-2, P1-9 | `adapters/codex.py`（基於 Phase 0 調查結果） | SD 4.3 |
| P3B-9 | **OpenCode adapter 實作** | P0-3, P1-9 | `adapters/opencode.py`（基於 Phase 0 調查結果） | SD 4.3 |

### 3B.5 進階 Dashboard

| Task ID | Task | 依賴 | 產出 | Ref |
|---------|------|------|------|-----|
| P3B-10 | **Convention effectiveness 可視化** | P3A-5, P3A-9 | Convention 注入前後的 flag 頻率對比圖 | SD 5.9.4, 10.3 |
| P3B-11 | **Cross-session aggregation 可視化** | P3A-9, P2-18 | Flag type 分佈圓餅圖、行為模式歸類展示 | SD 10.3 |

### Exit Criteria

- [ ] Convention 語意去重可運作（不產出重複 conventions）
- [ ] Convention expiry + re-activation 可運作
- [ ] Hint 機制介面已定義（空實作），日後可直接補上邏輯
- [ ] Analysis failure graceful handling 可運作
- [ ] DB 可從 filesystem 全量重建
- [ ] 至少一個額外 agent adapter 可運作（Codex 或 OpenCode）

### 預估時間

2-3 週

---

## 依賴關係總覽

```
Phase 0 ──────────────────────────────────────────────────────────────
   P0-1,2,3,4 (agent hook 調查)
      │
      ├─→ P0-5,6 (injection 可行性)
      │
      ├─→ P0-7 (storage spike) ─→ P0-8 (API server spike)
      │
      └─→ P0-9 (session identity spike)

   P0-10 (project scaffold)
      │
      ├─→ P0-11 (config module)
      └─→ P0-12 (schemas module)

Phase 1 ──────────────────────────────────────────────────────────────
   P1-1 (raw store) ──┐
   P1-2 (DB engine) ──┤
   P1-3 (events table)┘─→ P1-4 (pipeline) ─→ P1-6 (hook endpoints)
                                                     │
   P1-5 (server scaffold) ─────────────────────────┘
   P1-7 (session tracker) ─→ P1-6
   P1-8 (fallback) ─→ P1-6
   P1-9 (adapter interface) ─→ P1-10 (claude code adapter)
   P1-10 ─→ P1-11 (hook install)
   P1-12 (CLI scaffold) ─→ P1-13 (backfill)
   P1-14 (e2e test)

Phase 2 ──────────────────────────────────────────────────────────────
   P2-1 (behavior flags table) ──┐
   P2-2 (directives table) ─────┤
   P2-3 (segmenter) ────────────┤
   P2-4 (metrics) ──────────────┘─→ P2-5,6,7 (prompts)
                                         │
                                    P2-8 (orchestrator)
                                    P2-9 (behavior detector)
                                    P2-10 (aggregator)
                                         │
                                    P2-11 (tools) ─→ P2-12 (SDK agent)
                                                        ─→ P2-13 (LLM router)
                                    P2-14 (model selection)
                                    P2-15 (auto trigger)
                                    P2-16~19 (CLI + API)

Phase 3A ─────────────────────────────────────────────────────────────
   P3A-1 (convention selection) ─→ P3A-2 (injection) ─→ P3A-3 (endpoint)
   P3A-4 (lifecycle) ─→ P3A-5 (effectiveness) ─→ P3A-6 (delete)
   P3A-7 (frontend scaffold) ─→ P3A-8,9,10 (views)
   P3A-11 (TTL cleanup) ─→ P3A-12 (CLI cleanup)
   P3A-13 (observation API)

Phase 3B ─────────────────────────────────────────────────────────────
   P3B-1 (dedup) ─→ P3B-2 (expiry) ─→ P3B-3 (re-activation)
   P3B-4,5 (hint interface)
   P3B-6,7 (error handling)
   P3B-8,9 (adapters)
   P3B-10,11 (dashboard advanced)
```

---

## 成功指標

### Instrumentation Readiness 指標（必達）

| Phase | 指標 | 目標 |
|-------|------|------|
| Phase 0 | Hook 調查完成度 | Claude Code 完整調查，Codex/OpenCode 可行性已知 |
| Phase 0 | Convention injection 可行性 | 已驗證，有初步 comprehension baseline |
| Phase 1 | Event capture 完整度 | Claude Code 100% event capture |
| Phase 1 | Hook latency | < 10ms (bash+curl → server → response) |
| Phase 2 | Behavior flag detection | 6 種 flag types 可運作 |
| Phase 2 | Convention generation | Cross-session 彙整可產出 conventions |
| Phase 3A | Convention injection | SessionStart auto inject 可運作 |
| Phase 3A | Dashboard | Observation + Analysis views 可瀏覽 |

### Target Threshold 指標（Baseline → 迭代）

| Phase | 指標 | Early Stage 目標 | 成熟期目標 |
|-------|------|------------------|------------|
| Phase 1 | Event write latency（async） | < 50ms | < 20ms |
| Phase 2 | Behavior flag 準確率 | Baseline established | > 80% |
| Phase 2 | Flag 誤標率 | Baseline established | < 10% |
| Phase 3A | Agent 行為改變率 | Baseline established | > 30% |
| Phase 3A | Convention injection budget utilization | < 2000 tokens | 穩定在 budget 內 |
| Phase 3B | Convention 語意去重準確率 | Baseline established | > 90% |

---

## 時間估計總覽

| Phase | Tasks 數量 | 預估時間 | 累計時間 | Milestone |
|-------|-----------|----------|----------|-----------|
| Phase 0 | 12 | 2-3 週 | 2-3 週 | 技術可行性驗證 + project scaffold |
| Phase 1 | 14 | 2-3 週 | 4-6 週 | Observation pipeline 可運作 |
| Phase 2 | 19 | 3-4 週 | 7-10 週 | 兩層分析 + convention 產出 |
| Phase 3A | 13 | 3-4 週 | 10-14 週 | **Convention injection + Dashboard MVP** |
| Phase 3B | 11 | 2-3 週 | 12-17 週 | Lifecycle 完善 + 跨 Agent 擴展 |
| **總計** | **69** | - | **12-17 週** | - |

### Milestone 說明

- **Phase 1 完成**：能完整記錄 Claude Code 的 session 事件
- **Phase 2 完成**：能分析 session 行為、產出 conventions
- **Phase 3A 完成**：系統可運作，convention 自動注入 + dashboard 可視化
- **Phase 3B 完成**：系統可治理，lifecycle 完善、hint 介面預留、多 agent 支援

---

## 風險與注意事項

### Phase 0 風險

| 風險 | 影響 | 緩解策略 |
|------|------|----------|
| Claude Code hook payload 不含 session context | 無法做 session identity linking | P0-4 調查 subprocess 生命週期，考慮 process-based 推導（SD 3.8） |
| Codex / OpenCode 無 hook 機制 | 無法支援這些 agent | Phase 1 先專注 Claude Code，其他漸進支援（SD 4.3） |
| Convention injection 不被 agent 理解 | Feedback 失效 | P0-6 測試多種 phrasing，找到 comprehension baseline |

### Phase 1 風險

| 風險 | 影響 | 緩解策略 |
|------|------|----------|
| Hook latency 超過 10ms | 影響 agent 使用體感 | 已設計 thin client 架構，server 端全 async（SD 3.9.1） |
| DB concurrent write contention | 資料遺失 | WAL mode + busy_timeout（SD 3.5） |
| Server crash 導致事件遺失 | Observation 不完整 | Fallback 機制：寫入 fallback_events.jsonl，後續 backfill（SD 3.9.2） |

### Phase 2 風險

| 風險 | 影響 | 緩解策略 |
|------|------|----------|
| Behavior flag 誤標率過高 | Convention 品質下降 | Flag prompt 設計「不確定時不標記」（SD 5.5.2）；cross-session 統計過濾低頻 flags |
| LLM 不遵守 output format | Parse 失敗 | JSON output format + validation；失敗時 retry 或 skip |
| Cross-session 語意歸類不準 | Convention 不精確 | 先依 flag_type 分組降低歸類難度（SD 5.5.3） |

### Phase 3A 風險

| 風險 | 影響 | 緩解策略 |
|------|------|----------|
| Convention 注入導致 agent regression | 任務品質下降 | Effectiveness tracking + user 可從 dashboard 刪除（SD 5.9.4, 6.3） |
| Convention token budget 不夠 | 重要 conventions 被截斷 | 按頻率排序，優先注入最重要的（SD 5.8.3） |

---

## Ref 說明

本文件 task 的 Ref 欄位指向 `system_design.md` v2.1 的對應章節：

| 縮寫 | 對應 |
|------|------|
| SD 2.3 | system_design.md §二、2.3 架構原則：Library-First |
| SD 3.1 | system_design.md §三、3.1 設計原則：Filesystem-First |
| SD 3.5 | system_design.md §三、3.5 SQLite 配置 |
| SD 3.7 | system_design.md §三、3.7 Event Model |
| SD 3.8 | system_design.md §三、3.8 Session 識別策略 |
| SD 3.9 | system_design.md §三、3.9 Observation Data Flow Pipeline |
| SD 3.10 | system_design.md §三、3.10 Data Retention & Cleanup |
| SD 4.2 | system_design.md §四、4.2 Adapter Interface |
| SD 4.3 | system_design.md §四、4.3 支援範圍與跨 Agent 差異 |
| SD 5.2 | system_design.md §五、5.2 Agent Framework：PydanticAI |
| SD 5.3 | system_design.md §五、5.3 兩層分析架構 |
| SD 5.4 | system_design.md §五、5.4 Analysis Agent Tools |
| SD 5.5 | system_design.md §五、5.5 Analysis Prompt Architecture |
| SD 5.6 | system_design.md §五、5.6 Analysis 執行時機 |
| SD 5.7 | system_design.md §五、5.7 Analysis Model 設定 |
| SD 5.8 | system_design.md §五、5.8 分析結果大小限制 |
| SD 5.9 | system_design.md §五、5.9 Directive Lifecycle |
| SD 6.3 | system_design.md §六、6.3 Convention 的注入方式 |
| SD 6.4 | system_design.md §六、6.4 Hint 機制（Reserved） |
| SD 7.2 | system_design.md §七、7.2 目錄結構 |
| SD 7.3 | system_design.md §七、7.3 Behavior Flags 在 DB 中的結構 |
| SD 7.4 | system_design.md §七、7.4 Directive 在 DB 中的結構 |
| SD 8.2 | system_design.md §八、8.2 Frontend Stack |
| SD 8.3 | system_design.md §八、8.3 Server 部署方式 |
| SD 8.5 | system_design.md §八、8.5 Config 結構 |
| SD 9.1-9.3 | system_design.md §九、CLI 設計 |
| SD 10.2-10.4 | system_design.md §十、API Design & Frontend Views |
| SD 十一 | system_design.md §十一、Error Handling 策略 |
| SD 十三 | system_design.md §十三、待討論項目 |

---

## 版本歷史

| 版本 | 日期 | 變更 |
|------|------|------|
| 2.0 | 2026-04-27 | 基於 system_design.md v2.1 完整重寫。Phase 結構維持 0/1/2/3A/3B，但 task 內容全面更新：移除 durable queue / framework profile / action classification / span analysis / pattern detection，改為兩層行為分析架構（behavior flags + cross-session aggregation）；feedback 改為 convention-first + hint reserved；新增 API server core component / thin hook client / dashboard frontend tasks；所有 task 加上 system_design.md reference。Task 總數從 99 精簡為 69。 |
