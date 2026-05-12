# SecondSight System Design

**版本**：2.1
**日期**：2026 年 4 月 27 日
**基於**：PRD v1.3 / Plan v1.2

---

## 一、設計總覽

SecondSight 是一個 local-first 的 AI agent execution intelligence 系統，由三個核心支柱組成：Observation（觀測）、Analysis（分析）、Feedback（回饋），形成持續優化的閉環。

本文件記錄經討論確認的 system design 決策，作為後續開發的架構基準。

---

## 二、部署型態與執行模型

### 2.1 Observation Layer：Event-Driven 監控（單一模式）

SecondSight 的觀測層只有一種運作方式：採用 event-driven 機制監控使用者在使用 coding agent（Claude Code、OpenCode、Codex）時的行為。不論使用者是 interactive mode 還是 headless CLI 模式，觀測層都會捕捉 agent 的 tool calls、session events 等執行資料，寫入 raw trace store。

具體機制依 agent 而異（詳見第四節 Agent Adapter 架構）：

- **Claude Code、Codex**：走 subprocess hook（`settings.json` / `config.toml` 註冊），由 agent 在事件發生時主動觸發 hook script
- **OpenCode**：走 fs-event watching `~/.local/share/opencode/opencode.db`（SQLite WAL mode）+ 唯讀增量讀取，避開 OpenCode 官方 plugin 須跑在 Bun runtime 的限制（詳見 ADR-014）

### 2.2 Analysis Layer：兩種觸發方式

觀測資料的分析支援兩種觸發方式，差異在於 **誰提供 agent loop**：

| Mode | Agent Loop 由誰提供 | SecondSight 的角色 | 使用場景 |
|------|---------------------|-------------------|----------|
| **CLI 模式** | Coding agent 本身（Claude Code / Codex 等） | 提供 analysis prompts + tools + project folder access | 一般使用者，直接用 coding agent 做分析 |
| **SDK 模式** | SecondSight SDK 內建（基於 PydanticAI） | 提供完整 agent loop + tools，可自選 LLM provider | 開發者整合、自建 pipeline |

**CLI 模式**：使用者透過 coding agent CLI（如 Claude Code、Codex）觸發分析。Coding agent 本身就有 agentic loop 和 file access 能力，SecondSight 只需要提供分析用的 prompts/instructions、trace 資料、以及將 project folder 掛載進去，coding agent 自行判斷需要讀哪些檔案、做什麼分析。

**SDK 模式**：開發者透過 Python SDK 程式化呼叫。SecondSight 提供基於 PydanticAI 的 reference agent loop 實作，內建 tool calling 能力（讀 traces、讀 project 檔案、查 structured store），可自選 LLM provider 的 endpoint 與 token。

```
Observation（單一模式）
  Hook 監控 coding agent → 寫入 raw traces

Analysis（兩種觸發方式）
  CLI 模式：Coding agent 提供 loop → SecondSight 提供 prompts + tools
  SDK 模式：PydanticAI 提供 loop → 開發者自選 LLM provider
```

### 2.3 架構原則：Library-First

SecondSight Core 是一個 **Python library**，CLI 和 SDK 共用同一套 core logic（schemas、storage、analysis、feedback），差別只在觸發與呼叫方式。

API Server（FastAPI）是 **core component，常駐運行**，同時服務三個角色：

1. **Hook fast path**：hook script 透過 localhost HTTP 與 server 通信，server 做 async observation 寫入
2. **Frontend dashboard**：提供 human-readable 的分析結果、directive 管理介面
3. **Internal API**：CLI 和 SDK 的 backend，提供 analysis 觸發、directive 查詢等功能

```
src/secondsight/
├── config/             # Config loading, TOML parsing, defaults
│   ├── loader.py       #   config.toml read/write
│   └── settings.py     #   Config Pydantic settings
├── schemas/            # Pydantic models — shared data contracts
│   ├── events.py       #   Event types (SessionStart, ToolUseStart, etc.)
│   ├── analysis.py     #   AnalysisResult, BehaviorFlag, SessionReport
│   └── directives.py   #   Directive, Convention (Hint reserved)
├── observation/        # Hook → normalize → persist pipeline
│   ├── tracker.py      #   Session state tracking (segment index, sub-agent nesting)
│   └── pipeline.py     #   Ingest pipeline (filesystem + DB write)
├── storage/            # Persistence layer — DB + filesystem
│   ├── models/         #   SQLAlchemy Core table definitions
│   ├── repositories/   #   event_repo, session_repo, analysis_repo, directive_repo
│   ├── migrations/     #   Alembic
│   ├── raw_trace_store.py
│   └── engine.py       #   DB engine factory + PRAGMA config
├── analysis/           # Two-layer analysis logic
│   ├── prompts/        #   Analysis prompt templates
│   │   ├── behavior.py #     Session behavior analysis prompt
│   │   ├── summary.py  #     Session-level report prompt
│   │   └── aggregate.py#     Cross-session aggregation prompt
│   ├── segmenter.py    #   Event pairing (tool_use_start/end), segment loading
│   ├── orchestrator.py #   Session analysis + cross-session aggregation
│   ├── metrics.py      #   Supplementary metrics
│   ├── behavior.py     #   Behavior flag detection + classification
│   └── aggregator.py   #   Cross-session statistics + convention generation
├── feedback/           # Directive injection logic
│   ├── convention.py   #   Convention selection, budget enforcement
│   ├── hint.py         #   Hint matching (reserved, Phase 0 不實作)
│   └── lifecycle.py    #   Directive state machine
├── adapters/           # Agent-specific hook adapters (cross-cutting)
│   ├── base.py         #   Abstract adapter interface
│   ├── claude_code.py  #   Claude Code hook capture + injection
│   ├── codex.py
│   └── opencode.py
├── sdk/                # Public SDK interface (PydanticAI agent loop)
├── api/                # FastAPI server (core: hook endpoints + dashboard + internal API)
│   ├── hooks.py        #   POST /hook/session-start, /hook/pre-tool-use, etc.
│   ├── observation.py  #   GET /api/sessions, /api/sessions/{id}/segments, ...
│   ├── analysis.py     #   GET /api/analysis/summary, /api/analysis/sessions/{id}, ...
│   ├── directives.py   #   GET/PATCH /api/directives
│   └── server.py       #   Server lifecycle (startup, shutdown, daemon mode)
└── cli/                # Typer CLI
```

**Module 職責劃分**：

- `config/` + `schemas/`：shared infrastructure，所有 module 可用
- `observation/`：hook 進來的事件處理 pipeline（normalize → persist）
- `storage/`：persistence layer（DB + filesystem），被 observation、analysis、feedback 共用
- `analysis/` + `feedback/`：domain logic modules，透過 repository 存取資料
- `adapters/`：cross-cutting integration layer，映射各 agent 的 hook 到 SecondSight 的統一 event model
- `api/`：core component，hook fast path + dashboard + internal API，常駐運行

**Module 依賴方向**（digraph）：

```mermaid
digraph module_deps {
    rankdir=TB;
    node [shape=box];

    // Entry layer
    cli; api; sdk;

    // Business layer
    adapters; observation; analysis; feedback;

    // Foundation layer
    storage; schemas; config;

    // Entry → Business
    cli -> {analysis feedback};
    api -> {adapters observation analysis feedback};
    sdk -> {analysis feedback};

    // Business → Business
    adapters -> {observation feedback};
    analysis -> {feedback};
    observation -> storage;

    // Business → Foundation
    analysis -> storage;
    feedback -> storage;
    adapters -> schemas;
    observation -> schemas;
    analysis -> schemas;
    feedback -> schemas;

    // Foundation (shared, 所有 module 可用)
    storage -> {schemas config};
}
```

---

## 三、儲存架構

### 3.1 設計原則：Filesystem-First

**Filesystem 是 source of truth，structured store 是 derived index。**

這個設計基於以下判斷：

- Filesystem write 本身就是 durable 的，寫進去就不會丟
- Agent 可直接用 `ls/grep/cat` 操作 raw traces，符合 PRD 的 agent-friendly 設計
- 如果 DB 損壞或 schema migration 出錯，可從 filesystem 重建，零資料遺失
- 符合 PRD 原則「Preserve Raw Evidence」

### 3.2 拿掉 Claim-Confirm Queue

原 PRD 提到的 durable queue / claim-confirm pattern 經評估後決定移除，理由：

- SecondSight 是 local-first、單機、單 user、低 throughput（每秒 0.5–2 events）
- Filesystem write 已提供 durability 保證，不需要額外的 queue 層
- Claim-confirm pattern 適合分散式系統、多 consumer、高吞吐場景，不符合 SecondSight 的使用情境
- claude-mem 等成熟的 local-first 工具驗證了 filesystem-first 模式的可行性

**對 Plan 的影響**：P0-14（queue prototype）、P1-6（queue 實作）移除或降級為 optional。

### 3.3 Per-Project 儲存

為支援使用者同時開多個 coding agent 在不同 project 工作的場景，採用 **per-project DB + global registry** 架構。

**Per-project 好處**：

- 跨 project 零 write lock contention（各自獨立的 DB file）
- Framework philosophy 天然隔離
- Directive scope 不跨 project 汙染
- 單一 project 可獨立 archive / 刪除
- DB file size 不會因 project 數量膨脹
- Backup / migration 粒度是 project level

**目錄結構**：

```
~/.secondsight/
  config.toml                        # Global：API keys, LLM provider settings, user preferences
  registry.db                        # Global：project index, global stats, cross-project pattern cache
  projects/
    {project_id}/
      config.toml                    # Per-project：override global settings
      intelligence.db                # Per-project：behavior flags, directives, aggregation stats
      sessions/
        {session_id}/
          events/
            {timestamp}_{event_type}.json   # Raw traces
          metadata.json
          session_report.json               # Session 行為報告（filesystem 備份）
```

**Project ID 決定方式**（優先順序）：

1. 有 git remote origin → 用 remote URL 的 hash
2. 有 git repo 但無 remote → 用 repo root absolute path 的 hash
3. 非 git repo → 用 working directory path 的 hash
4. 使用者可透過 `secondsight init` 手動指定 project name 來 override

### 3.4 DB Sync 機制

Hook 寫完 filesystem 後，以 **event-driven sync** 方式同步到 structured store：

- Hook 寫完 raw trace JSON 後，嘗試做一個輕量的 DB INSERT
- 如果 DB INSERT 成功，同步完成
- 如果 DB INSERT 失敗（DB lock、process crash 等），raw trace 已安全落地，不影響資料完整性
- 可透過 lazy sync 補上：Analysis Agent 或 API server 查詢時，檢查並補同步尚未入庫的 raw traces
- 提供 `secondsight sync` CLI 指令做手動全量重建

### 3.5 SQLite 配置

所有 SQLite DB（registry.db、intelligence.db）統一啟用以下 PRAGMA。分為 **硬編碼**（best practice，不應修改）和 **可配置**（環境相關，透過 config 調整）：

```python
def configure_connection(conn, settings: StorageSettings):
    # ── 硬編碼：best practice，不開放修改 ──
    conn.execute("PRAGMA journal_mode=WAL;")           # 允許 concurrent read/write
    conn.execute("PRAGMA busy_timeout=5000;")           # lock contention 時等待 5 秒
    conn.execute("PRAGMA synchronous=NORMAL;")          # WAL mode 下 NORMAL 即安全
    conn.execute("PRAGMA wal_autocheckpoint=1000;")     # 控制 WAL file 大小

    # ── 可配置：透過 config.toml [storage.sqlite] 調整 ──
    conn.execute(f"PRAGMA cache_size=-{settings.cache_size_mb * 1000};")  # default: 64MB
```

```toml
# config.toml
[storage.sqlite]
cache_size_mb = 64    # SQLite in-memory cache（MB），記憶體有限的機器可調小
```

**WAL mode 的必要性**：即使是 per-project DB，同一 project 內仍有多個角色同時存取（hook INSERT、API server SELECT、Analysis Agent read/write）。WAL 確保 writer 不擋 reader，減少 interactive mode 體感延遲。

### 3.6 未來 Scale Path

當單一 project 的 observation 數據成長到百萬筆級別，SQLite 可能面臨 analytical query 效能瓶頸。屆時的 migration path：

- **DuckDB**：embedded、zero-dependency、單檔案，analytical query 效能遠優於 SQLite，Python 整合成熟
- 參考方向：Langfuse（ClickHouse, server-based）、Arize Phoenix（SQLite + pandas）
- 由於使用 SQLAlchemy Core + Repository pattern，DB 切換成本可控

### 3.7 Event Model

#### 3.7.1 設計原則

