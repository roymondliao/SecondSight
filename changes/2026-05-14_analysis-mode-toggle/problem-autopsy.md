# Problem Autopsy: analysis-mode-toggle

## original_statement

> 現在要來重新研究 analysis layer 的問題
> 1. 為什麼 analysis layer 會是走 SDK 的 process 而不是走 coding agent service such as claude code / codex ... etc
> 2. 目前 manual 執行 analysis 會因為要求走 SDK 的處理，所以導致 analysis 無法進行分析
> 3. 另外在測試實際產品時，當 session end event 觸發後出現 `... exc=Set the ANTHROPIC_API_KEY environment variable or pass it via AnthropicProvider(api_key=...) to use the Anthropic provider.`，這看起來也跟 2 有關
> 4. 我記得當初在設計的時候應該有一個 config 參數是選擇要走 SDK 還是走 coding agent service 的設定
>
> （後續澄清）
> 1. 真正的論點是 SecondSight 的產品要能夠 support Coding agent service 跟 SDK 的處理方案，而 user 可以透過 config 參數修改來決定要用哪一個方式。
> 2. default_agent 這個參數設定應該是要決定要用 SDK 還是 coding agent service
> 可以參考 @docs/system_design.md 的內容

## reframed_statement

System design (SD §2.2 / §5.7.3 / §8.5.1) 承諾雙 dispatch 路徑 + config 可選：(a) SDK 模式（PydanticAI 直連 LLM API）、(b) CLI 模式（spawn 使用者本機 coding agent CLI 當 analysis agent）。當前實作只交付了 SDK 路徑，且 SD 中真正承擔 toggle 角色的欄位 `[general] mode` 從未進 schema；使用者誤以為 `[analysis] default_agent` 是 toggle，但它在 SD 中其實是「CLI 模式下選哪個 coding agent」/「SDK 模式下選 model alias」的次級欄位。三個表象（CLI 路徑缺、manual analyze 被迫走 SDK、`ANTHROPIC_API_KEY` 在 session_end 才爆炸）共享同一個根：**設計-實作落差 + SD §8.5.4 規定的 pre-check 從未實作**。

## translation_delta

