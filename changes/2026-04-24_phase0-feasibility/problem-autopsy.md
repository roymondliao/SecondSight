# Problem Autopsy: Phase 0 — Exploration & Risk Validation

## original_statement

「根據 docs/plan.md 的內容現在要來處理 phase 0 的 task。」

Phase 0 定義（plan.md）：「Phase 0 的目的不只是技術可行性驗證，更是產品核心假設的風險探測。」包含四個面向：Instrumentation Feasibility、Directive Feasibility、Framework Philosophy Feasibility、Architecture Feasibility，共 15 個 task。

## reframed_statement

Phase 0 是 SecondSight 的資料管道可行性驗證。核心問題是：能不能從三個 agent 平台（Claude Code、OpenCode、Codex）取得足夠細粒度的 execution event，以及能不能將 directive 注入回 agent 且被理解。這決定了後續 14-16 週的開發投入是否有技術基礎。

## translation_delta

```yaml
translation_delta:
  - original: "產品核心假設的風險探測"
    reframed: "資料管道可行性驗證"
    delta: "plan.md 的措辭暗示 Phase 0 同時驗證產品假設與技術可行性，但經與 stakeholder 確認，產品假設（市場需要 analysis + feedback）已由市場調查確認。Phase 0 純粹是技術可行性。"

  - original: "15 個 task 跨四個面向"
    reframed: "6 個 must-have task + 9 個 nice-to-have"
    delta: "scope 分析後發現 15 個 task 中只有 6 個是真正的 go/no-go 判定點。其餘 9 個要嘛是加值功能（token estimation）、要嘛是可延後的實作細節（queue prototype）、要嘛可用人工輸入暫時替代（framework philosophy）。"

  - original: "驗證核心產品假設與技術可行性"
    reframed: "確認資料取得路徑是否可通，以及不通時的降級策略"
    delta: "原始措辭缺少 fallback path 的權重。但 P0-8（fallback design）其實是 Phase 0 最關鍵的產出之一——它決定了 SecondSight 在最差情況下的產品底線。"
```

## kill_conditions

```yaml
kill_conditions:
  - condition: "三個 agent 平台中有兩個以上在未來 6 個月內計劃大幅改動 hook/event API"
    rationale: "Phase 0 的調查結果會在短期內作廢，投入調查的時間無法轉化為穩定的設計決策。應等 API 穩定後再投入。"

  - condition: "三個 agent 都只能提供 session-level 粗粒度事件，無法取得 tool call level event"
    rationale: "沒有 tool call level event，Analysis Layer 無法做 action classification 和 span analysis，SecondSight 的核心差異化（相對於市面上的 observation-only 工具）不成立。這不是 fallback 能解決的問題，而是產品架構需要根本重新思考。"

  - condition: "directive injection 在所有 agent 上都不可行，且 session-start injection 也不可行"
    rationale: "如果連 session-start 時注入都做不到，SecondSight 只能做 observation + offline analysis，無法形成閉環。產品定位會大幅縮水，需要重新評估是否值得以當前規模投入。"
```

## damage_recipients

```yaml
damage_recipients:
  - who: "SecondSight 維護團隊"
    cost: "三個 agent 的 adapter 維護成本。每次 agent 平台更新 hook 機制，都需要跟進調整。Phase 0 驗證了「能做」，但同時承諾了長期追蹤 API 變動的義務。"

  - who: "Agent 平台方（Anthropic, OpenCode maintainers, OpenAI）"
    cost: "SecondSight 在其 hook API 上建立依賴後，平台方修改這些介面時多了外部壓力。但 open source 社群的反壓力會限制企業方片面限縮的空間。"

  - who: "使用 SecondSight 的 framework 維護者"
    cost: "需要處理 SecondSight 產生的 directive 與自身設計精神是否衝突的問題。這是新增的認知負擔，即使 SecondSight 內建了 philosophy consistency check。"
```

## observable_done_state

Phase 0 完成時，團隊手上有一份清楚的可行性報告，記錄每個 agent 能暴露的事件類型與限制、directive injection 的可行路徑、以及不可行時的降級策略。Unified Event Schema v0.1 已定義，雙層儲存架構已驗證，session identity model 已設計完成。團隊可以有信心地開始 Phase 1，不會因為「根本拿不到資料」而中途砍掉重練。
