# Discussion Notes — Memory Palace

> Captured during Research phase, before entering Planning.
> Open questions and findings that did not fit kickoff/autopsy structure.

## Feature Positioning (確定的)

SecondSight 從「觀測 agent」擴展到「觀測 human」。這是系統觀測視角的對稱性擴展，不是現有 directive 系統的延伸。

核心設計原則：
- Agent as human, human as agent
- SecondSight 讓 agent 跟 human co-work，像團隊配合
- 不追求 100% alignment，追求 measurable directional improvement

## Mode-Specific Value (確定的)

| Mode | 痛點性質 | 修正機制 | Tier |
|------|---------|---------|------|
| Interactive | 不適（每次都要 re-explain） | User 當場糾正 | Tier 2（副產品） |
| Auto | 靜默失效（偏差累積後才發現） | 無，只能事後 review | Tier 1（核心價值） |

**結論**：feature 核心價值在 auto mode。Interactive mode 是順帶解決的便利性問題。

## Auto Mode Cold Start Paradox (重要發現)

最需要 user model 的地方（auto mode），偏偏最難建立可靠的 model：

- Auto mode 對 user model 品質要求**最高**（沒有修正機會）
- Auto mode 觀測資料**最少**（session 短、聚焦、user prompt 少）
- Interactive mode 提供豐富資料，但對 model 品質需求低

含義：palace 的訓練資料主要來自 interactive mode，但主要消費者是 auto mode。這是一個跨 mode 的 data flow，不是單一 mode 的 self-contained loop。

## Observation Unit (已驗證)

SecondSight 已捕捉 user 對話文字。具體 pipeline：

- `event.py:34` — `USER_PROMPT` event type
- `segmenter.py:73` — `_extract_user_prompt` 從 segment events 抽取
- `analysis/schemas.py:261` — `SegmentData.user_prompt` 欄位
- `prompts/behavior.py:79` — user_prompt 已傳進 LLM 分析

所以 human pattern extraction 不需要從零捕捉資料，需要的是**新的 extraction prompt** + **新的 output schema**，在現有 analysis pipeline 中新增一個 extraction target。

## Open Questions Before Planning

### Q1: Signal 的定義 (最關鍵)

User 描述「觸及到某個訊號，就會進入該記憶宮殿」——但 signal 的具體定義未定。

Session start 時 SecondSight 已知資訊：
- working directory（project_id）
- agent 類型（claude_code / codex）
- 時間

**未知**：user 這次要做什麼 task。
- Interactive mode：第一個 user prompt 才是 signal，但這時 session 已開始
- Auto mode：task 來自外部（ticket / CLI），hook 未必能看到

候選 signal source：
- A. Working directory pattern（粗粒度，session-level）
- B. 第一個 user prompt 的 NLP 分類（細粒度，但需要前置分析）
- C. Task tag / label（需要 user 主動標記，違反「自動」原則）
- D. 全部載入（退化為 flat profile，失去 palace 設計的精髓）

未決定前，「memory palace」的 hierarchical 結構價值未驗證。如果 signal 不可靠，palace 退化為 flat profile，hierarchical 設計只是 dead weight。

### Q2: 與現有 Claude Code memory 的關係

`~/.claude/projects/<project>/memory/` 已存在，是手動維護的 memory palace（`feedback_*.md`、`project_*.md`、`user_*.md`、`reference_*.md`）。

SecondSight memory palace 是同一件事的**自動生成版本**。設計決策：

- Option 1：SecondSight 建立自己的存儲（`~/.secondsight/projects/<project>/user_model/`），與 Claude Code memory 並存
- Option 2：SecondSight 成為 Claude Code memory 的自動寫入者，共用同一個存儲
- Option 3：SecondSight 產出，但作為單獨的 injection path，不寫入 Claude Code 的 memory 目錄

選 Option 2 → SecondSight 邊界擴張，要管理跨工具的 memory 一致性
選 Option 1 → 兩套系統可能衝突或冗餘
選 Option 3 → 最小耦合，但放棄了「成為 Claude Code 生態的一部分」的可能

### Q3: Palace Update 時機

- A. 每次 session analysis 完成後即時更新（fresh but unstable）
- B. 累積 N session batch 更新（stable but lagged）
- C. 只在 user 主動觸發時更新（safest but adds friction）

不同選擇影響：palace content 的 staleness vs accuracy 平衡、analysis pipeline 的負擔、user 對 model 變動的可控性。

## Privacy 邊界 (待確認)

`damage_recipients` 已列出 privacy 風險，但具體邊界未定：

- User model 是否包含原始 prompt text？或只包含 extracted patterns？
- User model 是否可被 export？是否應被 export？
- 如果 user 想刪除 model（reset），路徑是什麼？

這些問題在 planning 階段需要明確 boundary。

## Status

Research 階段尚未結束。Kickoff 和 autopsy 已產出，但 Q1（Signal 定義）和 Q2（與 Claude Code memory 關係）必須在 planning 開始前由 user 決定，否則 planning 的 solution space 會發散。