SecondSight 定義自己的 **抽象 Event Model**，不是任何特定 agent hook 的 1:1 映射。Adapter 負責將各 agent 的 hook payload 映射到此抽象層。如果某個 agent 不支援特定 event type（例如 Codex 可能沒有 compact event），該 event type 就不會產出，analysis 層需能 handle 缺少某些 event types 的情況。

觀測粒度原則：**存行為結構與必要 context，不存工具操作的完整內容**。SecondSight 是 local-first 服務，資料完全在使用者自己的機器上，不存在資料敏感性問題。

#### 3.7.2 Event Types

| event_type | 說明 | 觸發來源（Claude Code） | Codex / OpenCode |
|-----------|------|------------------------|------------------|
| `session_start` | Session 開始，feedback 注入點 | SessionStart hook | 待 Phase 0 調查 |
| `session_end` | Session 結束，analysis 觸發點 | SessionEnd hook | 待 Phase 0 調查 |
| `user_prompt` | 使用者下指令，segment 邊界 | UserPromptSubmit hook | 待 Phase 0 調查 |
| `thinking` | Agent 推理（duration + token count） | 從 LLM response 推導 | 待 Phase 0 調查 |
| `tool_use_start` | Tool call 開始 | PreToolUse hook | 待 Phase 0 調查 |
| `tool_use_end` | Tool call 結束（含成功/失敗） | PostToolUse / PostToolUseFailure hook | 待 Phase 0 調查 |
| `sub_agent_start` | Sub agent 啟動 | SubagentStart hook | 待 Phase 0 調查 |
| `sub_agent_end` | Sub agent 結束 | SubagentStop hook | 待 Phase 0 調查 |
| `task_created` | Plan mode task 建立 | TaskCreated hook | 待 Phase 0 調查 |
| `task_completed` | Plan mode task 完成（成功/失敗） | TaskCompleted hook | 待 Phase 0 調查 |
| `response` | Agent 回應（token count + has_code_block） | Stop / StopFailure hook | 待 Phase 0 調查 |

**不擷取的 Claude Code hooks**（與行為分析無直接關聯）：

- `PostToolBatch`：可從連續 tool_use_end events 推導
- `PermissionRequest`：使用結果已反映在 tool_use_end 的 success/failure
- `PreCompact` / `PostCompact`：context 壓縮是 agent 內部行為，不影響行為觀測
- `Notification` / `ConfigChange` / `WorktreeCreate` / `WorktreeRemove` / `CwdChanged` / `FileChanged` / `TeammateIdle` / `InstructionsLoaded`：環境性事件，非行為

#### 3.7.3 Event Type 的資料結構

**Pre/Post Tool Use 分開存**：hook 在 PreToolUse 時只知道 tool_name + target，duration 和 success 在 PostToolUse 才拿到。分開存的好處：寫入快（直接 append，不需 UPDATE）、保留真實執行順序、不需配對 ID 機制。Analysis 層在需要完整 tool call 資訊時，按 sequence_number 配對 start/end。

各 event type 的 `data` JSON 內容：

```json
// session_start
{"external_session_ref": "claude-session-abc123", "agent_version": "1.2.0"}

// session_end
{"reason": "normal" | "timeout" | "error", "total_events": 42}

// user_prompt
{"prompt_text": "幫我修 src/utils.py 的 bug", "prompt_sequence": 1}

// thinking
{"thinking_token_count": 2000}
// duration_ms 用共用欄位

// tool_use_start
{"tool_name": "Read", "action_target": "/src/utils.py", "action_metadata": {"lines": "1-50"}}

// tool_use_end
{"success": true, "error_type": null, "output_size": 50}
// duration_ms 用共用欄位

// sub_agent_start
{"task": "分析現有 auth 實作", "sub_agent_id": "sa_001", "parent_agent_id": null}

// sub_agent_end
{"sub_agent_id": "sa_001", "success": true, "total_token_count": 3500, "total_tool_calls": 2}

// task_created
{"task_id": "task_001", "task_description": "修復 login 邏輯", "plan_step": 1, "total_steps": 5}

// task_completed
{"task_id": "task_001", "success": true, "plan_step": 1}

// response
{"response_token_count": 300, "has_code_block": true, "stop_reason": "normal" | "failure"}
```

#### 3.7.4 觀測資料粒度

核心原則：**存 action + target + metadata，不存 input/output content**。

**User Prompt**：完整儲存。User prompt 是判斷 agent 行為對齊度的核心依據。

**Thinking**：存 duration + token_count，不存 thinking 內容（量大且分析層不需要完整文字）。

**Tool Use**：存 tool_name + action_target + action_metadata + success + error_type + output_size，不存 input/output content。

具體範例：

- `Read("/src/main.py", lines=1-50)` → `tool_name: "Read"`, `action_target: "/src/main.py"`, `action_metadata: {lines: "1-50"}`, `output_size: 50`
- `Bash("npm test")` → `tool_name: "Bash"`, `action_target: "npm test"`, `success: false`, `error_type: "exit_code_1"`, `duration_ms: 3200`
- `Edit("/src/utils.py")` → `tool_name: "Edit"`, `action_target: "/src/utils.py"`, `action_metadata: {line_range: "42-45"}`

此粒度足以支撐：pattern detection（重複讀同一檔案、反覆失敗的 command）、span analysis（investigation → implementation → verification 行為序列）、cost attribution（每個 action 的 token/latency）。

**Response**：存 token_count + has_code_block + stop_reason，不存完整回應文字。

#### 3.7.5 Events Table 設計

採用 **單一 events 表 + JSON data column**：所有 event types 存在同一張表，共用欄位直接做 column，type-specific 欄位放 JSON。

```sql
CREATE TABLE events (
    id               TEXT PRIMARY KEY,
    session_id       TEXT NOT NULL,
    project_id       TEXT NOT NULL,
    event_type       TEXT NOT NULL,
    -- 'session_start' | 'session_end' | 'user_prompt' | 'thinking' |
    -- 'tool_use_start' | 'tool_use_end' | 'sub_agent_start' | 'sub_agent_end' |
    -- 'task_created' | 'task_completed' | 'response'
    timestamp        DATETIME NOT NULL,
    sequence_number  INTEGER NOT NULL,
    segment_index    INTEGER NOT NULL,     -- 屬於第幾個 segment（ingest 時計算）

    -- Sub-agent nesting
    sub_agent_id     TEXT,                 -- NULL = main agent，有值 = 在哪個 sub agent 內
    depth            INTEGER DEFAULT 0,    -- 0=main, 1=sub, 2=sub-sub

    -- 共用欄位（多數 event type 都有）
    duration_ms      INTEGER,
    token_count      INTEGER,

    -- Type-specific
    data             TEXT NOT NULL,         -- JSON：各 type 的專屬欄位

    UNIQUE(session_id, sequence_number)
);

CREATE INDEX idx_events_session_seq ON events(session_id, sequence_number);
CREATE INDEX idx_events_segment ON events(session_id, segment_index);
CREATE INDEX idx_events_type ON events(session_id, event_type);
CREATE INDEX idx_events_sub_agent ON events(session_id, sub_agent_id);
```

**設計理由**：

- 單一表：analysis 的主要查詢是「拿出一個 session 的所有 events 按時間排序」，單一表一個 query 搞定，不需 UNION ALL
- JSON data column：type-specific 欄位差異大，用 JSON 保持彈性；需要時用 SQLite `json_extract()` 查詢
- 共用欄位拉出來：`duration_ms`、`token_count` 可直接 SQL aggregate
- `segment_index`：ingest 時計算（每遇到 `user_prompt` event，index +1），analysis 不需重新切分
- `sub_agent_id` + `depth`：追蹤 sub-agent nesting，支援按 depth 做 cost attribution

#### 3.7.6 Segment Index 計算

Segment index 在 ingest 時即時計算，不需等 analysis。規則：每遇到 `user_prompt` event，segment_index +1。

由於 hook 是 stateless 的（每次觸發都是獨立 process），current segment index 需要從 DB 查詢：

```python
def next_segment_index(self, session_id: str, event_type: str) -> int:
    current_max = self.event_repo.get_max_segment_index(session_id)
    if event_type == "user_prompt":
        return (current_max or 0) + 1
    return current_max or 0
```

Hook throughput 極低（每秒 0.5-2 events），一次 `SELECT MAX(segment_index)` 的 overhead 可忽略。

#### 3.7.7 Sub-Agent Nesting 追蹤

Sub-agent events 使用 `sub_agent_id` 和 `depth` column 標記 nesting 層級：

```
seq=1   thinking          sub_agent_id=NULL    depth=0   ← main agent
seq=2   tool_use_start    sub_agent_id=NULL    depth=0   ← main agent 呼叫 Task
seq=3   sub_agent_start   sub_agent_id=NULL    depth=0   ← boundary event
seq=4   thinking          sub_agent_id="sa_001" depth=1  ← sub agent 內部
seq=5   tool_use_start    sub_agent_id="sa_001" depth=1
seq=6   tool_use_end      sub_agent_id="sa_001" depth=1
seq=7   sub_agent_end     sub_agent_id=NULL    depth=0   ← boundary event
seq=8   tool_use_end      sub_agent_id=NULL    depth=0   ← 回到 main agent
```

Observation tracker 維護 nesting state（agent stack），在 `sub_agent_start` 時 push、`sub_agent_end` 時 pop。

Edge case：如果 session 被強制中斷，最後一個 `sub_agent_start` 可能沒有對應的 `sub_agent_end`（orphan start），analysis 層需能 handle。同理，`tool_use_start` 沒有對應的 `tool_use_end` 也需處理。

#### 3.7.8 Session Metadata（每個事件都帶）

| 欄位 | 說明 |
|------|------|
| `session_id` | SecondSight 管理的 session identifier |
| `timestamp` | 事件發生時間 |
| `sequence_number` | 事件在 session 內的序號 |
| `segment_index` | 事件所屬的 segment（ingest 時計算） |
| `sub_agent_id` | 所屬 sub-agent（NULL = main agent） |
| `depth` | Nesting 深度（0 = main agent） |
| `project_id` | 所屬 project |

`external_session_ref`（agent 原生 session ID）和 `agent_type` 存在 `session_start` event 的 data JSON 中，不在每個 event 重複。

### 3.8 Session 識別策略

以 Claude Code 為例，一個 project 下有多個 session 檔案。SecondSight 需要知道當前 hook 事件屬於哪個 session。

**設計決策**：SecondSight 管理自己的 `session_id`，同時保留 `external_session_ref` 欄位關聯回 agent 原生的 session。

**識別策略**（依 Phase 0 調查結果選擇）：

1. **Hook context 直接提供**：如果 agent hook payload 帶 session identifier，直接作為 `external_session_ref` 使用
2. **Process-based 推導**：hook 執行時取 parent process PID，對應到當前 active session
3. **Session lifecycle hook**：在 session 開始時透過 session start event 建立 SecondSight 的 `session_id`，後續事件掛在此 ID 下

**Phase 0 調查項**：P0-1（Claude Code hook 機制調查）需確認 hook 觸發時能拿到哪些 session context。

### 3.9 Observation Data Flow Pipeline

Hook 觸發到資料落地的完整流程。Hook script 是 **thin HTTP client**，所有重邏輯都在 API server 端處理：

```
Hook script（thin client）                    API Server（常駐）
─────────────────────                        ──────────────────
hook 觸發
  → HTTP POST localhost:8420/hook/{type}     → 收到 request
     body: raw_payload                       → Adapter.normalize()
                                             → SessionTracker (segment_index, nesting)
  ← 收到 response (empty)                    ← 回傳 OK
  → hook 結束，不阻塞 agent                   → [Async] filesystem write + DB INSERT
```

```python
# api/hooks.py (server side)
@router.post("/hook/pre-tool-use")
async def handle_pre_tool_use(payload: HookPayload):
    # 1. Normalize
    event = adapter.normalize("PreToolUse", payload.dict())

    # 2. Session tracker (in-memory state)
    event.segment_index = tracker.next_segment_index(event.session_id, event.event_type)
    event.sub_agent_id = tracker.current_sub_agent_id
    event.depth = tracker.current_depth

    # 3. Observation 寫入 — async, 不阻塞 response
    asyncio.create_task(observation_pipeline.ingest(event))

    return {"status": "ok"}
```

```python
# observation/pipeline.py (async)
class ObservationPipeline:
    async def ingest(self, event: Event):
        # Filesystem: source of truth
        await self.raw_trace_store.write(event)

        # DB: derived index (best-effort)
        try:
            await self.event_repo.insert(event)
        except Exception as e:
            self.sync_log.record_failure(event.id, error=str(e))
```