```yaml
translation_delta:
  - original: "為什麼 analysis layer 會是走 SDK 的 process 而不是走 coding agent service"
    reframed: "為什麼當前 codebase 只有 SDK 路徑、CLI 路徑缺席"
    delta: "原句暗示一個選擇被做出，實際是 GUR-103 explicitly 把 CLI 模式標為 follow-up 並未實作。不是『選了 SDK』，是『只做了 SDK』。"

  - original: "manual 執行 analysis 會因為要求走 SDK 的處理，所以導致 analysis 無法進行分析"
    reframed: "manual analyze 走唯一可用的 SDK 路徑，而 SDK 路徑在 ANTHROPIC_API_KEY 缺漏的環境下會失敗"
    delta: "原句把『要求走 SDK』當失敗原因，實際失敗原因是『SDK 路徑沒有 startup pre-check』(SD §8.5.4 未實作)。即使 CLI 路徑存在，這個 SDK bug 仍會獨立存在。"

  - original: "ANTHROPIC_API_KEY error ... 看起來也跟 2 有關"
    reframed: "ANTHROPIC_API_KEY error 與 manual-analyze failure 同根於『SDK 路徑欠缺 pre-check + key 注入』，但與『mode toggle 缺席』正交"
    delta: "原句把 bug 與架構議題綁定。實作上是兩個獨立 fix：(i) 修 SDK pre-check / key 注入 → day-1 hot patch；(ii) 補 mode toggle + CLI dispatcher → multi-week feature。應該拆 ship。"

  - original: "default_agent 這個參數設定應該是要決定要用 SDK 還是 coding agent service"
    reframed: "SD 中真正的 toggle 是 [general] mode = 'cli'|'sdk'；default_agent 在 SD 中是次級欄位（CLI mode 選哪個 coding agent / SDK mode 選 model alias）"
    delta: "使用者記憶有 toggle 是對的（SD 確實寫了），但記錯欄位名。當前實作把 default_agent 收斂成單純 model 查表 key，是雙重偏離：mode toggle 整個沒做、default_agent 語意也偏離 SD §5.7.3。dishonest-naming 的程度高到連設計者本人都被自己的命名騙了。已於 config review 2026-05-14 lock 為 B4 方案（[analysis.cli] / [analysis.sdk] 巢狀子節），讓 mode 語意由 TOML 結構承載而非 docstring，dishonest-naming 風險直接歸零。"

  - original: "（SD §8.5.3）API key 優先順序：config.toml 有值 > 環境變數 > 不可用"
    reframed: "config 中 empty 字串 = 真的沒設；env 注入只能透過顯式 ${VAR} 內插語法；mode=sdk 時 bootstrap 強制驗證至少一組 provider 解析後有值"
    delta: "SD deviation E1。原 SD 語意 implicit env fallback 容易產生『user 以為 SecondSight 會自動讀 env，實際 loader 沒去讀 → 在 first dispatch 時才 raise』的 silent failure（即當前 ANTHROPIC_API_KEY production bug 的成因之一）。改為 explicit-only 後，user 必須在 config 寫 `\"${VAR}\"` 才會讀 env，意圖外顯；同時讓 bootstrap pre-check 能 deterministic 判斷『這個 config 在這個 mode 下能不能跑』，不依賴 env 在 runtime 的狀態。"

  - original: "（SD §5.7.4 / §8.5.1）fallback_models = ['gpt-4o-mini', 'gemini-2.0-flash']"
    reframed: "SDK mode 只允許單一 fallback model：[analysis.sdk].fallback_model = '...'"
    delta: "SD deviation E3。原 SD 設計多層 fallback chain，但實務上 (i) 第二層以後的 fallback 從未被任何 test 真正驗證走通；(ii) chain 越長 → 失敗時 error 訊息越難辨識真正根因（哪一層失敗？為什麼？）；(iii) cargo-cult 風險高。收斂為單一 fallback 是 yin-side 主動殺死 unverified path。"

  - original: "（SD §3.10 / §8.5.1）raw_traces_ttl_days = 90, analysis_ttl_days = 365"
    reframed: "Built-in default 改為 raw_traces_ttl_days = 30, analysis_ttl_days = 60"
    delta: "SD deviation E4。Rationale: agent 進化速度 > 歷史資料價值衰減 — 6 個月前的 trace 對今天的 directive 已幾乎無 informational value。短 TTL 是承認『資料的腐爛速度比預期快』。User 可在自己 install 加長，但 built-in 不再 default 90/365。"

  - original: "（SD §5.7.1）claude_code 預設模型 = claude-haiku-4-5-20251001（empty 時 fall-back）；codex/opencode empty → ModelSelectionError（schema.py:102-104）"
    reframed: "三個 [analysis.cli.models] 皆 empty 表示『不干涉，讓 coding agent 用自己的 default model』；SecondSight 不再代理 model 選擇權，不再有 fall-back 與 raise 的不對稱"
    delta: "SD deviation E5（superseded by E7）。原本『claude_code empty 會給你預設、codex empty 會炸』是 dishonest defaults — user 從 TOML 看不出兩個欄位行為不同。Q-8 review 時一度規劃為『至少一個必須非空』(E5)；最終由『auto = init-time agent + 不改動 model』語意取代(E7)：empty 不是錯誤，是『不干涉』。三個皆 empty 完全合法。這把 model 選擇權的 source of truth 從 SecondSight config 移交給 coding agent 自己的設定 — 是 yin-side 主動釋出代理權的決策。"

  - original: "（SD §5.7.3）'auto' = 從 observation 記錄中的 agent_type 欄位推導最常用的 agent"
    reframed: "'auto' = resolves to the agent selected at `secondsight init` time（一次性 snapshot），與 observation 事件無關"
    delta: "SD deviation E7。SD 設計的 'auto' 是 ongoing observation aggregation — 持續從 observation 事件 majority-vote 出當前最常用 agent。新語意改為 init-time snapshot — `secondsight init` 時 user 顯式選 / SecondSight 偵測到的 agent type 寫死下來，之後不再變更。Rationale: (i) deterministic — config 加上 init state 就能完全決定 auto 解析結果，不需要重播 observation 才能知道；(ii) cold-start friendly — first-run（observation 還沒任何 event）也能用；(iii) yin-side 簡化：少一條動態判斷路徑 = 少一個潛在 silent failure 來源。Init state 儲存位置（state.json / 環境變數 / DB）留待 planning lock。"

  - original: "（SD §8.5.2）per-project config 支援 [analysis].model、[retention].*、[feedback].* 等 override"
    reframed: "Per-project config 層在此 effort 期間 frozen — 不擴展、不重構，已有欄位行為保留 as-is；新欄位（含 mode override）一律 defer 到 follow-up effort"
    delta: "SD deviation E6。SD 把 per-project override 當作 first-class 設計元素，但實際只有兩個欄位（model、TTL）被實作過，per-project mode 從未存在。Q-6 review 時嘗試把 per-project mode 拉進此 effort，最終決定整層 defer — 理由是 single-user product 還沒有強烈 multi-tenancy / 多 project 不同 LLM 需求的訊號，dual-layer override 複雜度超出當前驗證得到的價值。Defer 條件 + kill conditions 寫在 TODO.md 『Per-Project Config Override: Deferred』section，未來再開新 changes/ research。"

