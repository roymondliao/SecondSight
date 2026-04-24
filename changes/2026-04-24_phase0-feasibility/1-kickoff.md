# Kickoff: Phase 0 — Exploration & Risk Validation

## Problem Statement

SecondSight 需要從 Claude Code、OpenCode、Codex 三個 agent 平台取得 execution event 資料，並驗證能否將 analysis 結果以 directive 形式注入回 agent。Phase 0 的任務是確認這條資料管道在技術上是否可通，以及在不可通時是否有可行的降級路徑。

## Evidence

- 市場調查已完成：目前包含 Google Cloud Next 2026 發表的 Gemini Enterprise Agent Platform 在內，市面上只做到 Observation 層（traces, spans, anomaly detection），沒有 Analysis + Feedback 的閉環優化能力。
- Google 最接近的能力（Agent Optimizer）停留在「提出建議」，不是自動改寫 policy 並安全上線。
- 三個目標 agent 平台各自有不同的 hook/event 暴露機制，格式差異未知，需要逐一調查。
- Runtime directive injection 是否被 agent 平台支援，目前缺乏系統性測試。

## Risk of Inaction

如果跳過 Phase 0 直接進入 Phase 1 實作：
- Adapter 可能寫到一半才發現某 agent 根本不暴露 tool call level event，導致返工。
- Directive injection 機制可能在 Phase 3 才發現不可行，此時已投入 8-11 週的開發成本。
- 沒有 fallback path 設計，一旦主路徑不通就沒有替代方案，產品定位會被迫被動調整。

## Scope

### Must-Have (with death conditions)

- **Hook 機制調查（P0-1, P0-2, P0-3）** — 死亡條件：三個 agent 中連一個都無法提供 tool call level event，觸發產品架構重新評估
- **Runtime injection 可行性測試（P0-5）+ Fallback design（P0-8）** — 死亡條件：runtime injection 和 session-start injection 都不可行，且 fallback path 無法提供有意義的回饋機制，SecondSight 退化為純 observation + analysis 工具
- **Directive comprehension experiment（P0-6）** — 死亡條件：三種 phrasing 策略下 agent 遵守率都低於 30%
- **Unified Event Schema 草稿（P0-12）** — 死亡條件：三個 agent 的 event 格式差異大到超過 50% 欄位為 agent-specific，改採 per-agent schema + translation layer
- **Storage architecture spike（P0-13）** — 死亡條件：filesystem + SQLite 在 1000 session 規模下查詢延遲超過 500ms
- **Session identity linking design（P0-15）** — 死亡條件：無法建立跨 session 的可靠 identity link，降級為 session-isolated analysis

### Nice-to-Have

- Token estimation 可行性測試（P0-4）
- Session-start injection test（P0-7）
- Framework philosophy acquisition spike（P0-9）
- Framework artifact sources survey（P0-10）
- Minimal philosophy input experiment（P0-11）
- Claim-confirm queue prototype（P0-14）

### Explicitly Out of Scope

- Agent 平台方的商業策略預測
- 具體的 adapter 實作（Phase 1 範疇）
- Analysis prompt engineering（Phase 2 範疇）
- 任何 UI/dashboard 工作
- 效能最佳化（Phase 0 只需驗證可行性，不需最佳化）

## North Star

```yaml
metric:
  name: "Phase 0 Feasibility Confidence Score"
  definition: "6 個 must-have task 各自產出 feasible / partially_feasible / infeasible 判定，加權後的整體信心度"
  current: 0%
  target: ">= 80% (至少 5/6 must-have 為 feasible 或 partially_feasible with viable fallback)"
  invalidation_condition: "如果 agent 平台方在調查期間發布重大 API 變更公告，調查結果的保鮮期會大幅縮短，此指標不再代表未來可行性"
  corruption_signature: "團隊為了達標而將 partially_feasible 樂觀升級為 feasible，忽略 fallback path 的設計品質。偵測方式：每個 partially_feasible 判定必須附帶具體限制清單與 fallback 方案，由第二人 review"

sub_metrics:
  - name: "Hook coverage rate"
    definition: "每個 agent 能暴露的 event 類型數 / SecondSight 需要的 event 類型數"
    current: unknown
    target: ">= 70% per agent"
    proxy_confidence: medium
    decoupling_detection: "如果 hook 暴露的 event 格式不含足夠欄位做 action classification，coverage rate 高但實際可用性低。需用 P0-12 的 schema mapping 交叉驗證"

  - name: "Injection viability"
    definition: "至少一種 injection path (runtime 或 session-start) 在至少一個 agent 上可運作"
    current: unknown
    target: "至少 1 agent × 1 injection path verified"
    proxy_confidence: high
    decoupling_detection: "injection 技術上可行但 agent 實際上忽略 injected content。需用 P0-6 的 comprehension test 交叉驗證"

  - name: "Directive comprehension baseline"
    definition: "agent 對 injected directive 的遵守率"
    current: unknown
    target: ">= 50% compliance rate on test directives"
    proxy_confidence: low
    decoupling_detection: "agent 表面遵守 directive 但行為改變不具實質效果。需在 Phase 2 用 span-level analysis 偵測"
```

## Stakeholders

- **Decision maker:** SecondSight product owner
- **Impacted teams:** SecondSight engineering team（承擔所有 phase 的實作）
- **Damage recipients:** SecondSight 維護團隊（三倍 adapter 維護成本）、agent 平台方（外部依賴壓力）、framework 維護者（新增 directive 衝突的認知負擔）