**Hook script（thin client）範例**：

```bash
#!/bin/bash
# ~/.claude/hooks/pre-tool-use.sh
curl -s -X POST "http://localhost:8420/hook/pre-tool-use" \
  -H "Content-Type: application/json" \
  -d "$HOOK_PAYLOAD"
```

#### 3.9.1 Hook Latency 分析

Claude Code 的 PreToolUse hook 是 **同步的**——agent 執行 tool 之前會先跑 hook script，等 hook 回傳後才真正執行 tool。

採用 API server 模式後，hook 只負責收集觀測資料（純寫入），不回傳任何 feedback。Latency 組成：

```
Hook script 啟動（bash/curl）     ~5ms
HTTP request → localhost         ~1ms
HTTP response                    ~1ms
─────────────────────────────────
Total                            ~7ms（不含 observation 寫入，因為 async）
```

Observation 的 filesystem write + DB INSERT 在 server 端 async 執行，不在 hook 的 critical path 上。

#### 3.9.2 Fallback：Server 未啟動時

如果 API server 沒有在跑（user 忘記啟動、server crash），hook script 需要 graceful fallback：

```bash
#!/bin/bash
# Hook script with fallback
RESPONSE=$(curl -s --connect-timeout 0.1 -X POST "http://localhost:8420/hook/pre-tool-use" \
  -H "Content-Type: application/json" \
  -d "$HOOK_PAYLOAD" 2>/dev/null)

if [ $? -ne 0 ]; then
  # Server 沒起來 → 直接寫 filesystem 作為 fallback
  echo "$HOOK_PAYLOAD" >> ~/.secondsight/fallback_events.jsonl
  exit 0  # 不阻塞 agent，observation 資料先存 fallback file
fi

echo "$RESPONSE"
```

Fallback 策略：將 raw event 寫入 fallback file，下次 analysis 時 backfill。使用者不會因為 server 沒跑就丟失 observation 資料。

#### 3.9.2 Error Handling：Backfill 機制

DB INSERT 失敗時，raw trace 已安全落地在 filesystem。失敗會被記錄（sync log），在 analysis 啟動時自動 backfill：

```python
# analysis/orchestrator.py
class AnalysisOrchestrator:
    def analyze_session(self, session_id: str):
        # Step 0: Backfill — 從 filesystem 補同步尚未入庫的 events
        self.backfill_service.sync_session(session_id)

        # Step 1: 從 DB 拿所有 events（此時已完整）
        events = self.event_repo.get_session_events(session_id)
        ...
```

Backfill 流程：掃描 session 目錄下的 event JSON files，比對 DB 中已有的 events（by id），INSERT 缺少的。也可透過 `secondsight sync` CLI 手動觸發全量重建。

**Analysis 從 DB 讀取 session 內容**：

```python
# Analysis 觸發時的查詢流程
events = event_repo.get_session_events(session_id)
# → SELECT * FROM events WHERE session_id = ? ORDER BY sequence_number
# → 回傳 list[Event] (Pydantic models)

# Segment 已由 segment_index 標記，不需重新切分
segments = event_repo.get_segments(session_id)
# → SELECT DISTINCT segment_index FROM events WHERE session_id = ?

# 按 segment 取 events
segment_events = event_repo.get_segment_events(session_id, segment_index=2)
# → SELECT * FROM events WHERE session_id = ? AND segment_index = 2 ORDER BY sequence_number

# Tool use pairing (analysis 層處理)
# tool_use_start 後面最近的 tool_use_end 就是它的 pair
# Edge case: orphan start (session 被 kill) → success=None
```

### 3.10 Data Retention & Cleanup

長期使用下，每個 project 會累積大量 session 資料。Data retention 採用 **TTL（Time-To-Live）機制**，支援 global 預設值和 per-project override。

#### 3.10.1 TTL 設定

```toml
# ~/.secondsight/config.toml

[retention]
raw_traces_ttl_days = 90          # Raw trace files + DB events 的預設保留天數
analysis_ttl_days = 365           # Analysis results 保留更久（量小、重新生成需 LLM tokens）
```

```toml
# ~/.secondsight/projects/{project_id}/config.toml

[retention]
raw_traces_ttl_days = 180         # 此 project 的 raw traces 保留更久
```

#### 3.10.2 清理範圍與策略

| 資料類型 | 預設 TTL | 清理理由 |
|---------|---------|---------|
| Raw trace files（filesystem JSON） | 90 天 | 量最大，analysis 完成後主要價值是 re-analysis 和 user 檢視 |
| DB events 記錄 | 90 天（同 raw traces） | 與 filesystem 同步清理 |
| Analysis results（DB + filesystem JSON） | 365 天 | 量小，但 re-generate 需花 LLM tokens |
| Behavior Flags | 365 天（同 analysis results） | 量小，為 cross-session 統計的基礎 |
| Directives | 不自動清理 | 由 lifecycle 機制管理（obsolete / expired / superseded） |

**不立即刪除 raw data**：analysis 完成後不刪除 raw traces，因為：使用者可能想回看某個 session 的細節、可能想以新版 prompt 或更強的 model re-analyze。

#### 3.10.3 清理時機

Cleanup 在 **session end 後的 analysis 完成後**觸發（搭便車，不額外啟動 process）：

```
Session end → Analysis 啟動
  → Backfill sync（補同步缺失的 events）
  → Per-segment LLM analysis
  → Session-level summary
  → Directive generation
  → Cleanup check：掃描 expired sessions，刪除超過 TTL 的資料
```

也可透過 `secondsight cleanup` CLI 手動觸發。

---

## 四、Agent Adapter 架構

### 4.1 Event-Driven 整合（hook + file watch）

以各 coding agent 的 event-driven 機制作為主要整合方式，具體實作分兩條路徑：

- **Subprocess hook 路徑**（Claude Code、Codex）：hook script 是 **thin HTTP client**（bash + curl），所有重邏輯在 API server 端處理。Hook latency ~8ms，不影響 agent 的使用體感
- **DB watch 路徑**（OpenCode）：對 `~/.local/share/opencode/opencode.db` 做 fs-event watching + 唯讀 SQLite 增量讀取（不採用 OpenCode 官方 Bun plugin，詳見 ADR-014）

**Fallback**：
- Subprocess hook 路徑：API server 未啟動時 hook script graceful skip（將 raw event 寫入 fallback file，後續 backfill）
- DB watch 路徑：fs-event 事件丟失時由低頻 polling 兜底（hybrid mode）

### 4.2 Adapter Interface

所有 adapter 實作統一的 abstract interface。Adapter 是 cross-cutting layer，同時處理 observation（hook event capture）和 feedback（convention injection）：

```python
class AgentAdapter(ABC):
    @abstractmethod
    def normalize(self, hook_type: str, raw_payload: dict) -> Event:
        """將 agent-specific hook payload 轉換為 SecondSight Event"""
        ...

    @abstractmethod
    def inject_convention(self, conventions: list[Convention]) -> str:
        """將 conventions 格式化為該 agent 的 SessionStart hook output"""
        ...

    @abstractmethod
    def inject_hint(self, hint: "Hint") -> str:
        """[Reserved] 將 hint 格式化為該 agent 的 PreToolUse hook output
        Phase 0 不實作，保留介面供日後擴展"""
        ...

    @abstractmethod
    def supported_event_types(self) -> set[str]:
        """回傳此 adapter 支援的 event types（不同 agent 支援度不同）"""
        ...
```

### 4.3 支援範圍與跨 Agent 差異

SecondSight 的 Event Model 是自己的抽象層，不是任何特定 agent 的 1:1 映射。不同 agent 支援的 event types 不同：

| Agent | 優先度 | Hook 機制 | 已知支援 | 待 Phase 0 調查 |
|-------|--------|-----------|---------|-----------------|
| Claude Code | P0 | Hook lifecycle（完整） | session、prompt、tool use、sub-agent、task、response | hook payload 格式、session context |
| Codex | P1 | 待調查 | 待確認 | 是否有 hook、支援哪些 event types、sub-agent / task 機制 |
| OpenCode | P1 | DB watch（fs-event + 唯讀 SQLite 增量讀取，不採用官方 Bun plugin——見 ADR-014） | session、tool calls、token usage（per-message）、sub-agent 親子關係（透過 `parent_id` 欄位） | Injection 路徑驗證（config 系統 prompt **UNVERIFIED**） |

**Analysis 層的 graceful degradation**：如果某 agent 不支援特定 event type（例如 Codex 沒有 task events），analysis 在缺少該 event 時仍能正常運作，只是少了對應的分析維度。

---

## 五、Analysis & Feedback Agent

### 5.1 兩種分析模式

詳見 2.2 節。Analysis Agent 的執行方式取決於觸發模式：

- **CLI 模式**：Coding agent（Claude Code / Codex 等）提供 agentic loop + file access，SecondSight 提供 analysis prompts + tools + project folder access
- **SDK 模式**：SecondSight 基於 PydanticAI 提供 reference agent loop，開發者可直接使用或只取 building blocks 自行組裝

### 5.2 Agent Framework：PydanticAI

選用 PydanticAI 作為 SDK 模式的 agent framework，理由：

- **輕量**：不像 Google ADK 是 heavy orchestration framework，PydanticAI 專注於 agent loop + tool calling
- **Pydantic-native**：與 SecondSight 的 tech stack（Pydantic v2、FastAPI）天然整合
- **Multi-provider**：原生支援 OpenAI-compatible endpoints，涵蓋大部分 provider；特殊 provider 可透過 LiteLLM 作為 provider fallback 掛載
- **設計自由度高**：不強加框架設計觀，適合 SecondSight 高度特化的分析邏輯

**Provider 策略**：
- 主要路徑：PydanticAI 原生 OpenAI-compatible provider（覆蓋 Anthropic、OpenAI、Google、Mistral 等）
- Fallback 路徑：透過 LiteLLM 作為 PydanticAI 的 provider，處理特殊或少見的 provider

### 5.3 兩層分析架構

Analysis 的本質是 **行為回顧**——像 senior engineer review junior 的做事過程，不是 review code 品質，而是 review **做事方式是否有效率**。整個分析分為兩層：

#### 5.3.1 第一層：Session-Level 行為分析

分析單一 session 的完整事件序列，產出該 session 的行為報告。以 **user prompt 為單位切分 segment**，每個 segment 獨立分析 prompt-action 對齊度。

Agent 的每個 action 都與 user prompt 相關，脫離 prompt context 的統計分析（如單純計算「讀了幾次同一檔案」）無法判斷行為是否合理。由於 context engineering 的特性，agent 在不同 prompt 下可能合理地重複讀取同一檔案。靜態 metrics 無法區分這種情況，只有 LLM 在理解 prompt intent 的前提下才能做出正確判斷。

**分析目標**：

- **意圖與行為的落差**：User prompt 表達了什麼意圖？Agent 的實際行為路徑跟這個意圖之間有沒有落差？有多少步驟是不必要的？
- **行為效率**：同樣的目標，有沒有更短的路徑可以達成？Agent 是不是在做重複或冗餘的操作？
- **行為分類標記**：每個被判定為低效的操作，標記其行為類型（Behavior Flag）

**Behavior Flag 類型定義**：

| Flag Type | 說明 | 範例 |
|-----------|------|------|
| `unnecessary_read` | 讀了跟任務無關的檔案 | User 指定 a.py，agent 先讀 README.md |
| `redundant_exploration` | 已經有足夠資訊還在探索 | User 給了明確路徑，agent 還在 ls / grep |
| `missed_shortcut` | 有更直接的路徑但沒走 | User 給了檔名卻還在用 grep 搜尋 |
| `repeated_operation` | 重複做同樣的操作 | 在同一 segment 讀了同一個檔案兩次 |
| `wrong_tool_choice` | 用了不適合的工具 | 該用 grep 卻逐個 read 檔案找內容 |
| `excessive_context_gathering` | 收集了過多不需要的 context | 簡單 edit 任務卻讀了十幾個不相關的檔案 |

每個 flag 帶上：涉及的事件 ID、當時的 user prompt 意圖摘要、為什麼判定為低效。這些在 dashboard 上可展示為一個 session 的行為回顧。

**Segment 結構**：

```
Session
├── Segment 1：user_prompt_1 + [event_1, event_2, ..., event_N]
├── Segment 2：user_prompt_2 + [event_N+1, ..., event_M]
└── Segment 3：user_prompt_3 + [event_M+1, ..., event_K]
```

**Segment 範例**：