```

## kill_conditions

```yaml
kill_conditions:
  - condition: "Task 4 用盡 prompt iteration budget（3 variants × 10 probes per agent = 30 probes）後，schema-match 率仍 < 95%，且 user 在 escalation packet 中選擇 (d) drop adapter"
    rationale: "Revised 2026-05-14: schema mismatch 被重新框定為 prompt-quality 問題（user 直覺：modern coding agent CLI 具備 follow structured-output 能力，做不到通常是 prompt 沒寫好），不再是 CLI capability 問題。kill condition 因此不再是『PoC 失敗即 drop』(auto-kill)，而是『prompt 工程已盡力 + user 顯式選擇 drop』(human-decided kill)。Drop 只發生在 user-explicit decision 後；自動 drop 是違反這個 reframe 的"

  - condition: "PRD 改變、產品定位回退為 SDK-only 開發者工具（不再面向 end user）"
    rationale: "雙 mode 的價值前提是 end user 不想申請 API key、寧可借用既有 coding agent subscription。失去這個前提後 CLI mode 就只是維護負擔"

  - condition: "GA 後 6 個月 telemetry 顯示 ≥ 95% 安裝皆停留在 default mode 且該 default mode 從未失敗"
    rationale: "Toggle 沒人用 = decorative，違反『存在即責任』；應砍掉 toggle、把贏家寫死"

  - condition: "Anthropic / OpenAI / OpenCode 未來 release 強迫所有 third-party 走 official SDK（headless CLI 被棄）"
    rationale: "CLI 模式的技術基礎消失"
```

## damage_recipients

```yaml
damage_recipients:
  - who: "SecondSight 維護者"
    cost: "Dual dispatch path 永久維護；每個 analysis-related PR 必須 cover 兩條路徑；test matrix 翻倍；session_end → analysis failure 的 root cause 排查路徑變多"

  - who: "End user（onboarding）"
    cost: "config 多一個概念（mode），新增 FAQ『我該選 cli 還是 sdk？』；docs / install smoke 必須說明選擇邏輯"

  - who: "Phase 4 install-smoke / CI"
    cost: "必須 test CLI mode happy path — 要嘛 mock subprocess（mock 與真實 coding agent 行為可能脫鉤），要嘛真的 spawn coding agent CLI（CI 環境需安裝 + auth）"

  - who: "ANTHROPIC_API_KEY error 的短期受害者（當前 SDK 試用者）"
    cost: "在完整 toggle 落地前繼續看到 cryptic 錯誤；若把 bug fix 綁進整個 toggle PR 而非單獨先 ship，受害週期會被拉長"

  - who: "Future debugger（含未來的 AI agent）"
    cost: "dispatch 多一個分支；symptom『analysis 失敗』需要先查 mode 再查路徑；mode auto-derive 邏輯本身可能成為 silent fallback 來源（corruption signature）"

  - who: "Documentation / SD 維護"
    cost: "SD §5.7 / §8.5 從『描述未來』轉為『描述現實』需要重寫；revision history 要記錄這次對齊"
```

## observable_done_state

設定 `[general] mode = "cli"` + 重啟 server，session_end 與 `secondsight analyze` 都會 spawn 使用者本機的 coding agent CLI（claude/codex/opencode）以 analysis prompts + project mount 完成分析，產出寫入 `intelligence.db` 的 schema 與 SDK mode 完全一致；同樣 toggle 切回 `"sdk"` 並故意拔掉 `ANTHROPIC_API_KEY` 時，`secondsight init` / server start 階段就以 actionable error 拒絕啟動（per SD §8.5.4 pre-check），不再讓 background analysis 在 session_end 才以 RouterTerminalError 默默失敗。Telemetry 上每筆 analysis row 帶 `dispatched_via: cli|sdk` 標記，可在 storage layer 直接 query『過去 N 天兩 mode 的 success rate』。
