# SecondSight Task Breakdown

**版本**：1.2
**日期**：2026 年 4 月 23 日
**基於**：PRD v1.3
**變更**：Phase 3 拆分為 3A/3B、Failure Attribution 改為 hypothesis-based、補 Evaluation Governance、補 Fallback Design、成功指標分層

---

## 概述

本文件將 SecondSight 的開發工作拆解為五個階段：

| Phase | 名稱 | 目的 |
|-------|------|------|
| Phase 0 | Exploration & Risk Validation | 驗證核心產品假設與技術可行性 |
| Phase 1 | Observation Layer | 建立 multi-agent execution 記錄系統 |
| Phase 2 | Analysis Layer | 建立 execution diagnosis 系統 |
| Phase 3A | Feedback Layer MVP | 建立可運作的 directive 生成與採納系統 |
| Phase 3B | Bounded Autonomy & Governance | 建立安全治理、lifecycle 管理與進階控制 |

**處理順序**：Phase 0 → Phase 1 → Phase 2 → Phase 3A → Phase 3B

**整合範圍**：Claude Code、OpenCode、Codex

---

## Phase 0：Exploration & Risk Validation

Phase 0 的目的不只是技術可行性驗證，更是**產品核心假設的風險探測**。

### 0.1 Instrumentation Feasibility（觀測可行性）

| Task ID | Task | 目的 | 產出 |
|---------|------|------|------|
| P0-1 | **Claude Code hook 機制調查** | 了解 Claude Code 如何暴露 tool calls、session events、token usage | 技術筆記：可用 hooks、event 格式、限制 |
| P0-2 | **OpenCode hook 機制調查** | 了解 OpenCode 的 event 暴露方式 | 技術筆記：可用 hooks、event 格式、限制 |
| P0-3 | **Codex CLI hook 機制調查** | 了解 Codex 的 event 暴露方式 | 技術筆記：可用 hooks、event 格式、限制 |
| P0-4 | **Token estimation 可行性測試** | 測試是否能從各 agent 取得或估算 token 消耗 | 可行性報告 + 估算方法 |

### 0.2 Directive Feasibility（回饋可行性）

| Task ID | Task | 目的 | 產出 |
|---------|------|------|------|
| P0-5 | **Runtime injection feasibility test** | 驗證三種 agent 是否支援 runtime feedback 注入 | 可行性報告：inject 方式、粒度、latency |
| P0-6 | **Directive comprehension experiment** | 測試 agent 能否理解並穩定遵守 directive 格式 | 實驗報告 + directive phrasing 建議 |
| P0-7 | **Session-start injection test** | 驗證 session 開始時注入 directive 的機制 | 可行性報告 + 注入方式 |
| P0-8 | **Runtime feedback fallback design** | 定義當 runtime injection 不可行時的替代路徑與產品降級策略 | Fallback 設計文件 + 產品定位調整建議 |

### 0.3 Framework Philosophy Feasibility（設計脈絡可行性）

| Task ID | Task | 目的 | 產出 |
|---------|------|------|------|
| P0-9 | **Framework philosophy acquisition spike** | 調查 philosophy 從哪裡取得：prompt、hooks、config、README、人工輸入 | 來源清單 + 可行性評估 |
| P0-10 | **Framework artifact sources survey** | 列舉各種 framework 可能提供的 artifacts | Artifact 類型清單 + 抽取難度評估 |
| P0-11 | **Minimal philosophy input experiment** | 測試只提供最小必要欄位時，analysis 品質是否可接受 | 實驗報告 + 最小欄位建議 |

### 0.4 Architecture Feasibility（架構可行性）

| Task ID | Task | 目的 | 產出 |
|---------|------|------|------|
| P0-12 | **Unified Event Schema 草稿** | 基於 P0-1~3 的調查，設計跨 agent 的統一 event schema | Event Schema v0.1 |
| P0-13 | **Storage architecture spike** | 驗證 filesystem + SQLite 雙層儲存的可行性與效能 | 架構驗證報告 + prototype |
| P0-14 | **Claim-confirm queue prototype** | 驗證 durable queue pattern 的實作方式 | Queue prototype |
| P0-15 | **Session identity linking design** | 設計 session 如何關聯到 agent / framework / project / directive lineage | Identity model 設計文件 |

