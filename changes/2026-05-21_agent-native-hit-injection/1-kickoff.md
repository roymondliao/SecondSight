# Kickoff: agent-native-hit-injection

## Problem Statement

Current hit injection is rules-based: a sidecar classifier
(`src/secondsight/feedback/prompt_evaluator.py`) spawns a separate
`claude --print` subprocess on each user prompt, parses its JSON
verdict, and surfaces guidance via a curl call from a shell hook.
This shape is incompatible with both (a) sub-second hook latency
budgets and (b) the AI-era thesis that rules-based agent feedback
mechanisms rot as rules accumulate.

The redesign moves hit injection to **meta-injection (B-META)**:
the main agent — which is already in hot context with full
conversation history and tool state — self-evaluates whether a
user's prompt provides enough information to execute the requested
task, and reports back to the user in natural language when it
doesn't. No subprocess. No external classifier. No JSON envelope.

The directive layer now has two explicit injection mechanisms:
**convention inject** (per-session ambient, lives in system prompt,
governed by Change 2's lifecycle) and **hit inject** (per-prompt
transient via UserPromptSubmit meta-injection, no persistence).

## Evidence

- `bugfix/2026-05-20_user-prompt-injection-timeout/root-cause.yaml`
  profiled the existing sidecar at 11.7s / 13.6s / 16.7s Python
  wall-clock per evaluator call. haiku-4-5 TTFT under the agent's
  ~55K-token system prompt is 6–12s; no hook budget reconciles
  this with user-facing UX.
- Layer 3 of that bugfix found that the JSON parser does not strip
  markdown fences, so every real haiku verdict silently downgraded
  to `pass_open(reason="malformed_output")`. The death test at
  `tests/feedback/test_prompt_evaluator.py::test_dt_parse_evaluator_output_handles_markdown_fenced_json`
  remains intentionally RED on main as a tripwire that this change
  must clear (either by deleting the parser module, or by making
  the test green).
- `reference_opensoure/claude-code-prompt-improver/scripts/improve-prompt.py`
  ships meta-injection as <50 ms pure-Python string wrap. It is the
  architectural template; this change adapts but does not copy
  (see Damage Recipient #4 — SecondSight diverges on neutral-observer
  framing).

## Risk of Inaction

- Hit injection continues to 100% silent-fail on every prompt; the
  feature's existence is performative.
- Layer 3 RED death test stays red on `main`, normalising broken
  green-bar discipline.
- The Q1 thesis (rules-based feedback rots in AI era) goes
  untested — SecondSight stays in an architectural identity it has
  already grown past philosophically.

## Scope

### Must-Have (with death conditions)

- **#1 — Meta-injection wrapper in `scripts/hooks/user-prompt.sh`
  (or a Python helper invoked by it)**
  Replace the existing curl-to-sidecar block with a pure-Python (or
  shell) string operation that wraps the user prompt with an
  executability-self-evaluation meta-instruction and returns it via
  `hookSpecificOutput.additionalContext`. Zero subprocess. Zero
  added LLM call.
  **Death condition:** Claude Code (or other supported coding
  agent) ships native prompt self-evaluation at the platform level
  — wrapper becomes redundant → remove.

- **#5 — Analysis-layer LLM double-check review**
  When analysis aggregates behavior_flags into directives, a second
  LLM pass validates that flagged behaviours are genuine anomalies
  and not artefacts of the meta-injection wrapper's own framing.
  This is the operational instrument for invariant #4
  (distribution-shift contamination mitigation) and for Q2
  hard-stop #2 (analyzer-self-corruption-stop).
  **Death condition:** Sustained single-pass false-positive rate
  < N% over M sessions (N, M defined at planning) AND Q2 hard-stop
  #2 has never triggered → double-check becomes nice-to-have →
  remove.

- **#9 — `config.toml` hit injection toggle**
  Boolean key under `[feedback]` (e.g.
  `hit_injection_enabled = true`); default `true`.
  **Death condition:** None by design. This is the user-side
  escape hatch for Q3 cost transfer; ethical commitment, not
  functional one. Preserved permanently.

- **#11 — Delete `src/secondsight/feedback/prompt_evaluator.py`
  and the `/hook/injection/user-prompt/{agent}` endpoint in
  `src/secondsight/api/injection.py`**
  This is the visible anti-rules outcome. The sidecar pathway must
  not coexist with meta-injection — no hedging.
  **Death condition:** One-shot destructive event. If a future
  change reintroduces a sidecar classifier, the Q1 framing of this
  research chain is invalidated and must be re-litigated.

### Verify-Only (already exists; wiring confirmation only)

- **#4 — session_end trigger for analysis**
  `src/secondsight/analysis/orchestrator.py:488` already runs
  `run_lifecycle_automation` per session_end. Verify it covers
  the convention-reinforcement signal required by Change 2's
  Option (ii) loop.