```
Segment：「幫我修 src/utils.py 的 bug」

  thinking  (2000 tokens, 3s)
  tool_use  Read("/src/utils.py", lines=1-100)        ✓ 200ms
  thinking  (1500 tokens, 2s)
  tool_use  Read("/src/tests/test_utils.py")           ✓ 150ms
  thinking  (3000 tokens, 4s)
  tool_use  Read("/src/config.yaml")                   ✓ 120ms   ← Flag: unnecessary_read
  thinking  (800 tokens, 1s)
  tool_use  Edit("/src/utils.py", line_range=42-45)    ✓ 100ms
  thinking  (500 tokens, 1s)
  tool_use  Bash("pytest tests/test_utils.py")         ✓ 2.1s
  response  (300 tokens, has_code_block=true)

Behavior Flags:
  - unnecessary_read: Read("/src/config.yaml")
    intent: "修 utils.py 的 bug"
    reason: "config.yaml 與 bug fix 無關，agent 在收集不必要的 context"
```

**分析流程**：

1. **Segment 切分**（自動，不用 LLM）：依 user prompt 邊界將 session trace 切成 segments
2. **Supplementary metrics 計算**（自動，不用 LLM）：為每個 segment 計算輔助指標（total tokens、unique files、duration、error count），作為 LLM 分析時的 supplementary context
3. **LLM 行為分析**（per segment）：將 user prompt + event 序列 + supplementary metrics 送給 LLM，判斷每個 event 在此 prompt context 下是否合理，產出 behavior flags
4. **按需讀取 project 檔案**：LLM 判斷不確定時，透過 `read_project_file` 讀取實際檔案內容驗證
5. **Session Report 彙總**：所有 segments 的 behavior flags 彙總為 session-level 的行為報告

由於 observation layer 只存 action + target（不存 content），trace 資料本身非常輕量。一個 200 tool calls 的 session 可能只需 5000-8000 tokens，用 Haiku 級別的 model 分析成本極低。

#### 5.3.2 第二層：Cross-Session 彙整

每次新的 session 分析完成後，立即重新統計所有歷史 session 的 behavior flags，歸納出反覆出現的行為模式，取 **top N** 作為 active conventions。

**彙整流程**：

1. **統計所有歷史 behavior flags**：按 flag type + 語意相似度 歸類，計算各行為模式的出現頻率
2. **排序**：按出現頻率排序，頻率高代表 agent 確實有這個低效習慣
3. **產出 Convention**：將 top N 行為模式轉換為自然語言的行為準則（convention）
4. **動態更新**：隨著 agent 改善行為，某些模式的出現頻率下降，自然被擠出 top N

Convention 的品質來自統計基礎——不是單次觀察就產出建議，而是多個 session 反覆出現的模式才會成為 convention。

**彙整範例**：

```
Cross-Session 統計（最近 20 sessions）：
  1. unnecessary_read     出現 15 次（75%）→ Convention: "當 user 指定目標檔案時，直接操作，跳過探索步驟"
  2. redundant_exploration 出現 12 次（60%）→ Convention: "已有足夠資訊時，避免額外的 ls / grep 探索"
  3. wrong_tool_choice     出現 5 次（25%） → 暫不產出 convention（頻率偏低）
```

#### 5.3.3 長 Segment 處理

如果單一 user prompt 觸發了大量 actions（例如 100+ tool calls），可進一步切成 span：

- **Investigation span**：連續的讀取 / 搜尋操作
- **Implementation span**：連續的寫入 / 修改操作
- **Verification span**：連續的測試 / 驗證操作

但核心判斷基準始終是 user prompt——span 只是組織方式，不改變分析邏輯。

### 5.4 Analysis Agent Tools

Analysis Agent 在執行分析時可使用的 tools：

```python
class AnalysisTools:
    def read_traces(self, session_id: str) -> list[TraceEvent]:
        """從 raw trace store 讀取 session 的事件序列"""

    def read_project_file(self, project_id: str, file_path: str) -> str:
        """按需讀取 project 中的實際檔案內容（用於驗證 agent 行為是否合理）"""

    def query_structured_store(self, query: StructuredQuery) -> list[dict]:
        """查詢 intelligence.db（歷史分析、behavior flags、conventions 等）"""

    def read_historical_flags(self, project_id: str) -> list[BehaviorFlagSummary]:
        """讀取歷史 session 的 behavior flags 統計（cross-session 彙整用）"""
```

**按需讀取 project 檔案的場景**：當 LLM 從 trace 序列無法確定某個 action 是否合理時（如 agent 讀了 `config.yaml`，但不確定跟任務是否相關），可透過 `read_project_file` 讀取實際檔案驗證。此設計的前提：SecondSight 是 local-first，analysis agent 跟 project 在同一台機器上。

### 5.5 Analysis Prompt Architecture

Analysis 的 prompt 設計遵循「底層專注做好一件事」原則，每層 prompt 有明確的輸入格式、任務定義、輸出格式。

#### 5.5.1 BehaviorFlagType 定義（Source of Truth）

Flag type 在 code 層級定義（`schemas/analysis.py`），作為 single source of truth。Prompt 組裝時從此定義動態生成 flag type 說明段落，確保 LLM 只會產出合法的 flag type。

```python
class BehaviorFlagType(str, Enum):
    UNNECESSARY_READ = "unnecessary_read"
    REDUNDANT_EXPLORATION = "redundant_exploration"
    MISSED_SHORTCUT = "missed_shortcut"
    REPEATED_OPERATION = "repeated_operation"
    WRONG_TOOL_CHOICE = "wrong_tool_choice"
    EXCESSIVE_CONTEXT_GATHERING = "excessive_context_gathering"

FLAG_DEFINITIONS: dict[BehaviorFlagType, FlagDefinition] = {
    BehaviorFlagType.UNNECESSARY_READ: {
        "description": "讀了跟當前任務意圖無關的檔案",
        "criteria": "該檔案的內容與 user prompt 的意圖無直接關聯",
        "example": "User 要求修改 a.py，agent 先讀了 README.md"
    },
    BehaviorFlagType.REDUNDANT_EXPLORATION: {
        "description": "已經有足夠資訊完成任務，仍在做額外探索",
        "criteria": "agent 已具備完成任務所需的資訊，卻繼續 ls / grep / read 不相關的路徑",
        "example": "User 給了明確路徑，agent 還在 ls 整個目錄結構"
    },
    BehaviorFlagType.MISSED_SHORTCUT: {
        "description": "有更直接的路徑可達成目標但沒走",
        "criteria": "存在更短的操作路徑，agent 選了迂迴的方式",
        "example": "User 給了檔名，agent 卻用 grep 搜尋整個 codebase 才找到"
    },
    BehaviorFlagType.REPEATED_OPERATION: {
        "description": "在同一 segment 內重複做同樣的操作",
        "criteria": "相同的 tool + target 組合在同一 segment 出現多次且無合理原因",
        "example": "同一個 segment 內讀了同一個檔案兩次"
    },
    BehaviorFlagType.WRONG_TOOL_CHOICE: {
        "description": "使用了不適合當前任務的工具",
        "criteria": "存在更適合的工具但 agent 選了效率較低的替代方案",
        "example": "該用 grep 搜尋關鍵字，卻逐個 read 檔案找內容"
    },
    BehaviorFlagType.EXCESSIVE_CONTEXT_GATHERING: {
        "description": "任務規模不需要大量 context，agent 卻收集了過多資訊",
        "criteria": "簡單任務（如單檔 edit）卻讀了大量不相關的檔案建立 context",
        "example": "簡單 bug fix 卻讀了十幾個不相關的檔案"
    },
}
```

#### 5.5.2 Segment-Level Analysis Prompt（第一層）

單一 prompt 處理一個 segment，產出該 segment 的行為標記。Prompt 結構：

```
[System]
你是 coding agent 行為分析專家。你的任務是分析 agent 在回應 user prompt 時的操作效率。

[Schema 說明]
以下是 segment 資料的 field 定義：
- user_prompt: agent 收到的使用者指令，是判斷所有操作是否必要的基準
- events: 按時間排序的事件序列
  - thinking: agent 的推理步驟（token_count 反映推理深度，duration_ms 反映推理時間）
  - tool_use_start: 工具操作開始（tool_name, target, metadata）
  - tool_use_end: 工具操作結束（tool_name, target, success, duration_ms）
  - sub_agent_start/end: 子 agent 呼叫的開始與結束
  - response: agent 回覆使用者（token_count, has_code_block）
- supplementary_metrics: 輔助統計，僅供參考，不作為獨立判斷依據

[Flag Type 定義]
以下是所有合法的 behavior flag 類型，你只能使用這些類型：
{動態生成自 FLAG_DEFINITIONS}

[任務]
分析此 segment 中每個 event 是否為達成 user prompt 意圖的必要操作。
對不必要或低效的操作標記 behavior flag。
注意：只有你確信該操作不必要時才標記，不確定時不標記。

[Segment Data]
{ segment JSON }

[Output Format]
回傳 JSON：
{
  "segment_summary": "對此 segment agent 整體表現的一句話評價",
  "flags": [
    {
      "flag_type": "必須是上述定義的合法類型",
      "event_ids": ["涉及的事件 ID"],
      "reason": "為什麼判定為低效（一句話）",
      "confidence": "high | medium | low — LLM 對此 flag 判定的信心度"
    }
  ],
  "total_events": number,
  "flagged_events": number
}
```

`confidence` 由 LLM 自行判定。Orchestrator 可選擇丟棄低信心 flag
以降低 false-positive；schema 層不過濾。`schemas/analysis.py`
（`secondsight.analysis.schemas.BehaviorFlag`）為唯一 source of truth，
LLM 輸出由該 Pydantic 模型驗證。

#### 5.5.3 Cross-Session Aggregation Prompt（第二層）

彙整流程分三步，只有 Step 2 使用 LLM：

**Step 1（自動化）**：按 flag_type 分組，每組收集所有 flags 的 segment_summary + reason

**Step 2（per flag_type LLM call）**：每組送一次 LLM，做語意歸類 + convention 產出

```
[System]
你是 coding agent 行為模式分析專家。你的任務是從多個 session 的行為標記中歸納出行為模式，並產出行為準則。

[任務]
以下是多個 session 中被標記為 "{flag_type}" 的行為記錄。
請：
1. 依據 segment_summary 和 reason 做語意歸類，辨識出不同的行為模式
   （同一 flag_type 下可能有多種不同的行為模式）
2. 統計每個行為模式的出現次數
3. 為每個行為模式產出一條自然語言的行為準則（convention）
   - Convention 必須精煉（2-5 句，≤ 200 tokens）
   - Convention 必須是可操作的指導，不是抽象原則

[Behavior Flags]
{ 同一 flag_type 下的所有 flags: [{session_id, segment_summary, reason}, ...] }

[Output Format]
回傳 JSON：
{
  "patterns": [
    {
      "pattern_description": "此行為模式的描述",
      "occurrence_count": number,
      "representative_sessions": ["貢獻此模式的 session IDs"],
      "convention": "產出的行為準則文字"
    }
  ]
}
```

**Step 3（自動化）**：合併所有 flag_type 的 patterns，按 occurrence_count 排序，取 top N（config: `convention_top_n`）作為 active conventions，寫入 directives table。

### 5.6 Analysis 執行時機

**Analysis 只在 session 結束後觸發，不在 session 進行中執行。** Session 進行中 SecondSight 只做 observation（記錄事件），不做任何分析或即時介入。

**觸發方式**：

- **正常結束**：Coding agent 的 session end event 觸發（透過 session lifecycle hook）
- **異常結束 fallback**：如果使用者直接關閉 terminal 或 kill process，沒有乾淨的 session end event，則以 timeout-based 偵測作為 fallback——超過 N 分鐘沒有新事件，視為 session 已結束
- **手動觸發**：使用者可透過 `secondsight analyze` 手動觸發指定 session 的分析

**Background 執行**：Analysis 在 background 進行，不阻塞使用者。第一層（session 行為分析）完成後，立即執行第二層（cross-session 彙整），更新 active conventions。分析結果寫入 intelligence.db + filesystem JSON 備份後，下次 session start 時 conventions 自動可用。

### 5.7 Analysis Model 設定

#### 5.7.1 Default 策略：最輕量 Model

Analysis 預設使用最輕量、最快速的 model，降低使用者成本。預設選擇使用者所使用的 coding agent 體系中最 lite 的 model。

| Agent 體系 | Default Model | 理由 |
|-----------|---------------|------|
| Claude Code | `claude-haiku-4-5-20251001` | Anthropic 體系最輕量，成本低速度快 |
| Codex | 待 Phase 0 調查 | 需確認 Codex 支援的 models，選最 lite 版本 |
| OpenCode | 需 user 自行設定 | 支援的 provider/model 組合太多，無法預設 |

