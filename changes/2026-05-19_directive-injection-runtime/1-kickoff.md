# Kickoff: directive-injection-runtime

## Problem Statement

SecondSight 已經把 `analysis -> directives` 這段資料流串通，但「把 directive 正確注入到下一次 agent session」的 runtime 設計仍停在高層概念，沒有落到足以實作的 contract。現況的 convention path 能成功 fetch directive，卻在最後一哩把 agent-specific hook output contract 混進 shell script，導致 Claude transcript 中看不到注入內容；而 hit-based path 目前只有 placeholder，還沒有同步返回路徑、matcher contract、或 runtime evaluator 與 persisted hints 的關係定義。這個問題若不先定義清楚，後續實作會把 selection、formatting、transport、agent capability 混寫在一起，造成多 agent 擴充困難與測試盲區。

## Evidence

- `POST /hook/session-start` 目前只返回 `conventions: str`，沒有 agent-specific hook output envelope：
  - [src/secondsight/api/session_start.py](src/secondsight/api/session_start.py)
- `scripts/hooks/session-start.sh` 直接把 conventions 純文字 `printf` 到 stdout，沒有依 agent hook contract 包裝：
  - [scripts/hooks/session-start.sh](scripts/hooks/session-start.sh)
- adapters 目前只有 `inject_convention()` / `inject_hint()`，沒有負責 render final hook output 的介面：
  - [src/secondsight/adapters/base.py](src/secondsight/adapters/base.py)
- Claude transcript 已經顯示 SessionStart 注入需要 `hookSpecificOutput.additionalContext` 類型的結構化 output；目前測試假設與此不一致。
- `HintSelector.match()` 是 stub，`user-prompt.sh` 也只做 observation ingest，沒有任何同步注入返回：
  - [src/secondsight/feedback/hint.py](src/secondsight/feedback/hint.py)
  - [scripts/hooks/user-prompt.sh](scripts/hooks/user-prompt.sh)
- 既有測試把 SessionStart hook 的正確行為定義成「stdout 出現純文字 conventions」，這個假設與 Claude 實際 transcript 顯示的 `hookSpecificOutput.additionalContext` 不一致：
  - [tests/scripts/test_hook_fallback.py](tests/scripts/test_hook_fallback.py)
  - [tests/api/test_session_start.py](tests/api/test_session_start.py)
- Codex 端目前有可直接參考的 project-local SessionStart hook script，明確以 top-level `systemMessage` 做注入：
  - [.codex/hooks/samsara-session-start.sh](.codex/hooks/samsara-session-start.sh)
- Codex 端另外還有 `~/.codex/hooks.json` 與 `~/.codex/hook-captures/*` 可作為 input-side hook contract 證據；至少對 SessionStart 而言，output contract 不再是猜測 `systemMessage` 或 `additionalContext`，而是已有本機實作證據支持 `systemMessage`。
- 先前研究已經指出 pre-execution directive check 需要新增 hook 返回值支援，但沒有進一步定義 multi-agent transport seam：
  - [changes/2026-05-13_prompt-trigger-directive-loop/1-kickoff.md](changes/2026-05-13_prompt-trigger-directive-loop/1-kickoff.md)

## Risk of Inaction

如果現在直接開始做 injection feature，最可能的結果不是功能缺一塊，而是整條路徑在每個 agent 上各自長出不同 shell hack。短期會出現「DB 有 directive、API 回得出內容、但 transcript 看不到注入」這種假成功；中期會讓 Codex / OpenCode support 變成逐 agent 重寫 hook script；長期則會把 hit-based prompt guidance 綁死在 Claude 專屬輸出格式，失去統一 runtime 的可能。

## Scope

### Must-Have (with death conditions)
- **Convention injection runtime contract** — 將 multi-agent transport contract、SessionStart endpoint shape、以及 server / adapter / shell script 三層責任作為同一個設計問題處理。目標是：server 產生 agent-ready payload，adapter 擁有 agent-specific output rendering seam，shell script 保持 thin transport。已知 evidence：Claude Code 需要 `hookSpecificOutput.additionalContext` 類型的輸出；Codex SessionStart 可接受 top-level `systemMessage`。
  Death condition: 若最後設計仍要求每個 hook script 內寫死 agent-specific JSON shape，或 `/hook/session-start` 仍只回純文字、要求 hook script 自己理解 agent contract，代表 runtime 邊界沒有立起來，應退回重設計。

