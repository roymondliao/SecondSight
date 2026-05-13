# Problem Autopsy: Prompt-Trigger Directive Loop

## original_statement

「關於這個專案的特點，如果要整合到 SecondSight 這個 project 會是什麼樣的方式？應該說不是整的專案整合進來，而是這的 open source 的概念是對 user prompt 的分析並給予指導，這點似乎 analysis 可以借鏡？」

## reframed_statement

SecondSight 的 analysis layer 目前只回顧 agent 的工具呼叫行為，不分析觸發這些行為的 user prompt。Prompt-improver 展示了一個可借用的架構模式：在 prompt 進入執行之前先評估它，再基於分析給予指導。

SecondSight 可以借鏡這個模式，但以 project-specific 的方式實現：不做通用「prompt 是否模糊」評估，而是讓 analysis pipeline 在偵測 BehaviorFlags 時同時提取「哪種 prompt 語言特徵觸發了這個問題」，把這些特徵附加到 Directives，再讓 UserPromptSubmit hook 在執行前比對當前 prompt，於匹配時注入 directive 提醒。

這樣 SecondSight 就從「事後告訴你做錯了什麼」進化成「在你即將重複同樣錯誤時提前攔截」。

## translation_delta

```yaml
translation_delta:
  - original: "如果要整合到 SecondSight 這個 project"
    reframed: "借鏡特定概念，不是引入整個 plugin 或其依賴"
    delta: "User 明確說不是整合整個專案——重要的是概念移植，而不是程式碼複用"

  - original: "對 user prompt 的分析並給予指導"
    reframed: "分析 prompt 對應 project-specific 歷史行為 pattern，給予 directive-grounded 指導"
    delta: "Prompt-improver 做通用澄清；SecondSight 做的是 project-specific pattern match。前者是「這個 prompt 說清楚了嗎？」，後者是「這個 prompt 在這個 project 裡歷史上導致了什麼？」"

  - original: "analysis 可以借鏡"
    reframed: "analysis pipeline 需要新增 prompt-feature extraction；hook 需要新增 directive-check 返回值"
    delta: "「借鏡 analysis」被我拆成兩個獨立工作：(A) analysis 層提取 trigger pattern，(B) hook 層使用 trigger pattern。兩者都需要，但 A 是 B 的前提"
```

## kill_conditions

```yaml
kill_conditions:
  - condition: "feedback/ directive injection 路徑（Phase 3）確認不會在近期 ship"
    rationale: "Pre-execution directive check 的價值來自 directives 的品質與數量。如果 directive lifecycle 管理和 injection 邏輯本身都沒有 ship，在上面加 prompt trigger 是在空洞的地基上建樓"

  - condition: "兩個 in-flight features (config-unification, server-analysis-runtime-wiring) 任一出現重大架構翻轉"
    rationale: "Prompt-trigger 功能需要穩定的 Orchestrator 管道（runtime-wiring）和一致的 config 模型（config-unification）。如果這兩個 feature 的方向改變，prompt-trigger 的 integration point 也會跟著變"

  - condition: "hook script 的 GET /api/directives 呼叫在 p95 > 200ms"
    rationale: "Pre-execution check 的成本是每次 prompt 都要等伺服器回應。超過 200ms 代表 SecondSight server 的反應速度會成為使用者感知的瓶頸，摩擦成本超過 directive 指導的價值"

  - condition: "在 5 個以上真實 project 測試後，trigger_pattern match rate 長期低於 5%"
    rationale: "代表從 session 分析中提取的 prompt 特徵無法有效預測哪些 prompt 會導致問題——信號不存在，功能應降級或移除"
```

## damage_recipients

```yaml
damage_recipients:
  - who: "使用者（user）"
    cost: "每次 prompt 提交前增加了一個 latency hop（hook → GET /api/directives → 返回 additionalContext）。即使只有 50ms，在長時間工作流中會累積成可感知的延遲"

  - who: "Analysis pipeline 維護者"
    cost: "Segment 分析邏輯需要在每個 BehaviorFlag 偵測結果旁附加 prompt_features 提取——增加 LLM prompt 複雜度和輸出 schema。SegmentAnalysis Pydantic model 需要新欄位"

  - who: "Directive schema 和 storage layer"
    cost: "DirectivesRepository 的 upsert 邏輯需要處理 trigger_patterns 的 merge（多個 session 可能產生不同 pattern，需要聚合策略）。DB schema migration 不可避免"

  - who: "Hook installer"
    cost: "現有 hook script 是 fire-and-forget，改成需要返回 JSON 的同步呼叫，且需要 fallback（server 不可用時不能讓 hook 阻塞 Claude Code）"
```

## observable_done_state

在有 active directive 的 project 裡，當 user 提交一個 prompt（例如「幫我 explore 所有相關的 config files 再修 bug」），SecondSight hook 在 200ms 內返回 `additionalContext`，Claude 在執行前看到「⚠ Directive #3：這個 project 的歷史顯示『explore all』型 prompt 導致 redundant_exploration（出現 8 次），建議先確認目標檔案範圍再 explore」。使用者可以繼續或修改 prompt。對比基準：無 pre-execution check 的相同 project 的同類 prompt session，flagged behavior 出現率下降 ≥ 30%。