#### 5.7.2 設定結構

```toml
# ~/.secondsight/config.toml（全域設定）

[analysis]
default_agent = "auto"    # auto = 從 observation 記錄推導最常用的 agent_type

[analysis.models]
claude_code = "claude-haiku-4-5-20251001"
codex = "auto"            # 待 Phase 0 確認
opencode = ""             # 需 user 自行設定

[analysis.models.fallback]
# LLM Router：primary model 不可用時，依序嘗試 fallback models
# 適用 SDK 模式；CLI 模式借用 coding agent 本身的 model，不需 fallback
fallback_models = ["gpt-4o-mini", "gemini-2.0-flash"]
```

```toml
# ~/.secondsight/projects/{project_id}/config.toml（per-project override）

[analysis]
model = "claude-sonnet-4-6"    # 此 project 分析品質需求較高，升級到 Sonnet
```

#### 5.7.4 LLM Router（SDK 模式）

SDK 模式下，LLM API call 失敗時自動 fallback：

```
Primary model（config 設定）
  → 失敗（timeout / rate limit / API error）
  → Fallback model 1
  → 失敗
  → Fallback model 2
  → 全部失敗 → 記錄錯誤，該 session 的 analysis 標記為 failed，可之後手動 re-analyze
```

LiteLLM 原生支援 fallback routing，PydanticAI 可透過 LiteLLM provider 掛載使用。CLI 模式不需要 LLM router，因為借用的是 coding agent 本身的 model 和 API key。

#### 5.7.3 Agent 選擇邏輯

CLI 模式下，`secondsight analyze` 預設使用與 user 相同的 coding agent 來執行分析，確保一定有可用的 application。Agent 選擇可從 observation 記錄中的 `agent_type` 欄位推導。使用者可在設定中 override。SDK mode 是 opt-in，適合需要客製化 provider/model 的開發者。

### 5.8 分析結果大小限制

Feedback 內容必須精簡，過多的 conventions 會增加 inject tokens、稀釋 agent 注意力、反而降低效果。

#### 5.8.1 單條 Directive 大小限制

| 類型 | 大小限制 | 說明 |
|------|----------|------|
| Convention | 2-5 句，≤ 200 tokens | SessionStart 注入，必須精煉 |
| Hint（reserved） | 1-3 句，≤ 100 tokens | 預留欄位，Phase 0 不實作 |

#### 5.8.2 Per-Project Active Directive 上限

| 類型 | 上限 | 說明 |
|------|------|------|
| Active Conventions | ≤ 15 條 | 超過則按出現頻率 + effectiveness 排序淘汰 |
| Active Hints（reserved） | ≤ 30 條 | 預留欄位，Phase 0 不實作 |

#### 5.8.3 注入 Token Budget

SessionStart 注入所有 active conventions 的總 tokens 上限為 **2000 tokens**。超過時按出現頻率排序，只取最重要的 conventions 直到 budget 用完。

### 5.9 Directive Lifecycle（Iterative 優化）

Conventions 不是建立後永遠存在。隨著 agent 行為改善、LLM 能力提升、或 user 習慣改變，某些問題會自然解決，對應的 conventions 需要被淘汰。由於 conventions 來自 cross-session 統計，lifecycle 天然跟統計數據連動。

#### 5.9.1 Lifecycle 狀態

```
Convention Lifecycle
├── created → active（cross-session 統計進入 top N，開始注入）
├── active → effective（追蹤到 agent 行為改善，頻率下降但仍在 top N）
├── active → obsolete（對應 behavior flag 頻率持續下降，被擠出 top N）
├── active → superseded（新一輪彙整產出更精確的 convention → 取代舊的）
├── active → expired（超過 TTL → 強制重新評估）
└── obsolete → re-activated（pattern 頻率又回升，重新進入 top N）
```

#### 5.9.2 新增機制

每次 cross-session 彙整可能產出新的 conventions。新增前先比對現有 active conventions：

- **語意重複**：如果新 convention 跟現有的語意相似，不新增（合併計數）
- **語意相似但更精確**：新 convention supersede（取代）舊的
- **全新 pattern**：直接新增為 active

#### 5.9.3 淘汰機制

Conventions 的淘汰主要由統計數據驅動：

| 條件 | 說明 | 動作 |
|------|------|------|
| **頻率下降** | 對應 behavior flag 在近 N 個 session 的出現頻率降到 top N 之外 | 被擠出 active list → obsolete |
| **過期** | Convention 超過 TTL（預設可設定，例如 30 天） | 狀態改為 expired，需重新評估 |
| **被取代** | 新彙整產出更好的 convention | 舊 convention 狀態改為 superseded |

**Re-activation**：如果一個 obsolete convention 對應的 behavior flag 頻率又回升到 top N，自動重新啟用。

#### 5.9.4 Effectiveness 追蹤

每個 convention 追蹤其效果：

- **行為改變率**：convention 注入後，對應 behavior flag 的出現頻率是否下降
- **如果無效**：經過 N 個 session 仍無改善，後續 cross-session 彙整可嘗試調整 phrasing 產出新版本取代

---

## 六、Feedback 機制

### 6.1 設計原則：不寫 CLAUDE.md

SecondSight **不會**將 feedback 寫入 CLAUDE.md 或其他 agent 的 project config 檔案，理由：

- CLAUDE.md 只適用於 Claude Code，不具跨 agent 通用性
- 研究指出 CLAUDE.md 適合提供 project map 類的知識脈絡，LLM 生成的規範類指令反而會降低 performance
- CLAUDE.md 是使用者自己維護的，SecondSight 不應自動修改，避免 ownership 混亂

### 6.2 Feedback 架構：Convention 為主，Hint 保留彈性

Feedback 的核心機制是 **Convention**——來自 cross-session 統計的行為準則，注入在 system prompt 中。

| 類型 | 角色 | 注入方式 | Phase 0 實作 |
|------|------|----------|-------------|
| **Convention** | 跨 session 的行為準則，來自統計 top N | SessionStart → system prompt | ✓ |
| **Hint（reserved）** | Convention 的即時再提醒 | PreToolUse → context injection | ✗（保留設計彈性） |

**Convention 與 Hint 的關係**：Hint 不是獨立的內容，而是 convention 的「即時提醒機制」。Convention 已在 system prompt 告訴 agent 規則，但 agent 不一定遵守。Hint 是在 PreToolUse 時觀察到 agent 又在做同樣的錯誤行為，再提醒一次對應的 convention。就像 senior engineer 已經在 team guideline 寫了規則，但看到 junior 又要犯同樣的錯，在動手前口頭提醒一次。

**Phase 0 不實作 Hint 的原因**：Hint 的觸發需要理解 user prompt 意圖與當前 action 的關係，不是單純的 tool_name + target pattern match 能做到的，可能需要輕量 LLM 判斷。先做好 convention 注入，觀察效果後再決定是否需要 hint 機制。

### 6.3 Convention 的注入方式

所有 convention 產出後 **直接自動注入**（auto），不需要 user 逐條確認。在 SessionStart hook 觸發時，自動將 active conventions inject 到 system reminder 內。

使用者可透過 dashboard 查看所有 active conventions，認為不適合的可直接刪除。

```
Convention 注入流程
└── Cross-session 彙整產出 convention → auto inject
    └── SessionStart hook → inject 到 system reminder
    └── User 可從 dashboard 刪除不適合的 convention
```

### 6.4 Hint 機制（Reserved，Phase 0 不實作）

Hint 機制保留完整的設計空間與介面定義，但 Phase 0 不實作。DB table 保留 hint 相關欄位（nullable），adapter interface 保留 `inject_hint` 方法，feedback module 保留 `hint.py`（空實作）。

**日後啟用時的概念設計**：

- Hint 的內容 = 對應的 convention 規則，不是獨立產出的內容
- 觸發時機：PreToolUse，當 agent 的當前 action 符合某個 convention 所描述的低效行為模式時觸發
- 觸發判斷：可能需要輕量 LLM 判斷（理解 user prompt 意圖 + 當前 action context），非簡單 pattern match

### 6.5 Feedback 資料流

從 analysis 到 feedback 的完整資料流：

```
Session 行為分析（第一層）
  → Behavior Flags（per-session, in intelligence.db）
    → Cross-Session 彙整（第二層）
      → 統計行為模式頻率 → top N → Active Conventions
        → Directive Store（intelligence.db, lifecycle management）
          → SessionStart hook → auto inject 到 system prompt
          → User 可從 dashboard 刪除不適合的 convention
```

### 6.6 跨 Agent 適配

不同 coding agent 的 hook 機制不同，feedback 的注入需要透過 adapter 層適配：

| Agent | SessionStart 機制 | PreToolUse 機制（reserved） | 待 Phase 0 確認 |
|-------|-------------------|----------------------------|-----------------|
| Claude Code | SessionStart hook → system reminder | PreToolUse hook → context injection | hook output 格式、latency 影響 |
| OpenCode | config 系統 prompt 欄位 或 AGENTS.md（**UNVERIFIED**——Phase 1 驗證生效時機） | 不適用（OpenCode plugin hook 限定 Bun runtime，已決議不採用——見 ADR-014） | Config 注入路徑是否真的在 session start 載入 |
| Codex | 待調查 | 待調查 | 是否支援類似 hook |

---

## 七、Analysis Results 儲存

### 7.1 儲存決策：DB 為主 + Filesystem 備份

Analysis results 的主要存取路徑是 **intelligence.db**（per-project），同時寫一份 JSON 到 filesystem 作為備份。

**DB 為主的理由**：

- Feedback 的消費端（session start convention query）需要結構化查詢
- Frontend dashboard 需要做 aggregation（behavior flag frequency、convention effectiveness）
- Cross-session 彙整需要查詢歷史 behavior flags 做統計

**Filesystem 備份的理由**：

- Analysis results 是 derived data，但重新生成需要花 LLM tokens
- DB 損壞時可從 JSON 重建，不用重跑 LLM 分析
- CLI 模式下 coding agent 可直接 `cat` 讀取 analysis result JSON

### 7.2 目錄結構（更新）

```
~/.secondsight/
  config.toml                        # Global：API keys, LLM provider settings, user preferences
  registry.db                        # Global：project index, global stats, cross-project pattern cache
  projects/
    {project_id}/
      config.toml                    # Per-project：override global settings
      intelligence.db                # Per-project：behavior flags, directives, aggregation stats
      sessions/
        {session_id}/
          events/
            {timestamp}_{event_type}.json   # Raw traces（observation layer 寫入）
          metadata.json                      # Session metadata
          session_report.json               # Session 行為報告的 filesystem 備份
```

### 7.3 Behavior Flags 在 DB 中的結構

第一層分析的產出——每個 session 的行為標記，存入 `behavior_flags` table：

```sql
CREATE TABLE behavior_flags (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    segment_index   INTEGER NOT NULL,       -- 發生在哪個 segment
    flag_type       TEXT NOT NULL,           -- 'unnecessary_read' | 'redundant_exploration' | 'missed_shortcut' | 'repeated_operation' | 'wrong_tool_choice' | 'excessive_context_gathering'
    event_ids       TEXT NOT NULL,           -- JSON array：涉及的事件 ID
    intent_summary  TEXT NOT NULL,           -- User prompt 的意圖摘要
    reason          TEXT NOT NULL,           -- 為什麼判定為低效
    created_at      DATETIME NOT NULL
);

CREATE INDEX idx_bf_project_session ON behavior_flags(project_id, session_id);
CREATE INDEX idx_bf_project_type ON behavior_flags(project_id, flag_type);
```

**關鍵查詢**：

- Dashboard 展示 session 行為回顧：`WHERE session_id = ?`
- Cross-session 統計：`WHERE project_id = ? GROUP BY flag_type`（按類型統計頻率）
- 彙整用語意歸類：`WHERE project_id = ?`（讀取所有 flags 做語意相似度比對）

### 7.4 Directive 在 DB 中的結構

Directives 在 intelligence.db 中需要支撐 feedback 機制的查詢需求。保留 `type` 欄位（`convention` | `hint`）以及 hint 相關欄位（nullable），確保日後啟用 hint 機制時不需要改 schema：