### Exit Criteria

- [ ] 三種 agent 的 hook 機制已調查清楚，知道能拿到什麼、拿不到什麼
- [ ] Runtime injection 可行性已驗證，知道能注入什麼、不能注入什麼
- [ ] **Fallback path 已設計**：若 runtime injection 不可行，產品如何降級仍然成立
- [ ] Agent 對 directive 的理解能力已測試，有初步 comprehension baseline
- [ ] Framework philosophy 的來源已調查，知道從哪裡取得、哪些需要人工輸入
- [ ] Unified Event Schema v0.1 已定義
- [ ] 雙層儲存架構已驗證可行
- [ ] Session identity linking 設計已完成

### 預估時間

2-3 週

---

## Phase 1：Observation Layer

建立 multi-agent execution 記錄系統。

### 1.1 Core Schemas

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P1-1 | **Unified Event Schema 定稿** | P0-12 | Event Schema v1.0 (JSON Schema) |
| P1-2 | **Session Schema 設計** | P1-1, P0-15 | Session metadata schema（含 identity linking） |
| P1-3 | **Framework Context Schema 設計** | P0-9, P0-11 | Framework profile schema (最小必要欄位 + 擴展欄位) |

### 1.2 Storage Layer

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P1-4 | **Raw Trace Store 實作** | P0-13 | Filesystem-based trace storage |
| P1-5 | **Structured Intelligence Schema + Persistence** | P0-13 | SQLite schema + basic CRUD |
| P1-6 | **Durable Queue 實作** | P0-14 | Claim-confirm work queue |
| P1-7 | **Event Writer Service** | P1-4, P1-5, P1-6 | 統一寫入介面，處理 queue → store |

### 1.3 Agent Adapters

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P1-8 | **Adapter Interface 定義** | P1-1 | Abstract adapter contract |
| P1-9 | **Claude Code Adapter** | P0-1, P1-8 | Claude Code → Unified Event 轉換 |
| P1-10 | **OpenCode Adapter** | P0-2, P1-8 | OpenCode → Unified Event 轉換 |
| P1-11 | **Codex Adapter** | P0-3, P1-8 | Codex → Unified Event 轉換 |

### 1.4 Signals & Metadata

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P1-12 | **Token estimation module** | P0-4 | Token 消耗估算邏輯 |
| P1-13 | **Latency tracking** | P1-1 | 每個 tool call 的 latency 記錄 |
| P1-14 | **Cost attribution signals** | P1-12 | 成本歸因的基礎資料 |
| P1-15 | **Session lifecycle hooks** | P1-2 | Session start/end 事件處理 |

### 1.5 Access Layer

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P1-16 | **Raw trace access layer** | P1-4 | Agent-friendly filesystem-style access (ls/grep/cat) |
| P1-17 | **Structured retrieval interface** | P1-5 | SQL-based 查詢介面（系統內部用） |
| P1-18 | **Session replay capability** | P1-16, P1-2 | 依時間序重建 session 執行過程（支援 analysis evaluation、regression analysis、outcome attribution） |

### Exit Criteria

- [ ] 能完整記錄三種 agent 的 session（100% event capture）
- [ ] 原始 traces 可被 agent 查詢（grep latency < 100ms for single session）
- [ ] 無資料遺失（queue 保證 at-least-once delivery）
- [ ] 支援最小可用的 framework context 記錄
- [ ] Session identity linking 可運作

### 預估時間

2-3 週

---

## Phase 2：Analysis Layer

建立 execution diagnosis 系統。

### 2.1 Analysis Agent Core

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P2-1 | **Analysis Result Schema 設計** | P1-1 | Analysis output JSON schema |
| P2-2 | **Analysis Agent scaffold** | P2-1 | Agent 基礎架構，可自主查詢 traces |
| P2-3 | **Trace reading strategy** | P2-2 | Agent 如何決定讀哪些 traces |

### 2.2 Evaluation Infrastructure

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P2-4 | **Analysis evaluation dataset curation** | P1-18 | 標註過的 session traces for evaluation |
| P2-5 | **Annotation guideline for diagnosis labels** | P2-4 | 標註規範 + inter-annotator agreement policy |
| P2-6 | **Evaluator role definition + adjudication protocol** | P2-5 | 定義誰能標註、衝突如何裁決、何時升級到 framework maintainer review |

