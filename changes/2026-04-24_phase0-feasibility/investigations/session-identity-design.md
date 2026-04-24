# Session Identity Linking Design

## 1. Identity Model

SecondSight's identity model defines five dimensions for tracking sessions and their relationships across agents, projects, and directive lineage.

### Identity Dimensions

| Dimension | Description | Source |
|-----------|-------------|--------|
| **agent_type** | Which agent produced this session (claude_code, opencode, codex) | Inferred from storage location and file format |
| **agent_session_id** | The agent-native session identifier (UUID for all three agents) | Extracted from session data |
| **ss_session_id** | SecondSight-owned canonical session identifier (UUIDv4) | Minted by SecondSight at ingestion time |
| **project_id** | Normalized project path — the cross-agent anchor | Derived from working directory with normalization |
| **directive_lineage** | Links a directive to its source session and outcome sessions | Maintained by SecondSight's directive tracking |

### Why ss_session_id (SecondSight's Own Identifier)

Agent-native session IDs have different lifetimes and stability guarantees. SecondSight mints its own stable `ss_session_id` (UUIDv4) at the moment a session is first ingested. This surrogate key:
- Decouples SecondSight from agent-native ID format changes
- Provides a stable reference even if agents recycle or reset IDs
- Enables cross-agent linking through a common key space

The mapping `(agent_type, agent_session_id) → ss_session_id` is stored permanently and is the foundation of all linking.

### Per-Agent Session Identity Attributes

**Claude Code:**
- Session ID: UUID, stored as `sessionId` field in JSONL transcript first line
- Storage: `~/.claude/projects/<encoded-cwd>/<session-id>.jsonl`
- CWD encoding: Slashes and dots converted to dashes (e.g., `/Users/user/project` → `-Users-user-project`)
- Available at session start: sessionId, cwd, version, gitBranch, timestamp
- Available per event: type, message content, tool calls, token usage
- Subagent detection: `isSidechain` flag
- Persistence: Persistent, resumable via `claude --resume <session-id> --cwd <cwd>`

**OpenCode:**
- Session ID: UUID, stored in SQLite `session.id` column
- Storage: `~/.local/share/opencode/opencode.db` (SQLite database)
- Schema: session table with id, project_id, parent_id, directory, title, version, time_created, time_updated, time_compacting, time_archived
- Available at session start: id, project_id, directory, title, version, time_created
- Available per message: role, modelID, cost, tokens, tool calls and outputs
- Subagent detection: `parent_id` non-null indicates subagent relationship
- Persistence: Persistent, resumable via `opencode --session <id>`

**Codex:**
- Session ID: UUID, stored in `session_meta` event payload as `id`
- Storage: `~/.codex/sessions/<session-id>.jsonl`
- Index: Optional `~/.codex/session_index.jsonl` for thread name mappings
- Available at session start: id, cwd, cli_version, agent_nickname, source (via `session_meta` event)
- Available per turn: cwd, model, git.branch (via `turn_context`), tool calls (via `response_item`), token usage (via `event_msg`)
- Subagent detection: `agent_nickname` or `source` field in session_meta
- Persistence: Persistent, resumable via `codex resume <session-id>`

### Session ID Stability Assessment

All three agents generate UUID session IDs that are **persistent** — they survive process restarts and are stable for the lifetime of a session. However, there are ephemeral risks:

- **No agent reuses session IDs across new sessions.** A new invocation always generates a new UUID. This means two sessions on the same project are distinct sessions with distinct IDs — linking them requires the project dimension, not the session ID.
- **Resumed sessions retain their original ID.** This is confirmed for all three agents. A resumed session is the same session, not a new one.
- **Fallback risk:** If an agent changes its session ID generation strategy (e.g., moves from UUID to sequential or hash-based), SecondSight's `ss_session_id` insulates the system from this change. The only impact would be on the mapping layer.

The ephemeral ID risk is **low for individual sessions** but creates a challenge for linking: two sessions on the same project by the same user have no inherent link except the project path.

## 2. Linking Strategy

### Cross-Session Linking

Sessions are linked through **project_id** — a normalized representation of the working directory.

**Normalization procedure (handles path format differences across agents):**
1. Resolve to absolute path (expand `~`, resolve symlinks)
2. Remove trailing slashes
3. For Claude Code's encoded paths: decode dashes back to slashes (reverse the `-Users-user-project` → `/Users/user/project` encoding)
4. Apply canonical form: lowercase on case-insensitive filesystems, NFC Unicode normalization
5. Hash the canonical path to produce a stable `project_id`

This normalization is critical because Claude Code encodes paths as `-Users-user-project` while OpenCode and Codex store absolute paths like `/Users/user/project`. Without normalization, the same project would appear as two different projects.

### Linking Scenarios

| Scenario | Linking Mechanism | Feasibility |
|----------|-------------------|-------------|
| Same agent, consecutive sessions | agent_session_id continuity + project_id match | **Fully supported** |
| Same agent, non-consecutive sessions | project_id match + time ordering | **Fully supported** |
| Cross-agent, same project | project_id match (via normalization) | **Supported with path normalization** |
| Cross-agent, different projects | No link expected | N/A |
| Subagent to parent | Agent-native parent_id (OpenCode) or isSidechain (Claude Code) or agent_nickname (Codex) | **Partially supported** — mechanism differs per agent |