```sql
CREATE TABLE directives (
    id              TEXT PRIMARY KEY,
    project_id      TEXT NOT NULL,
    type            TEXT NOT NULL,       -- 'convention' | 'hint'（hint reserved for future）
    status          TEXT NOT NULL,       -- 'active' | 'disabled' | 'expired' | 'superseded' | 'obsolete'
    instruction     TEXT NOT NULL,       -- Agent 可讀的自然語言指令
    frequency       REAL,               -- Cross-session 統計的出現頻率（convention 用）
    trigger_pattern TEXT,                -- [Reserved] Hint 用：觸發條件
    confidence      REAL,               -- [Reserved] Hint 用：信心度
    max_firing      INTEGER,            -- [Reserved] Hint 用：per session 最多觸發幾次
    source_flag_type TEXT,              -- 來源 behavior flag type
    source_sessions TEXT,               -- JSON array：貢獻此 convention 的 session IDs
    created_at      DATETIME,
    expires_at      DATETIME,
    updated_at      DATETIME,
    disabled_at     DATETIME,            -- NULL except when status = 'disabled'
    disabled_reason TEXT                 -- NULL except when status = 'disabled'
);
```

**關鍵查詢**：

- Session start 載入 conventions：`WHERE project_id = ? AND type = 'convention' AND status = 'active' ORDER BY frequency DESC`
- Lifecycle management：update status, check expiry, handle superseding
- [Reserved] Session start 預載入 hints：`WHERE project_id = ? AND type = 'hint' AND status = 'active'`

**Soft-disable lifecycle 合約：** 若 `status` 從 `'disabled'` 轉回 `'active'`，
`disabled_at` 與 `disabled_reason` 必須清除為 NULL（避免 stale 元資料）；
`status` 從 `'disabled'` 轉到任何非 `'disabled'` 值（含 `superseded` /
`expired` / `obsolete`）也同樣清除——只有 `'disabled'` 狀態擁有這兩個欄位。
進入 `'disabled'` 狀態 MUST 提供 `disabled_reason`（audit trail 不可省略）。
HTTP `PATCH /api/directives/{id}` (GUR-104) 僅接受 `{active, disabled}`；
`expired` / `superseded` / `obsolete` 由 analyzer 設定，不對外暴露。

---

## 八、Tech Stack

### 8.1 Backend（Python）

| 類別 | 選擇 | 說明 |
|------|------|------|
| Package / Env | **uv + pyproject.toml** | 已確定 |
| Schema / Validation | **Pydantic v2** | 定義 Event Schema、Directive Schema，與 FastAPI 天然整合 |
| API Server | **FastAPI** | Async、Pydantic 原生支援，服務 frontend |
| DB Abstraction | **SQLAlchemy Core + Alembic** | 不用 ORM，保留 analytical query 的靈活性，Alembic 管 migration |
| Agent Framework | **PydanticAI** | Agent loop + tool calling，Pydantic-native |
| LLM Provider Fallback | **LiteLLM**（optional） | 特殊 provider 的 fallback，掛載為 PydanticAI 的 provider |
| CLI | **Typer + Rich** | Type-safe CLI + 美觀 terminal output |
| Testing | **pytest + pytest-asyncio** | 已確定 |
| Linting | **pre-commit + ruff** | 已確定 |

### 8.2 Frontend（TypeScript）

| 類別 | 選擇 | 說明 |
|------|------|------|
| Framework | **React + TypeScript** | 已確定 |
| Build Tool | **Vite** | 快速 build、dev server HMR |
| UI Components | **shadcn/ui** | Dashboard 型 UI，可自訂性高 |
| Charts | **Recharts 或 Apache ECharts** | Session timeline、cost visualization |
| API Client | **TanStack Query (React Query)** | Server state 管理、cache、refetch |

### 8.3 Server 部署方式

API Server 是 core component，**預設常駐運行**。採用單一 process 模式：FastAPI 同時服務 hook endpoints、internal API、以及 Vite build 產出的 frontend static files。

- `secondsight serve --daemon`：background 常駐（安裝後預設啟動）
- `secondsight serve`：foreground 模式（開發 / debug 用）
- `secondsight serve --stop`：停止 daemon
- `secondsight status`：檢查 server 狀態
- 不需要 Node runtime、不需要兩個 process、不需要處理 CORS
- 開發階段可前後端分開跑（Vite dev server + FastAPI）

### 8.4 Infrastructure

| 類別 | 選擇 | 說明 |
|------|------|------|
| IaC | **Terraform** | 已確定（未來如有 hosted 版本） |

### 8.5 Config 結構

#### 8.5.1 Global Config

Repo 提供 `config.example.toml` 樣板，使用者複製到 `~/.secondsight/config.toml` 後修改。

```toml
# ~/.secondsight/config.toml

# ---- General ----
[general]
mode = "cli"                          # "cli" | "sdk"
log_level = "info"                    # debug | info | warning | error

# ---- LLM Providers ----
# CLI 模式不需要設定（借用 coding agent 的 API key）
# SDK 模式至少需要一組 provider
# 優先順序：config 有值 > 環境變數 > 不可用
[providers.anthropic]
ANTHROPIC_API_KEY = ""

[providers.openai]
OPENAI_API_KEY = ""

[providers.custom]                    # 自訂 OpenAI-compatible endpoint
API_KEY = ""
base_url = ""

# ---- Analysis ----
[analysis]
default_agent = "auto"                # auto = 從 observation 推導最常用的 agent_type
timeout_seconds = 300                 # 單次 analysis 最大執行時間

[analysis.models]
claude_code = "claude-haiku-4-5-20251001"
codex = "auto"                        # 待 Phase 0 確認
opencode = ""                         # 需 user 自行設定

[analysis.models.fallback]            # SDK 模式的 LLM router
fallback_models = ["gpt-4o-mini", "gemini-2.0-flash"]

# ---- Feedback ----
[feedback]
convention_injection_budget = 2000    # SessionStart 注入 conventions 的 token 上限
convention_top_n = 15                 # Cross-session 統計取 top N 作為 active conventions
# hint_confidence_threshold = 0.7    # [Reserved] Hint 信心度門檻（Phase 0 不啟用）
# hint_max_firing_per_session = 3    # [Reserved] 同一 hint per session 最多觸發次數（Phase 0 不啟用）

# ---- Retention ----
[retention]
raw_traces_ttl_days = 90
analysis_ttl_days = 365

# ---- Storage ----
[storage.sqlite]
cache_size_mb = 64                    # SQLite in-memory cache（MB），記憶體有限可調小

# ---- Server ----
[server]
host = "127.0.0.1"
port = 8420
auto_start = true                     # secondsight init 後自動啟動 server

# ---- Observation ----
[observation]
session_timeout_minutes = 30          # 超過 N 分鐘沒有新 event，視為 session 結束
```

#### 8.5.2 Per-Project Config

```toml
# ~/.secondsight/projects/{project_id}/config.toml

[project]
description = ""                      # Optional：project 技術背景（analysis agent 的補充 context）

[analysis]
model = ""                            # 空 = 用 global default

[feedback]
convention_injection_budget = 1500
convention_top_n = 10                 # 此 project 只取 top 10

[retention]
raw_traces_ttl_days = 180             # 此 project 保留更久
```

#### 8.5.3 Config 優先順序

```
Per-project config > Global config > Default values
```

API key 優先順序：

```
config.toml 有值 > 環境變數 > 不可用
```

#### 8.5.4 Config Validation（啟動時 Pre-check）

| Mode | 必要檢查 | 說明 |
|------|---------|------|
| CLI | `[analysis.models]` 有對應 agent type 的值 | 確認 model 設定存在 |
| SDK | 同上 + `[providers]` 至少一組有 API key | 確認 LLM provider 可用 |

啟動時若 pre-check 失敗，顯示清楚的錯誤訊息和修正建議。

#### 8.5.5 Framework Profile（不需要獨立 config）

SecondSight **不需要** user 手動填寫 framework profile。Analysis agent 從兩個來源動態取得 project context：

1. **System prompt**：coding agent 已將 CLAUDE.md / AGENTS.md 的內容注入 system prompt，analysis agent 從 session_start event 的 hook payload 取得
2. **Observation events 推導**：從 events 中的 skills / subagent / hook 使用記錄，推導 user 使用的 agent workflow framework

`[project] description` 欄位為 optional 補充，非必要。

---

## 九、CLI 設計

### 9.1 雙 Persona 設計

SecondSight CLI 同時服務兩個 persona：

| Persona | 輸出風格 | 說明 |
|---------|----------|------|
| **Human operator** | Rich tables、色彩、進度條 | 預設模式 |
| **Agent consumer** | `--format json` 結構化輸出 | Agent 透過 shell tool 呼叫時使用 |

### 9.2 Subcommand 結構

```
secondsight
├── init              # 初始化 project config
├── serve             # 啟動 API server（--daemon / --stop）
├── status            # 檢查 server 狀態、project 概覽
├── sync              # 手動觸發 filesystem → DB 同步（含 backfill）
├── analyze           # 手動觸發 analysis
├── directive         # 查詢 / 管理 directives
├── query             # 查詢 traces / analysis results
├── session           # Session 管理
└── cleanup           # 手動觸發 data retention cleanup
```

### 9.3 Agent 整合價值

CLI 是 agent 與 SecondSight 互動的主要 interface：

- **Hook 模式**：hook script 呼叫 `secondsight ingest --event-file /tmp/event.json`
- **Agent 自查**：agent 呼叫 `secondsight query --session latest --format json` 讀取歷史分析
- **Directive 取得**：agent 呼叫 `secondsight directive --active --format json` 取得當前 directives

---

## 十、API Design & Frontend Views

### 10.1 Frontend 資訊架構

Frontend dashboard 採用 **hierarchical drill-down** 模型，以 project 為單位，分為 Observation 和 Analysis 兩個主要 view。Feedback 不需要獨立 view（注入行為是 runtime 自動發生的，不需 user 操作）。

### 10.2 Observation Views（Drill-Down）

```
Project Dashboard
└── Level 1: Session List
    ├── session_id, start_time, duration, total_events, total_tokens, cost estimate
    └── Level 2: Segment List（展開某 session）
        ├── segment_index, user_prompt (truncated), event_count, token_count
        └── Level 3: Event Timeline（展開某 segment）
            ├── 完整 event sequence（thinking, tool_use_start/end, response...）
            └── Sub-agent nesting 可視化（depth indentation）
```

### 10.3 Analysis Views（結果 + 溯源）

```
Analysis Dashboard
├── Project-level Summary
│   ├── Behavior flag 分佈、flag type 趨勢（時間軸）、improvement rate
│   └── 最近 N 個 sessions 的 behavior flag 數量趨勢
├── Session Analysis List
│   ├── session_id, analysis_date, flag_count, key_findings
│   └── 展開：per-segment behavior flags
│       ├── Flag type、涉及事件、intent summary、reason
│       └── → Link 到 Observation Level 3 的對應 segment
└── Directive Management
    ├── Active conventions 列表（含出現頻率、effectiveness）
    ├── Lifecycle 狀態（effective / obsolete / superseded）
    └── 每條 convention 的 source behavior flags → Link 溯源
```

### 10.4 API Endpoints

```
Observation:
GET  /api/sessions                              → Session list（Level 1）
GET  /api/sessions/{id}                         → Session detail
GET  /api/sessions/{id}/segments                → Segment list（Level 2）
GET  /api/sessions/{id}/segments/{idx}          → Event timeline（Level 3）

Analysis:
GET  /api/analysis/summary                      → Project-level analysis summary
GET  /api/analysis/sessions                     → Session analysis list
GET  /api/analysis/sessions/{id}                → Per-session behavior report（flags + context）
GET  /api/analysis/sessions/{id}/flags          → Session behavior flags list
GET  /api/analysis/trends                       → Behavior flag frequency trends
GET  /api/analysis/aggregation                  → Cross-session flag statistics

Directives:
GET  /api/directives                            → Active directives list
GET  /api/directives/{id}                       → Directive detail + source tracing
PATCH /api/directives/{id}                      → Directive management（disable / re-activate）
```

---

## 十一、Error Handling 策略

### 11.1 Error Handling 總覽

| 情境 | 策略 | 說明 |
|------|------|------|
| DB INSERT 失敗 | 記錄 + Backfill | Sync log 記錄失敗；analysis 啟動時先 backfill（從 filesystem 補同步） |
| LLM API 失敗（SDK 模式） | LLM Router fallback | Primary model → fallback models 依序嘗試；全部失敗 → 標記 analysis failed |
| LLM API 失敗（CLI 模式） | Coding agent 自行處理 | CLI 模式借用 coding agent 的 model/API，錯誤由 coding agent 顯示 |
| Filesystem write 失敗（磁碟滿） | Graceful error | 記錄錯誤、不 crash，使用者自行處理磁碟空間 |
| DB 損壞 | `secondsight sync --rebuild` | 從 filesystem raw traces 完整重建 DB；機率極低（SQLite WAL mode 有 crash recovery） |
| Hook process crash | Observation 遺失該事件 | Raw trace 未落地則無法復原；不影響已落地的歷史資料和後續 hook |
| Analysis 中途失敗 | 標記為 failed，可 re-analyze | `secondsight analyze --session {id}` 手動重跑 |