### 2.3 Action Classification

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P2-7 | **Action classification taxonomy** | - | 定義 Aligned/Wasteful/Divergent/Exploratory/Premature/Over-verified |
| P2-8 | **Evidence sufficiency rubric** | P2-7 | 如何判斷 evidence 是否足夠的可重現判準 |
| P2-9 | **Action classifier prompt** | P2-7, P2-8 | LLM prompt for action classification |
| P2-10 | **Action classifier evaluation** | P2-9, P2-4, P2-6 | 準確率評估 |

### 2.4 Span / Episode Analysis

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P2-11 | **Span type taxonomy** | - | 定義 investigation/implementation/verification/wandering/recovery span |
| P2-12 | **Span boundary detection** | P2-11 | 將連續 events 聚合成 spans |
| P2-13 | **Span-level diagnosis prompt** | P2-12 | LLM prompt for span diagnosis |

### 2.5 Pattern Detection

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P2-14 | **High-confidence pattern library v1** | - | 預定義的 waste patterns（重複讀檔、過度搜尋等） |
| P2-15 | **Pattern detection and confidence scoring** | P2-14 | 基於規則 + LLM 的 pattern 偵測，含信心分數 |
| P2-16 | **Pattern detection evaluation** | P2-15, P2-4, P2-6 | 準確率/誤判率評估 |

### 2.6 Failure Source Attribution

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P2-17 | **Failure source attribution taxonomy** | - | 定義 model / agent behavior / harness design 三類 |
| P2-18 | **Failure source hypothesis generator** | P2-17, P2-2 | 生成 primary/secondary suspected source + confidence score（非 deterministic classification） |

> **設計說明**：Failure source attribution 輸出為 hypothesis-based，包含：
> - `primary_suspected_source`: 最可能的問題來源
> - `secondary_contributing_sources`: 可能的輔助因素
> - `confidence`: 歸因信心度
> - `evidence_summary`: 支持此歸因的證據摘要
>
> 這反映現實中 model / agent / harness 問題常高度糾纏的特性。

### 2.7 Constraint & Intent Adherence

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P2-19 | **Constraint adherence checker** | P1-3 | 檢查是否違反 framework constraints |
| P2-20 | **Intent drift detection** | P2-2 | 檢查是否偏離 session 任務目標 |

### 2.8 Cost Attribution

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P2-21 | **Per-action cost attribution** | P1-14, P1-1 | 每個 action 的 token/cost 歸因 |
| P2-22 | **Per-span cost aggregation** | P2-12, P2-21 | Span level 的成本彙總 |
| P2-23 | **Waste cost estimation** | P2-7, P2-21 | 計算 wasteful actions 的成本貢獻 |

### 2.9 Framework Philosophy Integration

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P2-24 | **Framework profile loader** | P1-3 | 載入並解析 framework philosophy |
| P2-25 | **Framework profile completeness checker** | P2-24 | 檢查 philosophy 是否完整，不完整時如何 fallback |
| P2-26 | **Philosophy-aware analysis prompt** | P2-24, P2-25 | 將 framework philosophy 納入 analysis context |

### 2.10 Output & Storage

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P2-27 | **Analysis result writer** | P2-1 | 將 analysis 結果寫入 structured store |
| P2-28 | **Analysis result query API** | P2-27 | 查詢歷史 analysis 結果 |

### Exit Criteria

- [ ] 能自主查詢 traces
- [ ] 高信心 waste pattern 偵測準確率 > 80%
- [ ] 誤判率 < 10%
- [ ] 能輸出 span-level diagnosis
- [ ] 能納入 framework philosophy 作為判斷依據
- [ ] 能判斷 evidence sufficiency（Premature / Over-verified）
- [ ] 能輸出 failure source hypothesis（含 confidence）
- [ ] Framework profile completeness check 可運作
- [ ] Evaluation infrastructure 可運作（dataset + annotator protocol）

### 預估時間

4-5 週

---

## Phase 3A：Feedback Layer MVP

建立可運作的 directive 生成、注入與採納追蹤系統。

