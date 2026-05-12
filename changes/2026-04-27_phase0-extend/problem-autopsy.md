# Problem Autopsy: Phase 0 Extend

## 1. original_statement

User 在 2026-04-27 對話中發起此 change 的原始措辭（按時序、verbatim）：

> 「目前根據 docs/plan_v2.md 的內容 phase 0 應該是已經結束了」
>
> 「這確實是一個不好的改動，但遵循原則，不能跳過，所以開一個 phase 0 extend 的來處理沒有做完的事。」
>
> 「會有 plan_v2.md 是因為在討論 system design 後，整個 product 內容更完整，而實際的 workflow 應該是 PRD -> System Design -> Plan 這樣的方式，但因為之前沒有這樣的模式，是這次考量進來的，所以才會變成後續更新了原本的 plan。所以既然是一開始的處理流程錯誤，那現在在實作過程就需要遵循原則把事情做完。」

## 2. reframed_statement

原 Phase 0（`changes/2026-04-24_phase0-feasibility/`）完成的是「**feasibility validation**」（投資可行性研究——9 個 task，全部產出 investigation reports 或 POC code）；但 `plan_v2.md`（補寫於 Phase 0 commit 三天後）的 Phase 0 定義包含「**scaffold + spike**」工作項——project scaffold 完整版、config module、schemas module 完整版、API server latency spike、Claude Code hook subprocess 調查、Convention comprehension live experiment——這些並未實際執行。

範圍 gap 的成因不是執行疏失，而是**流程缺一步**：原本的工作流是 PRD → 直接做事，缺少 System Design 與 Plan 兩個中間步驟。當這次補上 system_design.md 與 plan_v2.md 後，回頭看才發現 Phase 0 的範圍定義（plan_v2）與實際完成（feasibility validation）對不上。

因此 extend 的本質不是「補做幾個 task」，是「**校正 PRD→SystemDesign→Plan 流程缺失，把跳過的步驟補回來，並把該流程內未執行的任務做完**」。校正流程錯誤的紀律意義 ≥ 完成具體 task 的工程意義。

## 3. translation_delta

```yaml
translation_delta:
  - original: "處理沒有做完的事"
    reframed: "校正 PRD→SystemDesign→Plan 流程缺失，補做未執行任務"
    delta: |
      原話「沒有做完」隱含「有開始但沒做完」，但實際情況是 P0-4 / P0-8 /
      P0-11 等是「完全沒開始」——never started, not partially done。
      Reframed 加上了流程層面的意涵（user 在後續澄清確認此為流程錯誤）。
      若以原話為準，extend 的範圍會被誤認為「補完成度」；以 reframed
      為準，範圍是「校正流程 + 補完成度」，後者更深、更系統。

  - original: "遵循原則，不能跳過"
    reframed: "Plan 是 source of truth；流程錯誤的紀律性修正"
    delta: |
      原話的「原則」隱含但未明說具體是哪條原則。Reframed 把它顯化為
      兩層：(a) 該 phase 的 spec 不可被放寬以求容易達成（plan_v2 在
      extend 期間禁止放寬要求），(b) plan 與 reality 必須對齊（不允許
      事後改 plan 把現實合理化）。沒有這兩層顯化，「原則」會被各種
      合理化偷換意涵。

  - original: "一開始的處理流程錯誤"
    reframed: "PRD→SystemDesign→Plan 流程中 Plan 步驟原本不存在的歷史錯誤"
    delta: |
      原話指明「流程錯誤」但未指明哪個環節。Reframed 定位到「Plan 步驟
      不存在」這個具體缺失。這個定位很重要——它把錯誤從「執行不嚴」
      framing 改寫成「流程結構不完整」framing，後者的修正路徑是
      「補上流程步驟」（也是這次 plan_v2 + extend 的雙重產物）。
```

## 4. kill_conditions

