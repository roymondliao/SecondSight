# Kickoff: Memory Palace — Human Model Layer

## Problem Statement

Coding agents operate in two modes that share the same alignment problem. In **interactive mode**, the agent's primary context source is the "first citizens" file (CLAUDE.md / AGENTS.md), which may be poorly maintained, conflict with runtime instructions, or be forgotten mid-session due to context window constraints. In **auto mode**, the agent has only files and the initial instruction — no user intervention is possible, and deviation from user expectations accumulates silently until review. Both modes suffer from the same root cause: the agent has no living model of the specific human it is collaborating with.

## Evidence

- First citizens files are static and manually maintained. Most users do not maintain them consistently across project evolution.
- SecondSight already captures all hook events from agent sessions, including user instructions — but currently analyzes only agent behavior, not human patterns.
- Auto mode deviation is discovered only at output review, with no mid-course correction mechanism.
- The "Agent as human, human as agent" design principle implies the agent should build the same kind of working understanding a human teammate would develop over repeated collaboration.

## Risk of Inaction

In interactive mode: users continue to spend per-session cognitive overhead re-explaining context, preferences, and working style that should already be known. The agent's context quality degrades proportionally to how poorly the first citizens file is maintained.

In auto mode: without a user model, the agent's only alignment signal is the initial task instruction. Any gap between what the user said and what they meant surfaces only after execution — too late for correction without restart.

## Scope

### Must-Have (with death conditions)

- **Human behavior observation extraction** — Extend session analysis to extract user patterns: tech stack preferences, instruction style, thinking approach, response preferences, tool/skill usage. Death condition: If extraction accuracy cannot exceed a measurable baseline (TBD after pilot), degrade to "record only, do not inject."
- **User model storage (memory palace structure)** — File-based, hierarchical per-project storage under `~/.secondsight/projects/<project_id>/user_model/`. Each "palace" is a context domain (e.g., debugging style, architecture preferences). Death condition: If maintaining the file structure creates more overhead than it saves (measurable via user feedback), collapse to a single flat profile.
- **Signal-triggered injection at session start** — At session start (via hook), detect task context and load the relevant palace subset into the agent's context. Death condition: If false-trigger rate (wrong palace loaded) exceeds a threshold that degrades session quality, fall back to full-model injection or no injection.

### Nice-to-Have

- User-visible and user-editable palace contents (view/override auto-extracted knowledge)
- Confidence scoring per extracted pattern (low-confidence patterns are injected with lower priority)

### Explicitly Out of Scope

- DB / RAG / vector embeddings (file-based only)
- Cross-project user model sharing
- Multi-user project isolation (single-user MVP)
- Replacing first citizens files (supplement, not replace)

## North Star

```yaml
metric:
  name: "Session explanation overhead"
  definition: "Ratio of user messages that re-explain context or preferences the agent should already know, measured as a proportion of total user messages per session"
  current: unknown — requires baseline measurement from existing session data
  target: 30% reduction from baseline after memory palace is active for >= 5 sessions
  invalidation_condition: "User reports agent is doing unwanted things it wasn't asked to do — over-fitted model is worse than no model"
  corruption_signature: "Explanation overhead metric drops, but task rejection or correction rate rises — agent is acting on wrong knowledge silently"

sub_metrics:
  - name: "Palace hit rate"
    definition: "Proportion of sessions where the triggered palace was the correct one (validated by session outcome)"
    proxy_confidence: medium
    decoupling_detection: "High hit rate but user still issuing correction instructions → palace content is stale or wrong, not the trigger"

  - name: "User model staleness"
    definition: "Number of sessions since last palace update"
    proxy_confidence: low
    decoupling_detection: "Model not updated after N sessions despite user behavior change → extraction pipeline silently failing"
```

## Stakeholders

- **Decision maker:** yuyu_liao (project owner)
- **Impacted systems:** Session analysis pipeline (must extract human patterns in addition to agent patterns), hook injection system (must support palace-triggered context loading)
- **Damage recipients:** Analysis pipeline complexity doubles; user privacy risk (behavioral patterns stored as files — must be local-only, never synced)
