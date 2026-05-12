# Kickoff: Phase 0 Extend

## Problem Statement

原 Phase 0（`changes/2026-04-24_phase0-feasibility/`）完成了 9 個 feasibility validation tasks，但 `plan_v2.md`（寫於 Phase 0 commit 三天後、2026-04-27）的 Phase 0 定義包含 12 個 task——其中 **P0-4**（Claude Code hook subprocess 生命週期）、**P0-6**（Convention comprehension live experiment，protocol 已備好但未跑）、**P0-8**（API Server + thin hook client spike，latency < 10ms 驗證）、**P0-10/11/12 完整版**（project scaffold + config module + schemas module）並未實際執行。這些 gap 來自 PRD→SystemDesign→Plan 流程中 Plan 步驟原本不存在、事後才補上 plan_v2 的歷史錯誤。本 extend 在不放寬 plan_v2 範圍的前提下補完上述 task，讓 plan_v2.md Phase 0 Exit Criteria 6 條全部可誠實打勾，並建立「extend 不能再被 extend」的紀律先例。

## Evidence

- `plan_v2.md` line 73-79 列出 6 條 Phase 0 Exit Criteria，目前可誠實打勾僅 4/6（API Server latency、Project scaffold 兩條無 evidence artifact）
- `changes/2026-04-24_phase0-feasibility/index.yaml` 9 個 task 全部 `done_with_concerns`，但對應的是 plan_v2 P0-1/2/3/5/6/7/9 + Schema POC（task-7）+ Storage POC（task-8）+ Fallback design（不在 plan_v2 P0 範圍內）；P0-4、P0-8、P0-10 完整版、P0-11、P0-12 完整版 沒有對應 artifact
- `src/secondsight/` 目前只有 `poc/` 子目錄，無 `config/` 和 `schemas/` 模組
- 原 Phase 0 commit `b9b0301` message 寫 "Phase 0: Complete feasibility validation"，但實際完成範圍與 plan_v2 Phase 0 範圍不對齊
- `changes/2026-04-24_phase0-feasibility/investigations/directive-comprehension.yaml` 的 27 個（agent × directive × phrasing）cells 全部標記 `compliance: not_tested`，僅完成 protocol 設計

## Risk of Inaction

若不補完此 extend：

1. **若選「重定義 phase boundary」（修改 plan_v2 縮小 Phase 0 範圍）**：plan_v2.md 在 commit 後一週就失約，「Plan 是 source of truth」這個紀律前提崩塌
2. **若選「假裝沒問題、開 carry-over checklist 進 Phase 1」**：`b9b0301` commit message 的 "Complete" 變成謊言，git log 失去語意完整性；同時為未來每個 phase 邊界鬆動立下先例
3. **更深層風險**：每個 phase 都會出現類似 gap，extend 機制變成偷渡性質，紀律框架本身腐爛——Phase 1/2/3 任何一個踩到 plan vs reality 不對齊時，可以合理化「再開一個 extend」這條捷徑

## Scope

### Must-Have (with death conditions)

- **M1：P0-4 — Claude Code hook subprocess 生命週期調查** — Death condition: 若 M3 latency 結果是「同步寫入 < 10ms 已達標」，background work 不再是必須路徑，M1 降級為 nice-to-have

- **M2：P0-6 — Comprehension live experiment 代表性子集** — 至少跑 Claude Code agent × 3 directives × 1 phrasing = 3 cells（最小可行樣本）。Death condition: 若第一個 cell 結果 < 30% 合規率 → 觸發 (α)，立即停止 extend，回到 PRD/system_design 重新評估 Phase 3A 投資

- **M3：P0-8 — API Server + thin hook client spike** — 建最小 FastAPI server + bash/curl hook script，量測 round-trip latency。Death condition: 若 latency 結構性 > 50ms (5x 偏差) → 觸發 (δ)，停止 extend，回到 §3.9 architecture 重新設計

- **M4：P0-10 完整版 + P0-11 — Project scaffold + Config module** — 建 production-quality `src/secondsight/{config,schemas}/`，config.toml loading + per-project override + priority 邏輯（config > env > default）。Death condition: 若 system_design §8.5 config 結構在實作不可行 → 觸發 (β)，回到設計修訂

- **M5：P0-12 完整版 — Schemas module** — Events 11 種 + BehaviorFlagType + FLAG_DEFINITIONS + AnalysisResult + Directive 的 Pydantic models。Death condition: 若實作中發現 schema 細節需 Phase 1 觀測實作後才能定 → 觸發 (ε)，將該 schema 子集遷移 Phase 1，並附書面理由

- **M6：POC review 處置決定書** — 對 `src/secondsight/poc/` 內每個 .py 檔做明確處置決定：refactor / 重寫 / 遷移 Phase 1 / archive。三條路徑必須有書面決定，不允許靜默搬遷。Death condition: 若 review 結論「全部需 Phase 1 context」→ POC 全部 archive，正式 modules 由 M4/M5 從零建。M6 動作仍必須完成（產出處置決定書面化），但結論可能讓 M4/M5 範圍擴大

- **M7：phase0-extend closeout 文件** — 對 plan_v2.md 6 條 Exit Criteria 逐條 alignment + evidence 連結。Death condition: 若 M1-M6 任一未完成 → closeout 改寫成「Phase 0 部分 extended，剩餘項以書面理由 deferred」，且**不能再開 phase0-extend-2**（呼應 damage recipient #4 的緊縮機制）