```yaml
kill_conditions:
  - id: alpha
    condition: "M2 comprehension experiment 第一個 cell 結果 < 30% 合規率"
    rationale: |
      directive 完全無效，整個 Phase 3A 投資基礎崩塌（呼應 fallback-design
      FB-3 條件）；繼續補完 scaffold 是在錯方向上消耗。應立即停止 extend，
      回到 PRD/system_design 重新評估產品定位。

  - id: beta
    condition: "補 scaffold 過程中發現 plan_v2 / system_design 結構性設計問題"
    rationale: |
      例如做 P0-11 config module 時發現 system_design §8.5 config 結構在
      實作上不可行，需要回頭改設計。觸發回到設計修訂，extend 暫停；繼續
      在錯誤架構上施工是更深的腐爛。

  - id: delta
    condition: "M3 latency spike 結果 p50 > 50ms (5x 偏差於 < 10ms 目標)"
    rationale: |
      thin client + API server 架構假設被否定，需回到 §3.9 重新設計。
      latency 是 hook-based observation 整個架構的硬指標，不可妥協。

  - id: epsilon
    condition: "extend 進行到某 task 時，能寫出明確理由 + 證據證明該 task 最佳完成時機是 Phase 1"
    rationale: |
      紀律不應變成對文字的盲從。有正當理由（例如 P0-12 schemas 細節需
      Phase 1 觀測實作後才能定）的範圍縮小是流程修正的一部分，而非偷渡。
      Trigger threshold：必須「能寫出」明確理由，光有「感覺 Phase 1 較
      合理」不算；理由須在 scar-reports 留下書面紀錄，並同步更新
      plan_v2.md（縮小範圍，不放寬剩下範圍的標準）。
```

註：(γ) 「外部緊迫性 override」候選 kill condition 經 user 確認在當前 context 不適用（個人/研究性質、無 deadline 壓力），明確排除。

## 5. damage_recipients

```yaml
damage_recipients:
  - who: "b9b0301 commit 的語意完整性"
    cost: |
      該 commit 的 message 寫 "Phase 0: Complete feasibility validation"，
      extend 完成後從 git log 讀，會變成「部分完成」——「Complete」這個
      字成為事後不準確的描述。
    mitigation: |
      Extend 完成後的 commit message 必須顯式 reference b9b0301，說明它是
      「接續而非取代」。Closeout 文件（M7）也必須清楚交代兩個 commit 的
      範圍邊界。

  - who: "extend 機制本身的紀律強度"
    cost: |
      如果 extend 太容易啟動，未來 Phase 1/2/3 也可能各自需要 extend，
      plan 的 phase boundary 會持續鬆動。Extend 的存在等於把「plan 不準時
      的逃生口」制度化，需要對應的緊縮機制，否則紀律會被自我中和。
    mitigation: |
      硬規則：extend 不能再被 extend。這次 extend 完成後，plan_v2.md 6 條
      Exit Criteria 必須能誠實打勾，不可再開 phase0-extend-2。若真有未完
      項只能訴諸 (ε) 把它遷到 Phase 1 並更新 plan，不能用 extend-2
      偷渡。此規則寫入 M7 closeout 與 north_star corruption_signature。

  - who: "Phase 0 / Phase 1 boundary 概念"
    cost: |
      嚴格說某些 Phase 0 task（如 P0-12 schemas 細節）的最終形需要
      Phase 1 觀測層實作後才能定，此 extend 在 Phase 0 內完成最終版本帶
      內在矛盾。完成「最終版」可能在 Phase 1 過程中被部分推翻，浪費。
    mitigation: |
      已由 (ε) kill condition 部分緩解——遇到此情況可顯式遷移該 task 到
      Phase 1。剩餘的 boundary 概念弱化作為已知 trade-off 接受：流程紀律
      的價值高於「Phase 0 必須含完整最終版」這個僵化要求。Boundary 概念
      改為「Phase 0 包含可在當前資訊下定案的部分；其餘以 (ε) 顯式遷移」。
```

## 6. observable_done_state

打開 repo 後可從以下三處立刻判斷 extend 是否完成：

(1) **Plan 對齊**：`plan_v2.md` Phase 0 Exit Criteria 6 條全部可誠實打勾，且每條附 `changes/2026-04-27_phase0-extend/` 內 evidence artifact 的相對路徑連結；

(2) **Code 結構**：`src/secondsight/` 有 production-quality 的 `config/` 與 `schemas/` 模組（**非 POC 直接搬遷**），且 `src/secondsight/poc/` 內每個 .py 檔有書面處置決定（M6 產出的 disposition 文件），grep 可驗證沒有 production 路徑 import 已標 archive 的 POC module；

(3) **量測證據**：`changes/2026-04-27_phase0-extend/` 目錄內有 `latency-measurement.md`（含實際 p50/p95/p99 數字 vs < 10ms 目標、≥ 3 種 hook 類型 × ≥ 100 樣本）和 `comprehension-results.yaml`（至少 3 cells 的 baseline + with-directive 行為數據，每個 cell 含可量化指標而非「完成 / 未完成」）。
