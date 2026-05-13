# Kickoff: Prompt-Trigger Directive Loop

## Problem Statement

SecondSight 目前的回饋迴圈是單向且事後的：agent 執行完一個 session 之後，分析管道偵測行為問題（BehaviorFlags），累積成 Directives，儲存在 DB 裡。使用者透過 dashboard 或 CLI 查看這些 directives，但系統本身不會在「下一次執行之前」使用這些知識來影響行為。

Prompt-improver 開源專案展示了一個互補的模式：在 user 送出 prompt 的瞬間做輕量分析，再用分析結果給予指導。SecondSight 可以借鏡這個「pre-execution check」概念，但填入的不是通用的「prompt 是否模糊」，而是**project-specific 的歷史行為 pattern**：「這個 prompt 形態在這個 project 裡歷史上觸發過哪些 directive 對應的問題？」

這個功能由兩個緊密相連的子能力構成：
1. **Prompt-pattern 提取**（analysis layer）：分析每個 segment 的行為問題時，同時記錄啟動那個 segment 的 user prompt 語言特徵
2. **Pre-execution directive check**（hook layer）：UserPromptSubmit hook 從 fire-and-forget 進化成可以返回 `additionalContext`，將匹配的 directive 注入執行前 context

## Evidence

- `analysis/behavior.py` 目前只分析 agent 工具呼叫行為，完全不看觸發這些行為的 user prompt
- `feedback/` 模組存在但 directive injection 回 agent 是 Phase 3 未完成工作（in-flight: gur-105、gur-108）
- `installer/hook_install.py` 安裝的 hook script 對 UserPromptSubmit 事件只做 POST 觀察，不返回 `additionalContext`
- Prompt-improver 的 hook script（`improve-prompt.py`，71 行）示範了如何用 ~189 tokens 的輕量包裝在每個 prompt 上附加 evaluation context，且不引入額外 API call
- SecondSight 已有 `/api/directives` endpoint 可供 hook script 查詢 active directives

## Risk of Inaction

Directives 將繼續是一個「需要使用者主動去讀 dashboard 才有價值」的靜態資產。這個工具會變成事後驗屍報告，而不是前瞻性指導系統。長期看，若 directive 從不影響執行行為，使用者的動力會下降。

## Scope

### Must-Have (with death conditions)

- **Prompt 語言特徵提取** — 在 `Orchestrator.analyze_session` 的每個 segment 分析結果中加入 `prompt_features`（不超過 3 個結構化特徵：動詞類型、目標明確度、範圍指定詞），作為 directive upsert 時的 trigger pattern 候選  
  Death condition: 如果跨 10 個 project、50 個 session 後，directive 的 `trigger_pattern` 欄位 match rate 低於 5%，代表特徵提取沒有信號——移除

- **Directive schema 擴充**：`trigger_patterns: list[str]`，在 aggregate 階段填入  
  Death condition: 同上，欄位 match rate 低於 5% 時降為 optional/nullable，不再 aggregate

- **Hook 返回值支援**：UserPromptSubmit hook script 支援返回 `additionalContext` JSON（目前純 fire-and-forget）  
  Death condition: 如果 hook 引入 >200ms 延遲（需 GET /api/directives 同步等待），用戶回報干擾感 > 2 次，改為 async 注入或移除

### Nice-to-Have

- AskUserQuestion 互動模式（類似 prompt-improver）：若有高信心 directive match，用選項讓 user 確認
- Directive match 的 confidence score（基於歷史 precision/recall）
- Dashboard 上顯示每個 directive 的「歷史 prompt 觸發範例」

### Explicitly Out of Scope

- 通用 prompt 澄清（prompt-improver 的核心功能）——SecondSight 不做「prompt 模糊評估」，只做「project-specific pattern match」
- 跨 project 的 prompt pattern transfer learning
- LLM-based prompt 評分（純 string pattern matching 先行）

## North Star

```yaml
metric:
  name: "directive-suppression rate"
  definition: "sessions where a pre-execution directive match occurred AND the corresponding flag type count = 0, divided by total sessions with a directive match"
  current: 0  # feature doesn't exist yet
  target: 0.30  # 30% of matched sessions show no corresponding flags
  invalidation_condition: "if users disable pre-execution check in >40% of projects, the friction cost exceeds value"
  corruption_signature: "trigger_pattern match rate rises but flag count stays flat — directive patterns too broad, matching on noise"

sub_metrics:
  - name: "trigger_pattern match rate"
    current: 0
    target: 0.15  # 15% of prompts match at least one directive
    proxy_confidence: medium
    decoupling_detection: "if match rate > 30% consistently, patterns are over-broad; audit top 5 most-triggered directives"

  - name: "hook latency overhead"
    current: 0ms  # currently fire-and-forget
    target: "<100ms p95"
    proxy_confidence: high
    decoupling_detection: "measure hook script wall time independently from server response time"
```

## Stakeholders

- **Decision maker:** yuyu_liao
- **Impacted teams:** Analysis pipeline (orchestrator + behavior), Hook installer, Directive schema + storage
- **Damage recipients:** Users (added pre-execution friction); Analysis pipeline maintainers (increased segment analysis complexity)