### Cross-Agent Linking Feasibility Verdict

Cross-agent identity linking is **feasible with caveats**:
- All three agents expose working directory, so project_id normalization is the viable anchor
- Path normalization must be correct — Claude Code's dash-encoding must be reversible without ambiguity
- Cross-agent linking is **project-scoped**: it links sessions that work on the same project, not sessions that are "related" in any deeper sense
- Cross-agent directive lineage (directive generated from Claude Code session, applied in Codex session) depends on project_id linking + SecondSight's directive tracking — not on agent cooperation

**Risk:** Path normalization ambiguity. Claude Code's encoding is lossy for paths containing literal dashes. Example: `/Users/user/my-project` encodes to `-Users-user-my-project`, which could also decode as `/Users/user/my/project`. Mitigation: use the `cwd` field from JSONL content (which stores the original path) rather than decoding the directory name.

## 3. Directive Lineage

Directive lineage tracks the chain: **analysis of session S1 → directive D generated → D applied in session S2 → outcome observed in session S3**.

```
Session S1 (observed) ──analysis──→ Directive D1
                                        │
                                    applied in
                                        │
                                        ▼
                            Session S2 (with D1 active)
                                        │
                                    outcome
                                        │
                                        ▼
                            Directive Outcome Record
                                (D1 effective? / D1 caused regression?)
```

Lineage is maintained by SecondSight, not by agents. The directive store records:
- `source_ss_session_id`: which session's analysis produced this directive
- `applied_ss_session_ids[]`: which sessions had this directive active
- `outcome_records[]`: per-session outcome observations

This requires no agent cooperation — SecondSight knows which directives were injected into which sessions because SecondSight controls the injection.

## 4. Linking Timing

Identity linking happens in **two phases**:

### Phase A: At Ingestion Time (Real-Time)

When the first event of a session arrives, SecondSight has access to:
- **Minimum viable identity:** agent_type, agent_session_id, working directory (cwd), timestamp
- This is sufficient to: mint `ss_session_id`, compute `project_id`, create the session record

At the first event, SecondSight can immediately link this session to its project and agent. No deferred processing is needed for basic identity.

### Phase B: At Session End (Post-Session)

Some attributes are only available after the session ends:
- Total token usage and cost
- Session outcome (success/failure/abandoned)
- Final working directory (if it changed during session)
- Session duration

These are **enrichment attributes**, not identity attributes. They do not affect linking — they augment the session record after the fact.

### Minimum Viable Identity at First Event

| Field | Available at First Event | Source |
|-------|--------------------------|--------|
| agent_type | Yes | Inferred from storage format/location |
| agent_session_id | Yes | First event in all three agents contains session ID |
| cwd / working directory | Yes | All three agents include CWD in session start or first event |
| timestamp | Yes | All events are timestamped |
| git branch | Usually yes | Claude Code and Codex include in early events; OpenCode may not |
| model | Sometimes | May appear in first message, not guaranteed at session_meta level |

**Conclusion:** Minimum viable identity is available at the first event for all three agents. Deferred/lazy linking is not required for basic session identity.

## 5. Degradation Levels

When full linking is not achievable, the system degrades gracefully:

### Level 0: Full Linking (Best Case)
- All five identity dimensions available
- Cross-agent linking works via project_id normalization
- Directive lineage tracked across agents
- **Feature loss:** None

### Level 1: Single-Agent Linking
- Cross-agent project_id normalization fails (e.g., path encoding is ambiguous)
- Sessions are linked within the same agent only
- Directive lineage limited to same-agent chains
- **Feature loss:** Cannot observe directive effectiveness when user switches between agents. Cross-agent pattern detection not available. Loses the ability to track "directive generated from Claude Code analysis, applied in Codex session."

### Level 2: Session-Isolated (Worst Case / Fallback)
- No linking at all — each session is an island
- Occurs when: agent doesn't expose session ID or CWD, or storage format is unreadable
- **Feature loss:** No directive lineage. No cross-session learning. No cumulative optimization. SecondSight degrades to per-session analysis only. Cannot answer "is this agent improving over time?" Loses the ability to do any multi-session analysis.

### Degradation Detection

SecondSight should detect and report its current degradation level:
- **Level 0 → 1:** Path normalization confidence < 90% for a given agent. Log warning, mark cross-agent links as `low_confidence`.
- **Level 1 → 2:** Agent provides no session ID or no CWD. Log error, create session with `ss_session_id` only, mark as `isolated`.

## 6. Example Data