### 3A.1 Directive Contract

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P3A-1 | **Directive Schema 定稿** | P2-1 | Directive JSON schema v1.0 |
| P3A-2 | **Directive storage** | P3A-1 | Directive CRUD in structured store |
| P3A-3 | **Directive conflict resolution** | P3A-1 | 衝突 directives 的處理邏輯 |

### 3A.2 Advisor Agent

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P3A-4 | **Advisor Agent scaffold** | P3A-1, P2-1 | Agent 基礎架構，讀取 analysis → 生成 directive |
| P3A-5 | **Directive generation prompt** | P3A-4 | LLM prompt for directive generation |
| P3A-6 | **Directive adoptability evaluation** | P3A-5, P2-4 | 測試資料集 + 可採納性評估 |

### 3A.3 Pattern Library

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P3A-7 | **Pattern → Directive mapping** | P2-14, P3A-1 | 預定義 patterns 對應的 directives |
| P3A-8 | **Pattern library storage** | P3A-7 | Pattern library CRUD |

### 3A.4 Injection Mechanisms

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P3A-9 | **Injection interface definition** | P3A-1, P0-5, P0-8 | 定義 injection protocol（含 fallback path） |
| P3A-10 | **Claude Code injection** | P3A-9 | Claude Code 的 feedback 注入機制 |
| P3A-11 | **OpenCode injection** | P3A-9 | OpenCode 的 feedback 注入機制 |
| P3A-12 | **Codex injection** | P3A-9 | Codex 的 feedback 注入機制 |

### 3A.5 Post-run Directive Flow

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P3A-13 | **Session-end analysis trigger** | P2-27 | Session 結束時自動觸發 analysis |
| P3A-14 | **Post-run directive generation** | P3A-4, P3A-13 | Session 結束後生成 directives |
| P3A-15 | **Session-start directive injection** | P3A-14, P3A-2 | 下次 session 開始時注入 directives |

### 3A.6 Basic Adoption Control

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P3A-16 | **Adoption mode router (basic)** | P3A-1 | 根據 target 決定 auto vs review_required |
| P3A-17 | **Basic rollback mechanism** | P3A-2 | 簡易 directive 停用能力 |

### 3A.7 Outcome Tracking

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P3A-18 | **Directive adoption tracker** | P3A-2 | 追蹤 directive 是否被採納 |
| P3A-19 | **Directive outcome tracker** | P3A-18 | 追蹤採納後的效果（行為是否改變） |
| P3A-20 | **Basic regression detector** | P3A-19 | 偵測 feedback 是否造成明顯 regression |

### Exit Criteria

- [ ] 能生成可採納的 directives
- [ ] 至少一種 injection 機制可運作（runtime 或 session-start）
- [ ] Target Agent 行為出現可觀察改變（> 30%）
- [ ] Directive adoption 可追蹤
- [ ] Directive outcome 可追蹤
- [ ] Basic rollback 可運作

### 預估時間

3-4 週

---

## Phase 3B：Bounded Autonomy & Governance

建立安全治理、lifecycle 管理、進階控制與基本 harness optimization 能力。

### 3B.1 Directive Risk & Safety

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P3B-1 | **Directive risk scoring** | P3A-1 | 採納前的風險評估機制 |
| P3B-2 | **Safe/unsafe action boundary policy** | P3B-1 | 定義哪些 directive 類型屬於高風險 |
| P3B-3 | **Directive philosophy consistency validator** | P3A-1, P2-24 | 驗證 directive 是否違反 framework philosophy |

### 3B.2 Advanced Adoption Control

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P3B-4 | **Adoption mode router (advanced)** | P3A-16, P3B-1 | 根據 target + risk + philosophy 決定採納方式 |
| P3B-5 | **Shadow adoption mode** | P3B-4, P1-18 | 模擬 directive 效果而不實際採納 |
| P3B-6 | **Staged rollout strategy** | P3B-4 | 漸進式採納機制 |

### 3B.3 Governance Mechanisms

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P3B-7 | **Rollback / disable protocol** | P3A-17 | 完整的 directive 停用與回滾機制 |
| P3B-8 | **Directive execution guardrails** | P3B-2, P3B-7 | 執行時的邊界控制 |
| P3B-9 | **Kill switch mechanism** | P3B-7 | 緊急停用所有 directives 的能力 |

