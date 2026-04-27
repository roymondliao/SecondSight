# SecondSight Backlog

延後但有價值的 feature 與架構方向。每項記錄：

- **What**：做什麼
- **Why deferred**：為什麼現在不做
- **When to revisit**：什麼條件下該重新拿出來討論
- **Context**：相關討論或文件鏈結

---

## Evaluation Layer

**Date logged:** 2026-04-27

**What:**
在 Analysis Layer 與 Feedback Layer 之間插入一個驗證閘，每條 directive 候選必須通過效果驗證才會被升級為實際注入的 directive。

具體形式（討論共識）：
- **部署前 gate**：固定 benchmark task 上跑 baseline vs with-directive 的控制實驗，作為 sanity check
- **部署後監測**：縱向追蹤 directive 想修的目標指標（例如「同檔重讀次數 / session」），趨勢沒改善則自動 retire
- **雙維度追蹤**：必須同時看「目標指標」和「任務完成品質」，避免 Goodhart's law 反噬（指標達標但任務完成率下降）
- **Directive contract 擴充**：每條 directive 必須帶 `target_metric` + `measurement_protocol` 欄位

**Why deferred:**
現有 Phase 1/2/3 路線的核心目標是先把「觀測 → 分析 → 注入」閉環跑起來，加進 Evaluation Layer 會擴大範圍、延後核心交付。Phase 0 的 directive-comprehension 已經設計了一次性驗證 protocol，足以在 Phase 1 驗證核心命題（directive 是否被遵循）。把驗證從一次性實驗提升為持續架構元件是優化動作，不是必要動作。

**When to revisit:**
- Phase 1 directive-comprehension live test 完成後（若結果落在 30%~50% 合規區間，evaluation gate 變成必需而非選配）
- Phase 3A 完成、cross-session learning（Phase 3B）開始設計時——它跟 Phase 3B 高度重疊，可能直接合併
- 開始觀察到「analysis 產生明顯無效 directive 卻仍被自動注入」的具體案例時

**Context:**
- 起源討論：2026-04-27 conversation about Phase 0 investigation review
- 死亡情境：呼應 Phase 0 directive-comprehension 的 acknowledgment-without-behavior（DC-A）
- 設計腐爛點：evaluation 指標設計本身的 Goodhart's law 風險，必須在實作時雙維度追蹤

**Constraint on current design（不要把路堵死）:**
- 不要在 system_design.md / plan_v2.md 把現有 feedback 路徑寫死成「無法插入驗證閘」的結構
- Directive contract schema 保留 `target_metric` 與 `measurement_protocol` 欄位的擴充空間（Phase 3A 可以先不填，但 schema 不能拒絕這些欄位）