- **#6 — Autonomous convention proposal**
  `src/secondsight/analysis/aggregator.py` + `feedback/lifecycle_automation.py`
  already promote patterns to directives with SUPERSEDED transitions
  on dedup. Verify trigger semantics align with the loop framing
  (no human-in-loop step in the existing pipeline).

- **#10 — Provenance metadata**
  `Directive.source_flag_type` + `source_sessions` already on
  schema (`src/secondsight/analysis/schemas.py:166-167`). Verify
  GUR-106 dashboard surfaces them — required by capability-asymmetry
  hard-stop instrument.

### Nice-to-Have

- Promote the Layer 1 (`tests/scripts/test_user_prompt_hook_injection.py::
  test_dt_user_prompt_injection_completes_within_budget_for_1500ms_endpoint`)
  death test to also cover the new meta-injection format regression.

### Explicitly Out of Scope

- Convention TTL / weight mechanics — `changes/2026-05-21_directive-lifecycle-hygiene/`.
- Capacity ceiling / eviction policy — Change 2.
- Convention auto-revision pipeline — Change 2.
- Replacement of `BehaviorFlagType` enum — its 6 values are the
  North Star anchor (see below) and assumed stable.

## North Star

```yaml
metric:
  name: "Behavior flag rate per session, decomposed by BehaviorFlagType"
  definition: >
    For each active project, rolling mean over N sessions of
    behavior_flags emitted per session, broken down by the 6
    BehaviorFlagType values (UNNECESSARY_READ, REDUNDANT_EXPLORATION,
    MISSED_SHORTCUT, REPEATED_OPERATION, WRONG_TOOL_CHOICE,
    EXCESSIVE_CONTEXT_GATHERING). All six map directly to Q1's
    "agent doing too much / wrong thing" axis.
  current: baseline TBD at planning phase
  target: trending down over consecutive sessions per project
  invalidation_condition: >
    Total flag rate trending down BUT active session count also
    trending down → user churn, not feature success. Or: flag rate
    down while directive count growing rapidly → system over-
    compensating with rules instead of agent learning.
  corruption_signature: >
    (a) High-confidence flag ratio drops while low-confidence rises
        → analyzer surfaces noise it is unsure about.
    (b) Flag rate concentrates in 1-2 flag_types while others stop
        firing → analyzer perception narrowing.
    (c) "time-to-first-agent-action" extends meaningfully → agent
        learned to delay execution to avoid corrections.

sub_metrics:
  - name: "Directive frequency distribution + status transition rates"
    definition: >
      Number of ACTIVE directives with frequency > threshold; plus
      EXPIRED, SUPERSEDED, OBSOLETE transition rates per period.
    proxy_confidence: medium
    decoupling_detection: >
      Sample-LLM evaluate active conventions for specificity
      (instruction token length × frequency joint distribution);
      if specificity falls while frequency rises → conventions
      becoming vague to game frequency.
```

## Stakeholders

- **Decision maker:** operator (yuyu_liao)
- **Impacted teams:** SecondSight users (Q3 cost-transfer #4 —
  product narrative shift from "observer" to "directiver"); Claude
  Code hook contract dependents.
- **Damage recipients:**
  - **main agent context window** (silent — wrapper occupies ~150
    tokens per prompt; long-session users feel it as earlier
    context exhaustion but cannot attribute the cause)
  - **future maintainer (incl. operator)** — emergent-loop debug
    cost exceeds deterministic-code debug cost; bug reports shift
    from "function X crashes" to "the learning trajectory drifted"
  - **early adopters who valued the "neutral observer" narrative**
    — partial silent churn possible
  - **operator (yuyu_liao)** — no-human-in-loop axiom binds
    designer to framework stewardship indefinitely; framework
    failure → designer stress test, not an approve-away decision

## Chain Provenance

- **Inherits from:**
  `bugfix/2026-05-20_user-prompt-injection-timeout/` (Phase B
  decision = B-META, rationale in root-cause.yaml).
- **Inherited invariants** (carryover, do not relitigate):
  1. Agent as human, human as agent — no human-in-the-loop approval.
  2. Rating + TTL on conventions is the implicit graduation
     mechanism (refined in Change 2: weight + Option-ii signal).
  3. Analysis fires on session_end.
  4. Analysis must include LLM double-check review (operationalised
     here as must-have #5).
  5. `config.toml` user-facing hit-injection toggle, default
     enabled, never removable.
  6. Latency budget = main-agent inference itself; no extra
     subprocess.
  7. Hit-injection judge view = main agent self-evaluating
     (executability lens, not readability lens).
  8. (Discovered during this research:) directive layer has a
     capacity ceiling — addressed by Change 2.
- **Forced-upgrade trigger** to
  `changes/2026-05-21_directive-lifecycle-hygiene/`:
  when active conventions per project sustained > N for M sessions
  (specific N, M deferred to Change 2 planning), Change 2 must be
  activated to prevent unbounded accumulation.