### 3B.4 Advanced Outcome Analysis

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P3B-10 | **Advanced regression detector** | P3A-20 | 更精細的 regression 偵測與分類 |
| P3B-11 | **Outcome → Analysis feedback loop** | P3A-19, P2-2 | 將 directive outcomes 回饋給 Analysis Agent |
| P3B-12 | **Directive effectiveness scoring** | P3B-11 | 計算 directive 的長期效果分數 |

### 3B.5 Directive Lifecycle

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P3B-13 | **Directive lifecycle manager** | P3A-2 | 管理 directive 的 create/supersede/disable/expire |
| P3B-14 | **Directive expiry enforcement** | P3B-13 | 自動處理過期 directives |
| P3B-15 | **Philosophy conflict detector** | P2-24, P3B-3 | 偵測 profile 內部或跨 directive 的 philosophy 衝突 |

### 3B.6 Harness Optimization (Basic)

| Task ID | Task | 依賴 | 產出 |
|---------|------|------|------|
| P3B-16 | **Harness-level anti-pattern taxonomy** | P2-14 | 定義 harness 層級的問題模式 |
| P3B-17 | **Harness optimization proposal generator** | P3B-16, P3A-4 | 生成 harness 層級的優化建議 |
| P3B-18 | **Harness proposal review bundle** | P3B-17 | 打包給 framework 維護者審閱的建議包 |

> **Phase 3B.6 邊界說明**：
> 本階段僅產出 **review-oriented proposal bundle**，供 framework 維護者審閱與手動採納。
> - ❌ 不自動修改 harness
> - ❌ 不做 implementation patch generation
> - ❌ 不做 deployment-level change orchestration
>
> 上述能力屬於 Phase 4 範疇。

### Exit Criteria

- [ ] Directive risk scoring 可運作
- [ ] Philosophy consistency check 可運作
- [ ] Shadow adoption mode 可運作
- [ ] Staged rollout 可運作
- [ ] 完整 rollback mechanism 可運作
- [ ] Kill switch 可運作
- [ ] Directive lifecycle management 可運作
- [ ] 基本的 harness optimization proposal 可輸出
- [ ] Regression rate < 5%
- [ ] Philosophy violation rate < 5%

### 預估時間

3-4 週

---

## Phase 4：Harness Optimization Pipeline（未來）

完整的 harness / framework optimization 能力，延後實作。

### 預定範圍

| Task | 說明 |
|------|------|
| Harness implementation gap detector | 偵測 framework 設計與實際執行的落差 |
| Framework practice vs philosophy deviation analysis | 分析實踐與設計精神的偏離 |
| Harness change impact estimator | 估算 harness 變更的影響範圍 |
| Cross-framework optimization transfer | 跨 framework 的 pattern 遷移 |
| Automated harness patching | 自動生成 harness 修改 patch |
| Deployment-level change orchestration | 部署層級的變更協調 |

---

## 依賴關係總覽

```
Phase 0 ──────────────────────────────────────────────────────────────
   P0-1,2,3 (hook 調查)
      │
      ├─→ P0-4 (token estimation)
      │
      ├─→ P0-5,6,7 (directive feasibility) ─→ P0-8 (fallback design)
      │
      └─→ P0-9,10,11 (philosophy feasibility)
              │
              └─→ P0-12 (event schema) ─→ P0-13 (storage)
                                              │
                                              └─→ P0-14 (queue)
                                                      │
                                              P0-15 (identity)

Phase 1 ──────────────────────────────────────────────────────────────
   P1-1 (event schema) ─┬─→ P1-4 (raw store) ─┬─→ P1-7 (writer)
                        │                      │
                        ├─→ P1-5 (structured)──┤
                        │                      │
                        └─→ P1-8 (adapter if) ─┴─→ P1-9,10,11 (adapters)

   P1-3 (framework schema) ─→ P2-19, P2-24

Phase 2 ──────────────────────────────────────────────────────────────
   P2-1 (analysis schema) ─→ P2-2 (agent scaffold)
                                    │
              P2-4,5,6 (eval infra) ┤
                                    │
                    ┌───────────────┼───────────────┐
                    ↓               ↓               ↓
               P2-7~10         P2-11~13        P2-14~16
            (action class     (span analysis)  (pattern detect)
             + evidence)
                    │               │               │
                    └───────────────┴───────────────┘
                                    │
                            P2-17~18 (failure source hypothesis)
                                    │
                            P2-21~23 (cost)
                                    │
                            P2-24~26 (philosophy)
                                    │
                            P2-27~28 (output)

Phase 3A ─────────────────────────────────────────────────────────────
   P3A-1 (directive schema) ─→ P3A-4 (advisor agent)
                                    │
                    ┌───────────────┼───────────────┐
                    ↓               ↓               ↓
              P3A-7~8          P3A-9~12        P3A-13~15
            (pattern lib)     (injection)     (post-run)
                    │               │               │
                    └───────────────┴───────────────┘
                                    │
                            P3A-16~17 (basic adoption)
                                    │
                            P3A-18~20 (outcome tracking)

Phase 3B ─────────────────────────────────────────────────────────────
   P3B-1~3 (risk & safety) ─→ P3B-4~6 (advanced adoption)
                                    │
                            P3B-7~9 (governance)
                                    │
                            P3B-10~12 (advanced outcome)
                                    │
                            P3B-13~15 (lifecycle)
                                    │
                            P3B-16~18 (harness basic)
```