- **Hit-based guidance runtime contract** — 將 UserPromptSubmit 的同步返回路徑，以及 persisted hints 與 runtime prompt evaluator 的關係，視為同一個設計問題。需要先定義：哪些 guidance 是 runtime-only、哪些值得持久化、是否真的要落到 `DirectiveType.HINT` lifecycle。
  Death condition: 若設計強迫所有 prompt guidance 都先寫 DB 再讀回，導致每次 UserPromptSubmit 都依賴持久化 round-trip，應改為 runtime-only first；若在沒有證據前就把 prompt evaluator 綁進既有 directive lifecycle，也應停止往下實作。

- **同步 hook latency / degrade contract** — 定義 SessionStart / UserPromptSubmit 的同步上限、server 不可用時的空輸出策略、以及 output malformed 時的 fail-open 行為。
  Death condition: 若同步 hook 在正常路徑 p95 > 200ms，或 malformed output 可能中斷 agent 啟動/提 prompt，該路徑必須降級為不注入。

- **Test surface 重寫** — 補 agent contract tests、hook stdout tests、transcript-level E2E assertions，並淘汰把 plain-text stdout 視為充分正確的測試假設。
  Death condition: 若新設計沒有一條能直接驗證「transcript 內真的有 injected context」的測試路徑，視為未完成。

### Nice-to-Have
- Agent capability matrix 文件，明確列出每個 agent 支援的 injection mode、hook event、output field，以及其證據來源（docs / capture / transcript）。
- Prompt guidance 的 explainability metadata，例如哪條 directive 命中、為何命中、使用了哪個 matcher。
- Hook output renderer 的 golden fixtures，讓 adapter contract 變更能做 byte-level diff。

### Explicitly Out of Scope
- 通用 prompt clarification assistant 的完整產品化。
- 將 `reference_opensoure/claude-code-prompt-improver` 直接整包嵌入或複製進 runtime。
- 在本階段定義跨 project 的 hint reuse 或 ranking model。
- 先做 dashboard / UI 顯示注入內容。

## North Star

```yaml
metric:
  name: "verified injection success rate"
  definition: "new agent sessions where SecondSight-selected guidance is visible in the agent-observable hook output artifact, divided by sessions expected to inject"
  current: 0.0
  target: 0.95
  invalidation_condition: "if major supported agents expose incompatible or unstable hook output contracts such that a single runtime cannot produce reliable injected context"
  corruption_signature: "server returns non-empty guidance and API metrics look healthy, but transcript/capture artifacts show no injected context"

sub_metrics:
  - name: "session-start transport correctness"
    current: 0.0
    target: 1.0
    proxy_confidence: high
    decoupling_detection: "compare API non-empty responses against transcript/capture evidence for the same launched sessions"

  - name: "user-prompt hit-path correctness"
    current: 0.0
    target: 0.9
    proxy_confidence: medium
    decoupling_detection: "matched prompt events should emit non-empty hook output in capture fixtures; if matcher fires but output remains empty, transport and selection have diverged"

  - name: "sync hook latency p95"
    current: 0
    target: 100
    proxy_confidence: high
    decoupling_detection: "measure shell hook wall-clock separately from server-side endpoint timing to detect local script/rendering overhead"
```

## Stakeholders
- **Decision maker:** yuyu_liao
- **Impacted teams:** feedback/runtime, adapters, hook installer, analysis/aggregation
- **Damage recipients:** agent users (startup / prompt latency), adapter maintainers (new contract surface), future multi-agent support work

## Current Decision Status

- `A. Convention injection contract`: agreed
  - see [a-convention-injection-contract.md](a-convention-injection-contract.md)
- `B. Hit-based guidance contract`: core direction agreed
  - see [b-hit-based-guidance-contract.md](b-hit-based-guidance-contract.md)
  - exact hit categories, signals, and guardrails remain open
