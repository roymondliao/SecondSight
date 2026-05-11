# UI Issues — SecondSight Dashboard

Reviewed on 2026-05-11. All issues are frontend-only (`frontend/src/`).

---

## Bugs (已修復)

### [FIXED] Switch project button 無作用
- **File:** `frontend/src/app.tsx:183`
- **Cause:** `<Button>` 缺少 `type="submit"`；`Button` 元件預設 `type="button"`，不觸發 form `onSubmit`
- **Fix:** 加上 `type="submit"`

---

## Layout

### [FIXED] L-1 Directives 頁面右側大面積空白
- **Page:** `/directives`
- 內容卡片只佔左側 ~43% 寬度，右側 2/3 完全空白
- 建議：卡片擴展至更大寬度，或加入 directive detail panel 佔位結構

### [FIXED] L-2 Observation 空狀態垂直空間浪費
- **Page:** `/observation`
- 三欄卡片撐至 viewport 全高，內容只在上方 ~40%，下方大片空白
- **Fix:** `EmptyPanel` 移除 `h-full`，改為 `min-h-[160px]`，卡片收縮至內容高度

### [FIXED] L-3 Analysis 右欄過空
- **Page:** `/analysis`
- Per-session Report 右欄只有一個小 placeholder card，其餘大量空白
- 建議：即使無資料，layout 也應呈現「這裡會有什麼」的結構

---

## Typography

### [FIXED] T-1 全大寫 label 在窄欄內換行
- **Page:** `/analysis`
- **Fix:** `analysis-view.tsx` 兩處 label 加 `min-w-0` + `truncate`，防止折行

### [FIXED] T-2 Directives 統計數字過大
- **Page:** `/directives`
- 4 個 `0` 數字約 ~48px，在 dashboard 資訊密度場合視覺重量過重
- 建議：縮小至 32px 左右

### [FIXED] T-3 空狀態標題字重過重
- **Pages:** `/observation`, `/analysis`
- "No sessions found"、"Pick a session first" 使用粗體，但這些是 hint 文字而非 action heading
- **Fix:** `EmptyPanel` 標題改為 `font-medium`（500）

---

## UI 元件一致性

### [FIXED] U-1 IDLE / SEGMENTS / TIMELINE pill 視覺過重
- **Page:** `/observation`
- 全寬 pill label 佔用大量空間但幾乎不傳遞資訊
- **Fix:** `EmptyPanel` 眉標改為 `font-mono text-xs uppercase` 純文字，與欄標題同語言

### [FIXED] U-2 空狀態 card border 不一致
- **Pages:** `/observation`, `/analysis`
- 部分用 dashed border（placeholder），部分用 solid rounded border（IDLE card）
- **Fix:** `EmptyPanel` 改為 `border-dashed border-border/80 bg-white/45`，全站統一

### ~~U-3~~ [NOT AN ISSUE — 重新評估後降級]
- Landing 的「Enter dashboard」是首次 CTA 用 primary 實心（強引導），Dashboard 的「Switch project」是低頻操作用 secondary，兩者功能語義不同，差異化是合理設計

### U-4 空白圖表無 placeholder 說明
- **Page:** `/analysis`
- Flag trends 圖區只有空白軸線，沒有 "No data yet" 說明文字
- 建議：加入空狀態文字，避免使用者誤以為是 render 失敗

---

## 有資料時發現的問題（product-test project）

### [FIXED] O-1 Event timeline 事件間距過大
- **Page:** `/observation` → Event timeline 欄
- 每個 event row 佔約 200px，實際內容只有 ~40px，4 個事件就撐滿整個 viewport
- 建議：縮減 event row 的 min-height，讓 timeline 更緊湊

### ~~O-2~~ [FALSE POSITIVE — 已移除]
- `observation-view.tsx:245` 已有條件渲染，Spatial truth 只在有 events 時顯示，行為正確

### [FIXED] O-3 Segment 選中色與整體色系不一致
- **Page:** `/observation` → Level 2 Segments
- Session 選中狀態為深海軍藍（primary），Segment 選中切換為鮮橘紅，視覺上突兀
- 建議：統一使用同一套 active 顏色系統

### [FIXED] O-4 Event timeline 日期格式冗長
- **Fix:** `format.ts` 新增 `formatTimeOnly`；`observation-view.tsx` timeline 改用時間格式

### [FIXED] O-5 Event 類型缺乏視覺區分
- **Fix:** `observation-view.tsx` 加入 `eventTypeBadgeClass`：USER_PROMPT 藍、TOOL_USE 琥珀、SESSION_END 灰

### [FIXED] O-6 Session 名稱過短無視覺保護
- **Page:** `/observation` → Level 1 Sessions
- 其中一筆 session 名稱僅為 `S`，UI 沒有 min-length 保護，視覺上像 truncation bug
- 建議：短名稱加 tooltip 顯示完整 id，或加上最小字元警示

### [FIXED] A-1 "1 SLICES" 單複數錯誤
- **Fix:** `analysis-view.tsx:320` 加入 `distribution.length === 1 ? "slice" : "slices"` 判斷

### [FIXED] A-2 Sessions Analyzed 顯示 0，與 Observation 資料不一致
- **Fix:** `analysis-view.tsx` Analysis list 加入空狀態說明，引導使用者了解分析流程