---

## 成功指標

### Instrumentation Readiness 指標（必達）

這些指標確保系統具備量測能力，是所有後續評估的基礎。

| Phase | 指標 | 目標 |
|-------|------|------|
| Phase 0 | Hook 調查完成度 | 3 種 agent 全部調查完成 |
| Phase 0 | Runtime injection 可行性報告 | 完成，含 fallback path |
| Phase 0 | Directive comprehension baseline | 有初步測試結果 |
| Phase 1 | Event capture instrumentation | 100% 可觀測 events 被記錄 |
| Phase 1 | Session identity tracking | 可追蹤跨 session 的 directive lineage |
| Phase 2 | Evaluation dataset | 已建立，含標註 |
| Phase 2 | Annotator agreement | Protocol 已定義並執行 |
| Phase 3A | Directive adoption tracking | Instrumented and reportable |
| Phase 3A | Directive outcome tracking | Instrumented and reportable |
| Phase 3B | Risk scoring | Instrumented and reportable |
| Phase 3B | Rollback execution | Instrumented and reportable |

### Target Threshold 指標（Early Stage: Baseline Established）

這些指標在早期階段先建立 baseline，後續迭代再設定具體閾值。

| Phase | 指標 | Early Stage 目標 | 成熟期目標 |
|-------|------|------------------|------------|
| Phase 1 | Trace query latency | < 100ms (single session) | < 50ms |
| Phase 2 | Waste pattern 偵測準確率 | Baseline established | > 80% |
| Phase 2 | 誤判率 | Baseline established | < 10% |
| Phase 2 | Evidence sufficiency 判斷準確率 | Baseline established | > 75% |
| Phase 2 | Failure source hypothesis quality | Baseline established | Top-1 accuracy > 60% |
| Phase 3A | 行為改變率 | Baseline established | > 50% |
| Phase 3A | Efficiency 提升 | Baseline established | > 15% |
| Phase 3B | Regression rate | Baseline established | < 5% |
| Phase 3B | Philosophy violation rate | Baseline established | < 5% |
| Phase 3B | Rollback rate | Baseline established | < 10% |

### Learning-Oriented 長期指標

這些指標反映系統是否真正具備持續學習能力。

| 指標 | 說明 | 目標 |
|------|------|------|
| **Directive retention rate** | 某 directive 在 N 個後續 session 仍然有效的比例 | Baseline established → 趨勢上升 |
| **Directive reuse rate** | 某類 directive 在相近任務被復用的比例 | Baseline established → 趨勢上升 |
| **Adoption-to-outcome causal confidence** | 能歸因行為改變是因為 directive 的信心度 | Baseline established → > 60% |
| **Cross-framework transfer precision** | 部分 pattern 可遷移到其他 framework 的準確率 | Baseline established |
| **False-positive optimization rate** | 優化建議實際上造成負面效果的比例 | Baseline established → < 10% |
| **Self-improvement loop** | Agent 效率隨 session 增加而提升 | 可觀察到趨勢 |
| **Model upgrade benefit** | 底層模型升級後 diagnosis 與 directives 品質同步提升 | 可觀察到提升 |