### Nice-to-Have

- **N1：`pre-commit install` 啟用 hook**——避免 Phase 1 寫程式時 lint 不一致
- **N2：在 system_design.md 把 Codex 的 Phase 0 結論補進去**——對稱於 v2.2 的 OpenCode 更新

### Explicitly Out of Scope

- **O1**：任何 Phase 1+ 的實作（observation pipeline、analysis、feedback、adapters）
- **O2**：Refactor system_design.md / plan_v2.md 的根本架構（plan_v2 在 extend 期間僅可被縮小範圍，不可放寬要求）
- **O3**：Codex / OpenCode adapter 實作（plan_v2 將這些放 P3B-8/9）
- **O4**：Dashboard / API endpoints
- **O5**：完整跑 27 cells comprehension experiment（M2 只要代表性子集；完整矩陣留待 Phase 3A 之前或 Phase 3B 持續驗證）

## North Star

```yaml
metric:
  name: "plan_v2.md Phase 0 exit criteria honest-tick rate"
  definition: |
    Phase 0 exit criteria 6 條中，能被誠實打勾且附可驗證 evidence artifact
    連結的條目數量比例。「誠實」定義為：(a) artifact 存在於 repo / git
    歷史中可被讀取，(b) artifact 內容直接對應該條 criterion，
    (c) 沒有把「做一部分」描述成「完成」。
  current: "4/6 (66.7%) — Claude Code hook / Codex+OpenCode hook / Convention injection feasibility / Storage 已可勾；API server latency / Project scaffold 不可勾"
  target: "6/6 (100%)"
  invalidation_condition: |
    若在 extend 過程中發現「plan_v2.md = source of truth」這個前提本身有誤
    （例如 PRD 重大變動、system_design 結構性 refactor），則此目標失效——
    不再追求 6/6，改為先回到 PRD/system_design 對齊。
  corruption_signature: |
    1. Exit criteria 被打勾但 evidence artifact 是泛述、無具體數字（e.g.,
       "API server scaffolded" 沒有 latency 量測檔）
    2. plan_v2.md 在 extend 期間被修改以「放寬」要求（e.g., 把 "<10ms" 改
       成 "可量測"）；修改只能用於 (ε) 觸發的範圍縮小，且必須在
       changes/<extend>/scar-reports 留下書面理由
    3. POC 程式碼仍被 production 路徑 import，但 POC review 的處置文件聲稱
       「已 archive」

sub_metrics:
  - name: "POC files disposition coverage"
    definition: "src/secondsight/poc/ 內 .py 檔（不含 __init__、conftest）有書面處置決定（refactor / rewrite / migrate / archive）的比例"
    current: "0/2 (0%)"
    target: "2/2 (100%)"
    proxy_confidence: high
    decoupling_detection: "若 disposition 文件聲稱 'archive' 但 grep 顯示 POC code 仍被 src/ 其他 module import → 顯式 game"

  - name: "Hook latency measurement"
    definition: "M3（API Server + thin hook client spike）量測到的 round-trip latency p50 / p95 / p99 數字"
    current: "未量測"
    target: "p50 < 10ms, p95 < 20ms（硬指標）"
    proxy_confidence: high
    decoupling_detection: "量測必須涵蓋 ≥ 3 種 hook 類型（PreToolUse / PostToolUse / Stop），不可只測單一 happy path；樣本數 ≥ 100"

  - name: "Comprehension experiment minimum viable run"
    definition: "M2 跑完的（agent × directive × phrasing）cells 數量，且每個 cell 都有 baseline + with-directive 兩組行為數據"
    current: "0 cells"
    target: "≥ 3 cells（Claude Code × 3 directives × 1 phrasing）"
    proxy_confidence: medium
    decoupling_detection: |
      「跑完」不等於「有效」——必須驗證:
      (a) 每個 cell 有可比較的 baseline 任務 + with-directive 任務
      (b) 行為指標數字不只「完成 / 未完成」，要有量化（重複讀檔次數等）
      (c) 任務本身的設計是否會自然觸發目標行為（避免 baseline 就是 0）

  - name: "Production module integrity"
    definition: "src/secondsight/{config,schemas}/ 模組存在、可 import、有 type annotations、有 unit tests"
    current: "無"
    target: "兩模組都存在，每個 .py 檔有對應 test，pytest 可通過；每 module 至少 1 個 death test"
    proxy_confidence: high
    decoupling_detection: "test 數量達標但只測 happy path → 必須有至少 1 個 death test（samsara 風格） per module"
```

## Stakeholders

- **Decision maker**：yuyu_liao
- **Impacted teams**：Phase 1 開工團隊（同一人）；後續任何讀 git log 的協作者
- **Damage recipients**：
  - `b9b0301` commit 的語意完整性（緊縮：extend commit message 必須顯式 reference 並說明接續關係）
  - extend 機制本身的紀律強度（緊縮：extend 不可再被 extend，這次完成後 plan_v2 6 條必須誠實打勾）
  - Phase 0 / Phase 1 boundary 概念（已由 (ε) kill condition 部分緩解；剩餘弱化作為已知 trade-off 接受）