### 11.2 Backfill 詳細流程

```python
class BackfillService:
    def sync_session(self, session_id: str):
        """從 filesystem 補同步缺失的 events 到 DB"""
        # 1. 列出 filesystem 上所有 event files
        fs_events = self.raw_trace_store.list_events(session_id)

        # 2. 查 DB 已有的 event IDs
        db_event_ids = self.event_repo.get_event_ids(session_id)

        # 3. 差集 = 需要補同步的
        missing = [e for e in fs_events if e.id not in db_event_ids]

        # 4. Batch INSERT
        if missing:
            self.event_repo.batch_insert(missing)
```

---

## 十二、Storage Layer 模組架構

### 10.1 Repository Pattern

使用 SQLAlchemy Core（不用 ORM）+ Alembic，搭配 Repository pattern：

```
src/secondsight/storage/
├── models/                    # SQLAlchemy Core table definitions
│   ├── events.py              #   events table（單一表 + JSON data）
│   ├── sessions.py            #   sessions metadata table
│   ├── analysis.py            #   analysis results table
│   ├── directives.py          #   directives table
│   └── patterns.py            #   patterns table
├── repositories/              # 每個 domain 一個 repository
│   ├── event_repo.py          #   Event CRUD + segment query + sub-agent query
│   ├── session_repo.py        #   Session metadata 操作
│   ├── analysis_repo.py       #   Analysis result 操作
│   └── directive_repo.py      #   Directive CRUD + lifecycle management
├── migrations/                # Alembic migrations
├── raw_trace_store.py         # Filesystem raw trace 讀寫
└── engine.py                  # DB engine factory + PRAGMA 配置
```

Repository 內部可用 SQLAlchemy Core expression 或 raw SQL，外部只看到 Pydantic model 的輸入輸出。

**event_repo 的關鍵查詢**：

```python
class EventRepository:
    def insert(self, event: Event) -> None: ...
    def get_session_events(self, session_id: str) -> list[Event]: ...
    def get_segments(self, session_id: str) -> list[int]: ...
    def get_segment_events(self, session_id: str, segment_index: int) -> list[Event]: ...
    def get_max_segment_index(self, session_id: str) -> int | None: ...
    def get_sub_agent_events(self, session_id: str, sub_agent_id: str) -> list[Event]: ...
    def get_cost_by_depth(self, session_id: str) -> dict[int, int]: ...
```

---

## 十三、待討論項目

以下項目尚未深入討論，將在後續補充：

- [x] ~~Unified Event Schema 觀測粒度~~ → 已定義於 3.7
- [x] ~~Session 識別策略~~ → 已定義於 3.8
- [x] ~~Feedback 機制（粗粒度 / 細粒度）~~ → 已定義於第六節
- [x] ~~Analysis results 儲存決策~~ → 已定義於第七節
- [x] ~~Directive injection 機制~~ → 已定義於 6.3 / 6.4
- [x] ~~Event Model 完整設計~~ → 已定義於 3.7（11 event types、DB schema、segment index、sub-agent nesting）
- [x] ~~Observation Data Flow Pipeline~~ → 已定義於 3.9
- [x] ~~Codebase Folder Structure~~ → 已定義於 2.3（config 獨立、observation module、完整依賴方向）
- [x] ~~Error Handling 策略~~ → 已定義於第十一節（backfill、LLM router、rebuild）
- [x] ~~Data Retention & Cleanup~~ → 已定義於 3.10（TTL 機制、清理時機）
- [x] ~~API Design / Frontend Views~~ → 已定義於第十節（hierarchical drill-down、API endpoints）
- [x] ~~LLM Router fallback~~ → 已定義於 5.6.4
- [x] ~~Config 完整結構~~ → 已定義於 8.5
- [x] ~~Framework philosophy 的 input / 自動抽取機制~~ → 已移除（ADR-012）
- [x] ~~Hint 的 trigger_pattern 設計~~ → Hint 機制改為 reserved，Phase 0 不實作（見 6.4）
- [x] ~~Analysis Agent 的 prompt architecture（含 behavior flag 語意歸類）~~ → 已定義於 5.5（BehaviorFlagType code 定義 + segment-level prompt + cross-session aggregation prompt）
- [x] ~~Convention 從 review_required 升級為 auto 的條件~~ → 全部直接 auto，user 可從 dashboard 刪除
- [ ] Session identity linking 的具體實作（待 Phase 0 調查）
- [ ] Cross-project pattern transfer 機制（Phase 3B+）

**Phase 0 調查項（跨 Agent）**：

- [ ] Codex hook 機制：是否有 hook、支援哪些 event types
- [x] ~~OpenCode hook 機制：同上~~ → 已完成（Phase 0 task-2）：plugin API 75% / +DB polling 100% 覆蓋；採用 fs-event watching DB 路徑（ADR-014），plugin route 不採用
- [ ] Codex / OpenCode 的 sub-agent 機制：是否支援、hook 如何觸發
- [ ] Codex / OpenCode 的 task / plan mode：是否存在、hook 如何觸發
- [ ] 各 agent hook payload 格式：session context 帶什麼資訊
- [ ] Claude Code hook subprocess 生命週期：回傳 output 後是否可繼續 background work（影響 observation 寫入是否可 non-blocking）

---

## 十四、架構決策紀錄（ADR）

### ADR-001：拿掉 Claim-Confirm Queue

- **決策**：移除 durable queue / claim-confirm pattern，改用 filesystem-first + async DB sync
- **背景**：PRD 原文提到「採用 durable queue / claim-confirm pattern 避免資料遺失」
- **理由**：
  - Filesystem write 本身就是 durable，不需要額外的 queue 層提供 persistence
  - SecondSight 是 local-first、單機、低 throughput 場景，不符合 queue 的典型使用場景
  - claude-mem 驗證了 filesystem-first 模式在相同場景下的可行性
  - 減少架構複雜度（不需要 queue table、consumer daemon、claim/confirm 邏輯）
- **影響**：Plan 中 P0-14、P1-6 移除或降級為 optional

### ADR-002：Per-Project DB

- **決策**：每個 project 獨立一組 DB files（intelligence.db + filesystem raw traces），外加 global registry.db
- **背景**：使用者可能同時在多個 project 上跑不同 coding agents
- **理由**：
  - 消除跨 project 的 SQLite write lock contention
  - Framework philosophy、directives、session context 本質上都是 per-project 的
  - 支援 project-level archive / deletion / backup
- **影響**：需要 project discovery 機制（project ID 推導邏輯）和 global registry 管理

### ADR-003：SQLAlchemy Core over ORM

- **決策**：使用 SQLAlchemy Core + Alembic，不使用 SQLAlchemy ORM
- **背景**：需要 DB abstraction 支援未來 migration（SQLite → DuckDB / PostgreSQL）
- **理由**：
  - Analysis Layer 需要大量 analytical queries（aggregation、window functions），ORM 寫法彆扭
  - Core 提供 table definition、connection management、migration 能力，已滿足模組化需求
  - 保留直接寫 SQL 的靈活性
- **影響**：query 層需要手動管理，但搭配 Repository pattern 可維持良好封裝

### ADR-004：PydanticAI + LiteLLM fallback（取代 Google ADK）

- **決策**：使用 PydanticAI 作為 SDK 模式的 agent framework，LiteLLM 作為 optional provider fallback；不使用 Google ADK
- **背景**：SDK 模式需要提供 agent loop 實作，讓開發者可以直接觸發分析；CLI 模式則借用 coding agent 本身的 loop
- **理由**：
  - PydanticAI 輕量、Pydantic-native，與 SecondSight 的 tech stack 天然整合
  - PydanticAI 原生支援 OpenAI-compatible endpoints，覆蓋大部分 provider
  - 特殊 provider 可透過 LiteLLM 掛載為 PydanticAI 的 provider fallback
  - Google ADK 是 heavy orchestration framework，對 SecondSight 來說 overkill
  - ADK 的 multi-provider 底層就是 LiteLLM，等於多包一層不必要的 orchestration
- **影響**：SDK 模式有內建 agent loop；CLI 模式不需要自建 loop（借用 coding agent）

### ADR-005：觀測資料粒度——存行為結構，不存工具內容

- **決策**：Tool use 只存 action + target + metadata，不存 input/output content；user prompt 完整儲存
- **背景**：需要決定 observation layer 記錄多少資料
- **理由**：
  - SecondSight 是 local-first 服務，資料完全在使用者機器上，不存在資料敏感性問題
  - User prompt 是判斷 agent 行為對齊度的核心依據，必須完整保存
  - Tool use 的 content（檔案內容、command output）量大但非必要——行為模式分析只需 action + target
  - Analysis agent 需要深入判斷時，可按需讀取 project 的實際檔案（兩層分析策略）
  - 大幅減少儲存量，延後 scale 壓力
- **影響**：Analysis agent 需要 `read_project_file` tool 來支撐第二層判斷

### ADR-006：不寫 CLAUDE.md，使用 Hook 注入 Feedback

- **決策**：SecondSight 不修改 CLAUDE.md 或其他 agent project config，改用 hook 機制注入 feedback
- **背景**：需要決定 feedback directives 如何送達 coding agent
- **理由**：
  - CLAUDE.md 只適用於 Claude Code，不具跨 agent 通用性
  - 研究指出 CLAUDE.md 適合 project map 類知識，LLM 生成的規範類指令反而降低 performance
  - CLAUDE.md 是使用者自己維護的，自動修改會造成 ownership 混亂
  - Hook 機制（SessionStart / PreToolUse）提供更精確的注入時機和粒度控制
- **影響**：feedback 依賴各 agent 的 hook 機制，需要在 adapter 層做跨 agent 適配

### ADR-007：Analysis Results 存 DB 為主 + Filesystem JSON 備份

- **決策**：Analysis results 主要寫入 intelligence.db，同時寫一份 JSON 到 filesystem 作為備份
- **背景**：需要決定 analysis 結果的儲存位置
- **理由**：
  - Feedback 消費端（convention query、hint lookup、dashboard aggregation）都需要結構化查詢，DB 是主要存取路徑
  - Analysis results 是 derived data 但重新生成需花 LLM tokens，filesystem 備份避免 DB 損壞時重跑
  - CLI 模式下 coding agent 可直接讀取 JSON，不需經過 DB
- **影響**：每次 analysis 完成需同時寫 DB + filesystem，但寫入成本低

### ADR-008：API Server 為 Core Component（更新 v2.0）

- **決策**：API Server（FastAPI）從 optional frontend server 升級為 core component，常駐運行，同時服務 hook fast path（純觀測寫入）、frontend dashboard、internal API
- **背景**：原本 API server 只服務 frontend；hook script 需要低 latency 的 async observation 寫入
- **理由**：
  - Hook script 作為 thin HTTP client（bash + curl），所有重邏輯在 server 端處理，hook latency ~7ms
  - Server 端 in-memory 維護 session tracker，不需每次 hook 重新載入
  - Observation 寫入在 server 端 async 執行，不阻塞 hook response
  - 單一 process 同時服務 hook + dashboard + API，架構簡單
  - Fallback：server 未啟動時 hook graceful skip（raw event 寫入 fallback file）
- **替代方案**：每次 hook cold start Python process → ~30-50ms latency；獨立 daemon → 多一個 process 要管
- **影響**：`secondsight serve` 從 optional 變為必要；需要 daemon mode 支援（--daemon / --stop）；Config 需要 `[server]` section

### ADR-009：Segment-Based Analysis——以 User Prompt 為分析單位

- **決策**：Analysis 以 user prompt 為邊界切分 segment，每個 segment 獨立分析 prompt-action 對齊度；靜態 metrics 僅作為 supplementary context，不作為獨立判斷依據
- **背景**：需要決定 analysis 的分析粒度與方法
- **理由**：
  - Agent 的每個 action 與其 user prompt context 高度相關，脫離 prompt 的統計分析無法判斷行為合理性
  - Context engineering 下，agent 可能合理地重複讀取同一檔案（不同 prompt 間），靜態 metrics 會產出 false positive
  - 以 prompt 為單位能精準判斷 agent 是否理解意圖、行為是否對齊
  - Observation 只存 action + target（不存 content），trace 非常輕量，用 Haiku 級 model 分析成本極低