---

## 時間估計總覽

| Phase | Tasks 數量 | 預估時間 | 累計時間 | Milestone |
|-------|-----------|----------|----------|-----------|
| Phase 0 | 15 | 2-3 週 | 2-3 週 | Risk validation complete |
| Phase 1 | 18 | 2-3 週 | 4-6 週 | Observation layer operational |
| Phase 2 | 28 | 4-5 週 | 8-11 週 | Analysis layer operational |
| Phase 3A | 20 | 3-4 週 | 11-15 週 | **Feedback MVP operational** |
| Phase 3B | 18 | 3-4 週 | 14-19 週 | Bounded autonomy complete |
| **總計** | **99** | - | **14-19 週** | - |

### Milestone 說明

- **Phase 3A 完成**：系統可運作，能生成、注入、追蹤 directive，具備基本 rollback
- **Phase 3B 完成**：系統可治理，具備風險控制、lifecycle 管理、進階採納控制

---

## 風險與注意事項

### Phase 0 風險

| 風險 | 影響 | 緩解策略 |
|------|------|----------|
| Agent hook 機制不夠完整 | 無法取得必要資訊 | 優先調查，若不足則考慮 monkey-patch 或 fork |
| Runtime injection 不可行 | 需啟用 fallback path | P0-8 已設計 fallback，產品降級為 session-start only |
| Agent 無法理解 directive | Feedback 失效 | 測試多種 phrasing，找到 comprehension baseline |
| Framework philosophy 無法取得 | Analysis 品質下降 | 定義最小必要輸入，人工補足 |

### Phase 1 風險

| 風險 | 影響 | 緩解策略 |
|------|------|----------|
| 不同 agent event 格式差異大 | Adapter 開發時間增加 | 先專注 Claude Code，其他漸進支援 |
| 儲存效能不足 | 查詢延遲過高 | 雙層 store + 分層索引 |
| Session identity 難以追蹤 | 跨 session learning 受阻 | Phase 0 先設計好 identity model |

### Phase 2 風險

| 風險 | 影響 | 緩解策略 |
|------|------|----------|
| Action classification 準確率不足 | 診斷品質下降 | 先做高信心模式，漸進擴展 |
| Pattern detection 誤判率過高 | 信任度降低 | 嚴格限制初版 pattern 數量 |
| Evidence sufficiency 難以判斷 | Premature/Over-verified 偵測不準 | 建立 rubric，收集標註資料 |
| Failure source attribution 不準 | 歸因錯誤 | Hypothesis-based output，不過度承諾 |
| 標註者品質不一 | Evaluation 不可靠 | P2-6 定義 evaluator role + adjudication |

### Phase 3A 風險

| 風險 | 影響 | 緩解策略 |
|------|------|----------|
| Injection 不被 agent 理解 | Feedback 失效 | Comprehension test + phrasing iteration |
| Feedback 造成 regression | 任務品質下降 | Basic regression detector + rollback |

### Phase 3B 風險

| 風險 | 影響 | 緩解策略 |
|------|------|----------|
| Shadow mode 不準確 | 模擬與實際不符 | 用 session replay 驗證 |
| Directive 違反 philosophy | 優化偏離設計精神 | Philosophy consistency validator |
| Lifecycle 複雜度過高 | 維護困難 | 漸進增加 lifecycle features |

---

## 版本歷史

| 版本 | 日期 | 變更 |
|------|------|------|
| 1.0 | 2026-04-23 | 初版，基於 PRD v1.3 拆解 |
| 1.1 | 2026-04-23 | 根據 review 補強：Phase 0 風險導向重構、Framework Philosophy Layer 完整化、Bounded Autonomy 落地、Evidence Sufficiency、Failure Source Attribution、Learning-Oriented Metrics、Harness Optimization Basic、Directive Lifecycle |
| 1.2 | 2026-04-23 | Phase 3 拆分為 3A (MVP) + 3B (Governance)、Failure Attribution 改為 hypothesis-based、補 Evaluator Role Definition、補 Runtime Feedback Fallback Design、成功指標分層為 Instrumentation Readiness + Target Threshold、Harness Optimization 邊界明確化 |
