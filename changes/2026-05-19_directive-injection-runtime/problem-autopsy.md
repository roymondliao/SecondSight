# Problem Autopsy: Directive Injection Runtime

## original_statement

「那我理解，目前狀況是 fetch directive layer 的結果沒問題，現在的問題點在注入這部分，從 `_ss_inject_conventions` function 的實作上確實有問題。

現在來規劃一下注入這個 feature，這個 injection 的 feature 分兩個注入：
1. 從 analysis 到 directive 的結果，屬於 Conventions 的注入到 system prompt 內
2. 屬於 hit 類型的，是經過分析 user prompt 的指令是否明確、清楚，借鏡 reference_opensoure/claude-code-prompt-improver 這個專案的概念來實作這部分

首先有幾個技術細節要注意，因為 Conventions 的注入不是只對應 claude code 一個 coding agent，目前專案 support claude code / codex，之後會 support opencode，未來可能也會 support other coding agent 的可能。

所以這邊的實作設計必須有可以支援不同 coding agent 的 design pattern 來處理。重點是不同的 coding agent 在注入的方式可能不同
- claude code: additionalContext
- codex: systemMessage or additionalContext
...
這點需要注意，有必要可以查詢 docs，我可以提供。」

## reframed_statement

問題不是「directive 有沒有被產生」，而是「SecondSight 要用什麼 runtime contract，可靠地把不同類型的 guidance 注入到不同 agent 的同步 hook 流程裡」。目前規劃只描述了兩種 injection mode 的存在，但還沒有定義 selection、formatting、transport rendering、agent capability、同步降級行為、與觀測驗證方式。

因此這個 feature 真正要研究的不是單一 `_ss_inject_conventions()` bug，而是兩組 runtime contract：
1. convention injection contract：multi-agent transport contract、SessionStart endpoint shape、server / adapter / shell script 的責任分界
2. hit-based guidance contract：UserPromptSubmit 的同步返回路徑，以及 persisted hints 與 runtime prompt evaluator 的關係

這兩組 contract 若不先定義，後續實作只會把責任混進 shell script 或隨意擴張 directive schema。

## translation_delta

```yaml
translation_delta:
  - original: "fetch directive layer 的結果沒問題，現在的問題點在注入這部分"
    reframed: "資料選取已通，但 transport/runtime contract 未定義完整"
    delta: "原句看起來像單點 bug；實際上是 server、adapter、hook script 三層責任分界不清"

  - original: "這個 injection 的 feature 分兩個注入"
    reframed: "至少有兩條同步 guidance 路徑：project-scoped convention 與 prompt-scoped hit guidance"
    delta: "不是單純兩個文案來源，而是兩條不同觸發時機、資料來源、與 SLA 的 runtime path"

  - original: "借鏡 claude-code-prompt-improver 這個專案的概念"
    reframed: "借鏡其同步 UserPromptSubmit evaluation + structured hook output 模式，而不是複製其通用 prompt-clarity 產品邏輯"
    delta: "需要移植的是 hook contract 與 execution timing，不是整套 vague-prompt decision policy"

  - original: "不同的 coding agent 在注入的方式可能不同"
    reframed: "這不是三個分散小問題，而是 convention injection contract 的核心：agent-specific output rendering 必須是正式抽象，而不是分散在 shell script 的條件分支"
    delta: "multi-agent transport contract、SessionStart endpoint shape、server / adapter / shell script 的責任分界，本質上是同一個設計問題"

  - original: "codex: systemMessage or additionalContext"
    reframed: "Codex SessionStart 已有 project-local hook script 證據可定錨為 top-level systemMessage；其他 event / mode 仍需額外證據"
    delta: "這不再是完全未定狀態。至少 SessionStart 的 convention injection contract 可以先以 `.codex/hooks/samsara-session-start.sh` 為基準收斂"

  - original: "屬於 hit 類型的，是經過分析 user prompt 的指令是否明確、清楚"
    reframed: "hit-based guidance 不是單獨一支 hook script，而是一個需要同時定義 matcher、runtime evaluator、與 persisted hint 邊界的 contract"
    delta: "hit-based path 與 persisted hints 的關係若不先釐清，後續容易把 prompt-improver 類邏輯粗暴塞進 DirectiveType.HINT"
```

## kill_conditions

```yaml
kill_conditions:
  - condition: "如果無法為每個宣稱支援的 agent / hook event 組合找到可驗證的 hook output contract 證據"
    rationale: "沒有 contract 證據的 multi-agent support 只是在製造 silent failure；應縮回只支援已定錨組合。以目前 evidence，Codex SessionStart 可先支援 `systemMessage`，但不能自動外推到其他 event"

  - condition: "如果 convention injection contract 無法把 server、adapter、shell script 三層責任分離，仍需要在 shell script 中硬編 agent-specific JSON"
    rationale: "這代表抽象層次選錯；繼續做只會把 technical debt 固化"

  - condition: "如果 hit-based guidance contract 必須依賴每次 prompt 都查持久化資料、或必須先落 DB 再同步讀回"
    rationale: "UserPromptSubmit 是高頻同步路徑，若設計成重 IO round-trip，延遲與脆弱度會超過功能價值"

  - condition: "如果 transcript / hook capture 無法成為驗證注入成功的權威證據"
    rationale: "只有 API 回應而沒有 agent-observable evidence，功能會持續落入假成功狀態"
```

## damage_recipients

```yaml
damage_recipients:
  - who: "agent 使用者"
    cost: "SessionStart 和 UserPromptSubmit 變成同步等待點；若 render 或 endpoint 設計不良，會直接感知到延遲與噪音"

  - who: "adapter 維護者"
    cost: "需要承擔新的 hook output rendering contract，不能再只負責 event normalization 與單行 text formatting"

  - who: "hook installer / script 維護者"
    cost: "既有 'thin transport only' 假設會被修改；若分層沒做好，腳本將開始承擔 agent-specific 分支"

  - who: "analysis / feedback 維護者"
    cost: "若過早把 runtime prompt evaluator 綁進 persisted hint lifecycle，會增加 schema、aggregation、與 lifecycle 複雜度"

  - who: "測試維護者"
    cost: "需要升級測試基準，從 'stdout 有字串' 轉成 'stdout shape 符合 agent contract，且 transcript 可見'"
```

## observable_done_state

對於一個有 active conventions 的 project，開啟新 Claude session 時，SecondSight 產生的 guidance 會以 Claude 可消費的 `hookSpecificOutput.additionalContext` 出現在 transcript/capture 中；開啟新 Codex session 時，則會以 top-level `systemMessage` 形式出現，而不是只在 server response 或 shell stdout 裡存在。對於一個命中 prompt guidance 規則的 `UserPromptSubmit`，hook 會在同步 SLA 內返回可觀測的 guidance，且 server 不可用時能乾淨降級為空輸出、不阻塞 agent。對於不支援或未定錨 contract 的 agent / event 組合，系統會明確回報 unsupported，而不是默默輸出一個可能被忽略的格式。

## status_note

截至目前研究結論：

- `A. Convention injection contract` 已收斂並定案
- `B. Hit-based guidance contract` 已收斂出核心方向，但尚未定義
  hit categories、signals、與 false-positive guardrails

定案與方向文件：
- [a-convention-injection-contract.md](a-convention-injection-contract.md)
- [b-hit-based-guidance-contract.md](b-hit-based-guidance-contract.md)