- **替代方案**：純 rule-based 靜態分析 → 無法處理 context-dependent 行為；全 session 一次性分析 → 長 session tokens 過多且結果模糊
- **影響**：Analysis Agent 需要 segment splitting 邏輯（自動、不用 LLM）+ per-segment LLM 分析 + session-level 彙總

### ADR-010：Default 使用最輕量 Model + 不做 In-Session 偵測

- **決策**：Analysis 預設使用最輕量 model（Claude Code → Haiku，Codex → 待確認，OpenCode → user 設定）；不在 session 進行中做任何分析或即時偵測
- **背景**：需要在分析品質、成本、使用者體驗之間取得平衡
- **理由**：
  - Analysis 是 post-run 任務，延遲不敏感，但頻率高（每個 session 都跑），成本必須低
  - Haiku 級 model 對 trace 結構化資料的分析能力已足夠（token 量低、結構明確）
  - In-session 偵測會干擾使用者工作流程，增加 hook latency，且當前 session 的 pattern 判斷容易誤判
  - Per-project config 允許使用者對品質需求高的 project 升級 model
- **影響**：Phase 0 需調查 Codex 支援的 model 選項；所有 hint 皆來自歷史 analysis，無即時偵測

### ADR-011：Single Events Table + JSON Data Column + Pre/Post 分開存

- **決策**：所有 event types 存在單一 `events` 表，共用欄位（duration_ms、token_count）為 column，type-specific 欄位存 JSON `data` column；tool_use_start 和 tool_use_end 分開存為兩筆 event
- **背景**：需要決定 events 的 DB schema 設計和 Pre/Post tool use 的儲存方式
- **理由**：
  - Analysis 的主要查詢是「拿出 session 所有 events 按序排列」，單一表一個 query 搞定
  - 11 種 event types 各有不同欄位，分表會導致 UNION ALL 查詢複雜
  - JSON data column 保持彈性，新增 event type 不改 schema
  - Pre/Post 分開存：寫入快（直接 append，不需 UPDATE）、保留真實執行順序、不需配對 ID 機制
  - Analysis 層配對 tool_use_start/end 時，按 sequence_number 掃描即可
- **替代方案**：分表 per event type → 查詢複雜；Pre/Post 合併為一筆 → 需要 UPDATE 或配對 ID 管理
- **影響**：Analysis 層需要 tool event pairing 邏輯；edge case 處理（orphan start/end）

### ADR-012：拿掉 Framework Profile，改為動態推導

- **決策**：不需要 user 手動填寫 framework profile，也不需要獨立的 framework_profile.json。Analysis agent 從 system prompt 和 observation events 動態取得 project context
- **背景**：原設計有 framework_profile.json 讓 user 描述 project 的技術背景
- **理由**：
  - Coding agent 已將 CLAUDE.md / AGENTS.md 注入 system prompt，SecondSight 從 session_start hook payload 取得即可，省掉一個處理
  - 從 observation events 可推導 user 使用的 skills / subagent / hooks，判斷 agent workflow framework
  - 減少 user 手動設定負擔，降低 onboarding friction
- **影響**：移除 framework_profile.json；per-project config 保留 optional `[project] description` 作為補充

### ADR-013：兩層分析架構 + Convention-First Feedback

- **決策**：Analysis 分為兩層（session-level 行為分析 + cross-session 彙整），feedback 以 convention 為主要機制，hint 保留設計彈性但 Phase 0 不實作
- **背景**：原設計中 hint 透過 PreToolUse pattern match 注入，但實際場景中有效的指導是行為策略層級的（如「user 指定檔案時直接操作」），無法用 tool_name + target 的 pattern match 有效觸發
- **理由**：
  - 行為分析的本質是回顧 agent 做事過程的效率，像 senior engineer review junior 的做事方式
  - 單次觀察可能是特殊情況，跨 session 統計才能確認是真正的低效習慣
  - Convention 注入在 system prompt 比 PreToolUse 逐次注入更自然，且不增加 hook latency
  - Hint 的有效觸發需要理解 user prompt 意圖，非簡單 pattern match 能做到，先保留後續用輕量 LLM 判斷
- **影響**：
  - 新增 `behavior_flags` table 儲存第一層分析結果
  - Hook fast path 簡化為純觀測寫入，不做 hint matching
  - Convention 來源從「analysis agent 直接生成」改為「cross-session 統計 top N」
  - DB schema 保留 hint 相關欄位（nullable），code interface 保留 hint 方法（空實作）

### ADR-014：OpenCode 走 fs-event Watch DB，不採用 Plugin Hook

- **決策**：OpenCode 的 observation 走 file-system event watching（macOS FSEvents / Linux inotify）監聽 `~/.local/share/opencode/opencode.db` + 唯讀 SQLite 增量讀取；**不採用** OpenCode 官方的 plugin hook API（`@opencode-ai/plugin`）
- **背景**：OpenCode 是三個目標 agent 中唯一**沒有 subprocess hook** 機制的——它的「hook」是 plugin 內部的 JS/TS callback，必須跑在 OpenCode 自己的 Bun runtime 內（in-process）。Phase 0 task-2 完整列舉 9 種 plugin hook + 2 種 DB polling 機制，並確認 plugin route 與 DB route 是兩條獨立可選的觀測路徑（見 `changes/2026-04-24_phase0-feasibility/investigations/opencode-hooks.yaml`）
- **理由**：
  - 採用官方 plugin 會綁進 Bun runtime 依賴，違反「Python-first、輕量整合」的架構原則
  - 跨語言 bridge plugin（JS plugin → HTTP → Python daemon）會多一個 runtime + 一份 JS 程式碼要維護，部署成本提高、failure surface 擴大
  - lazyagent 已驗證唯讀 SQLite 在 OpenCode WAL mode 下不會造成 lock 衝突（OpenCode 寫入時 reader 不被擋）
  - fs-event watching 比純 interval polling 更接近即時（<100ms vs 平均 N/2 秒）、且事件驅動更省資源；lazyagent 的 3 秒 polling 是「能跑就好」的最小版本，不是好設計
  - OpenCode SQLite schema 雖是非官方，但有 lazyagent cross-validation 提供 schema 確認的 reference point
- **影響**：
  - OpenCode adapter 命名應中性（如 `OpenCodeDBObserver`），不要綁實作細節（不要叫 `OpenCodePollingAdapter`）——觀測機制（fs-event vs polling）會迭代演進
  - 採用 hybrid 模式：fs-event 為主 + 低頻 polling 兜底（fs-event 偶爾丟失事件時補救）
  - 必須維持唯讀 access（避免 WAL corruption——lazyagent 已驗證的設計約束）
  - 必須有 schema validation：OpenCode SQLite schema 跨版本不保證穩定，schema 飄移要顯化（不可用 `try/except: pass` 靜默吞掉）
- **Trade-off**：接受「schema 飄移」這個顯性風險（可用 schema validation 偵測），換掉「跨 runtime 依賴」這個更難管理的隱性風險（Bun crash 會牽連整個 deployment、failure 不易歸因）
- **未驗證假設**（待 Phase 1 實測）：
  - fs-event watching 在 macOS / Linux 的延遲分布與事件丟失率
  - hybrid mode 中 polling 兜底的合適間隔（過頻吃資源、過疏延遲變大）
  - OpenCode WAL checkpoint 時機是否會對唯讀 reader 造成可觀察的延遲
- **關聯死亡情境**：若 OpenCode 改變 SQLite schema 而 SecondSight 沒偵測到，會靜默產生錯誤資料（token 統計變 0、failure 變 success）；schema validation 是這個死亡情境的偵測機制，不是可選功能


---

## 版本歷史

| 版本 | 日期 | 變更 |
|------|------|------|
| 1.0 | 2026-04-25 | 初版，記錄所有已確認的 system design 決策 |
| 1.1 | 2026-04-25 | 修正第二節：Observation 為單一模式（hook monitoring），Analysis 才有 CLI/SDK 兩種觸發方式；所有 codebase 路徑統一為 `src/secondsight/` |
| 1.2 | 2026-04-25 | 新增 3.7 觀測資料粒度（存行為結構不存內容、user prompt 完整儲存）、3.8 Session 識別策略 |
| 1.3 | 2026-04-25 | 重寫第五節：CLI/SDK 兩種分析模式、PydanticAI 取代純 LiteLLM、Analysis Agent tools 設計；更新 ADR-004 |
| 1.4 | 2026-04-25 | 修正 2.2 CLI 模式描述（coding agent 提供 loop）、新增 5.4 兩層分析策略、新增 ADR-005 觀測資料粒度決策 |
| 1.5 | 2026-04-25 | 新增第六節 Feedback 機制（粗粒度 convention + 細粒度 hint、不寫 CLAUDE.md）、新增第七節 Analysis Results 儲存（DB 為主 + filesystem 備份）、新增 ADR-006/007/008、章節重新編號 |
| 1.6 | 2026-04-25 | Analysis Layer 完整設計：新增 5.3 Segment-Based Analysis（以 user prompt 為分析單位）、5.5 Analysis 執行時機（post-run only）、5.6 Analysis Model 設定（default 最輕量 model）、5.7 分析結果大小限制（Hint ≤100 tokens / Convention ≤200 tokens / SessionStart ≤2000 tokens）、5.8 Directive Lifecycle（iterative 優化、淘汰、re-activation）、新增 ADR-009/010 |
| 1.7 | 2026-04-25 | Observation Layer + Event Model 完整設計：重寫 3.7 為完整 Event Model（11 event types、Claude Code hook mapping、跨 agent 抽象層）、新增 3.7.5 Events Table 設計（single table + JSON data + segment_index + sub-agent nesting）、新增 3.9 Observation Data Flow Pipeline、更新 2.3 Codebase Folder Structure（config 獨立、observation module、依賴方向）、更新第四節 Adapter Interface（cross-cutting + graceful degradation）、更新第十節 Storage Layer、新增 ADR-011、新增 Phase 0 跨 Agent 調查項 |
| 1.8 | 2026-04-25 | 新增 3.9.1 Hook Latency 分析（blocking vs non-blocking path）、3.9.2 Backfill 機制、3.10 Data Retention & Cleanup（TTL）、5.6.4 LLM Router（SDK fallback）、第十節 API Design & Frontend Views（hierarchical drill-down + endpoints）、第十一節 Error Handling 策略、analysis/prompts/ folder 加入 codebase structure、章節重新編號（十～十四） |
| 1.9 | 2026-04-27 | API Server 升級為 Core Component（常駐運行）、Hook Scripts 改為 thin HTTP client（bash+curl ~8ms）、Observation Pipeline 改為 server-side async 寫入、新增 Fallback 機制（server down → fallback_events.jsonl）、完整 Config 結構（config.toml global/per-project、priority: config > env var > default）、拿掉 Framework Profile（ADR-012：動態推導取代靜態設定）、CLI 更新（新增 serve/status/cleanup、移除 ingest）、ADR-008 更新為 API Server Core Component |
| 2.0 | 2026-04-27 | Analysis 重構為兩層架構：第一層 session-level 行為分析（Behavior Flags 標記系統：6 種 flag types）、第二層 cross-session 彙整（統計頻率 → top N → convention）。Feedback 簡化為 convention-first（system prompt 注入），hint 機制保留設計彈性但 Phase 0 不實作（ADR-013）。Hook fast path 簡化為純觀測寫入。新增 behavior_flags DB table、更新 directives table（新增 frequency/source_flag_type 欄位、hint 欄位改 nullable reserved）。更新 codebase structure（analysis/ 新增 behavior.py + aggregator.py）。Config feedback section 簡化（移除 hint 設定、新增 convention_top_n） |
| 2.1 | 2026-04-27 | Analysis Prompt Architecture 完整設計：新增 5.5 節（BehaviorFlagType code 層級定義 + FLAG_DEFINITIONS as source of truth、segment-level analysis prompt 結構、cross-session aggregation 三步流程——自動分組 → per flag_type LLM 歸類 → 自動合併取 top N）。Convention 全 auto（移除 review_required / adoption_mode，user 可從 dashboard 刪除）。Module 依賴方向改用 mermaid digraph。SQLite PRAGMA 分硬編碼 + 可配置（cache_size_mb via config.toml）。章節重新編號（5.5~5.9） |
| 2.2 | 2026-04-27 | OpenCode observation 機制定案：採用 fs-event watching `opencode.db` + 唯讀 SQLite 增量讀取，不採用官方 Bun plugin（避免跨 runtime 依賴）。更新 2.1 / 4.1 / 4.3 / 6.6 OpenCode 相關段落、Phase 0 checklist 標記 OpenCode hook 機制完成、新增 ADR-014 |