| ss_session_id | agent_type | agent_session_id | project_id | cwd | timestamp | directive_applied | notes |
|---------------|------------|------------------|------------|-----|-----------|-------------------|-------|
| S1 | claude_code | `fa493ff8-3856-...` | `proj_abc123` | `/Users/dev/myapp` | 2026-04-20T10:00Z | — | Initial debugging session |
| S2 | claude_code | `b2e1c4d7-9a3f-...` | `proj_abc123` | `/Users/dev/myapp` | 2026-04-20T14:00Z | D1 (from S1 analysis) | Same agent, same project, directive applied |
| S3 | opencode | `8c5d2e1a-f4b6-...` | `proj_abc123` | `/Users/dev/myapp` | 2026-04-21T09:00Z | D1 (from S1 analysis) | Cross-agent: different agent, same project |
| S4 | codex | `3f7a9b2c-d1e5-...` | `proj_abc123` | `/Users/dev/myapp` | 2026-04-21T11:00Z | D2 (from S3 analysis) | Cross-agent: Codex on same project |
| S5 | claude_code | `e9f8d7c6-b5a4-...` | `proj_xyz789` | `/Users/dev/other-app` | 2026-04-21T13:00Z | — | Different project, no linking to S1-S4 |
| S6 | opencode | `1a2b3c4d-5e6f-...` | `proj_abc123` | `/Users/dev/myapp` | 2026-04-22T10:00Z | D1, D2 | Cross-agent: back to same project with accumulated directives |
| S7 | claude_code | `7g8h9i0j-1k2l-...` | `proj_abc123` | `/Users/dev/myapp` | 2026-04-22T15:00Z | D3 (from S6 analysis) | Directive lineage: S1→D1→S2,S3→D2→S4→S6→D3→S7 |

**Linking demonstrated:**
- S1 → S2: Same agent, same project, consecutive (linked via project_id)
- S1 → S3: Cross-agent (Claude Code → OpenCode), same project (linked via normalized project_id)
- S3 → S4: Cross-agent (OpenCode → Codex), same project
- D1 lineage: Generated from S1 analysis → applied in S2, S3, S6 → outcomes observed
- S5: Different project — correctly not linked to S1-S4

## 7. Phase 1 Implementation Requirements

Phase 1 must implement the following from this design:

### Storage Schema

```sql
CREATE TABLE sessions (
    ss_session_id TEXT PRIMARY KEY,        -- SecondSight canonical ID (UUIDv4)
    agent_type TEXT NOT NULL,              -- claude_code | opencode | codex
    agent_session_id TEXT NOT NULL,        -- Agent-native session ID
    project_id TEXT NOT NULL,              -- Normalized project hash
    cwd TEXT NOT NULL,                     -- Original working directory path
    cwd_normalized TEXT NOT NULL,          -- Canonicalized path
    git_branch TEXT,                       -- If available
    agent_version TEXT,                    -- Agent CLI version
    started_at TEXT NOT NULL,              -- ISO 8601 timestamp
    ended_at TEXT,                         -- NULL if session still active
    parent_ss_session_id TEXT,             -- For subagent sessions
    degradation_level INTEGER DEFAULT 0,  -- 0=full, 1=single-agent, 2=isolated
    UNIQUE(agent_type, agent_session_id)
);

CREATE INDEX idx_sessions_project ON sessions(project_id);
CREATE INDEX idx_sessions_agent ON sessions(agent_type, started_at);
```

### Path Normalization Module

Implement `normalize_project_path(cwd: str, agent_type: str) -> str`:
- Handles Claude Code's dash-encoded paths (prefer `cwd` field from JSONL over directory name decoding)
- Resolves symlinks and expands `~`
- Applies canonical form
- Returns stable hash as `project_id`

### Session Identity Service

Implement `ingest_session(agent_type, agent_session_id, cwd, timestamp) -> ss_session_id`:
- Mints `ss_session_id` if new session
- Returns existing `ss_session_id` if session already ingested
- Computes `project_id` from normalized path
- Sets `degradation_level` based on available attributes

### Adapter-Level Requirements

Each agent adapter (P1-9, P1-10, P1-11) must extract and provide:
- `agent_session_id` from the agent's native format
- `cwd` from the session's working directory field
- `timestamp` from the first event

## 8. Limitations and Unsupported Scenarios

### Not Supported in This Design

- **User identity linking:** SecondSight does not currently model user identity. Two different users working on the same project would have their sessions linked by project_id. User dimension can be added later if agents expose user identifiers.
- **Cross-machine linking:** If the same project is checked out on two different machines with different absolute paths, they will appear as different projects. Git remote URL could be used as an alternative anchor, but this is deferred.
- **Session merge:** If a user resumes a session that was previously ingested as "ended," SecondSight treats the resumed portion as the same session (same agent_session_id maps to same ss_session_id). It does not create a new session.
- **Real-time cross-agent conflict detection:** If two agents are running simultaneously on the same project, SecondSight will ingest both sessions independently. It does not detect or resolve concurrent access.

### Known Risks

- **Path normalization ambiguity:** Claude Code's dash encoding is theoretically lossy for paths with literal dashes. Mitigated by reading `cwd` from JSONL content rather than decoding directory names.
- **Agent format changes:** If any agent changes its session storage format, the corresponding adapter must be updated. SecondSight's `ss_session_id` insulates the rest of the system.
- **OpenCode SQLite locking:** Reading OpenCode's SQLite database while OpenCode is writing could cause locking issues. Phase 1 should use WAL mode or read from a copy.
