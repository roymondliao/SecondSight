# Problem Autopsy: agent-native-hit-injection

## original_statement

> 現有的 hit injection mechanism 有天然的 latency 問題，雖然這個
> 問題可以被拆解到 analysis layer 而不做 runtime 的 hit injection，
> 但是因為 hit 如果還是透過 analysis result -> rules base -> grep
> or similarity 機制到 rules -> 提供 user hit，這樣的路線就是傳統
> 的規則做法，這樣做法已經證實會難以維護，且當 rules 越多，就會
> 越複雜。所以參考 reference_opensoure/claude-code-prompt-improver/
> 的設計就是一個最好的方式，而且要更相信 agent 自身的能力。在 AI
> 時代下，需要相信 agent 的能力同時也提供 agent self-evolution
> 的能力。
>
> （Operator, 2026-05-21）

## reframed_statement

Hit injection's surface symptom is latency, but the substantive
defect is that the entire sidecar-classifier architecture is an
expression of rules-based agent feedback — a class of mechanism
whose maintenance complexity scales with rule count, and that the
AI era specifically invalidates. The redesign rejects rules-based
feedback wholesale (not merely faster rules) and reframes hit
injection as an **executability self-evaluation by the executor
itself**: the main agent — already in hot context — judges whether
the current prompt provides enough information to execute the
requested task, and surfaces back to the user when it doesn't.
This is structurally identical to
`reference_opensoure/claude-code-prompt-improver/`, with the explicit
addition of (a) closed-loop self-evolution via the directive layer
and (b) anti-rules architectural commitment manifested as the deletion
of `src/secondsight/feedback/prompt_evaluator.py`.

## translation_delta

```yaml
translation_delta:
  - original: "hit injection 有天然的 latency 問題"
    reframed: "hit injection sidecar-classifier architecture is rules-based and incompatible with AI-era feedback design"
    delta: >
      Surface "latency" framing was a symptom; the root framing
      is architectural class (rules-based vs agent-native), not
      timing. Without this reframe, Phase A's "raise budget"
      response would have seemed sufficient — it never was.

  - original: "分析路線 -> rules base -> grep or similarity 到 rules"
    reframed: "moving to async + rules-DB matching is still rules-based and still rots"
    delta: >
      Operator pre-emptively rejected an obvious "fix" (async +
      rules lookup) before it could be proposed, anchoring the
      Q1 framing on a forward-looking architectural commitment
      rather than a backward-looking critique.

  - original: "在 AI 時代下，需要相信 agent 的能力同時也提供 agent self-evolution 的能力"
    reframed: "trust agent + give agent self-evolution = no human-in-the-loop, no rules-DB, the system improves as the agent improves"
    delta: >
      Elevates "trust agent" from a UX preference to a system-
      level axiom. Pulls self-evolution from "ambitious future"
      into "necessary corollary of trusting agent". This is what
      makes "Agent as human, human as agent" a load-bearing
      invariant rather than a marketing line.
```

## interrogation_record

### Q1 — problem shape provenance

The problem was reshaped over six stages: operator probe →
production bug → debugging (3-layer rot) → operator reframe
(agent-perspective) → dialogue (closed-loop framing) → operator
affirmation (Agent-as-human axiom). Every stage was internal —
no external stakeholder pulled scope outward. This is reshape via
internal architectural reflection, not market demand.

### Q2 — kill conditions

```yaml
kill_conditions:
  hard_stops:
    - id: capability-asymmetry-stop
      cause: "LLM understanding outruns user; conventions lose auditability"
      trigger: >
        Diagnostic shows user cannot articulate the provenance of
        active conventions for > N% of cases (N defined at planning).
      action: feature defaults to opt-in (not opt-out) for this user instance

    - id: analyzer-self-corruption-stop
      cause: "Analyzer itself biased; conventions become contaminated"
      trigger: >
        Invariant #4 LLM double-check (must-have #5) shows sustained
        disagreement rate > X% over N sessions.
      action: pause convention proposal pipeline; existing conventions enter frozen state

  known_blindspots:
    - id: user-contribution-blindness
      cause: "user unaware their own prompts are a source of agent drift"
      why_blind: >
        Convention origin is traceable, but user's own cognitive
        blindness is not observable from system side.
      mitigation: >
        Convention dashboard shows provenance ('originated from your
        prompts in sessions X, Y, Z'); cannot force user to see it.

    - id: user-self-denial
      cause: "user dismisses correct analysis output (ego-syntonic preservation)"
      why_blind: >
        System cannot distinguish 'user correctly rejected bad
        recommendation' from 'user incorrectly rejected good
        recommendation'.
      mitigation: >
        None at system level. Named here as structural limit of any
        self-evolution loop, not a defect.

  risk_transfer_markers:
    - id: cross-context-memory-leakage
      cause: "coding agent's own memory / external memory layer leaking across projects"
      scope: out-of-bounds (not SecondSight's responsibility)
      mitigation: >
        Documentation must state that SecondSight project-scoped
        directives do not guarantee the agent's underlying memory
        layer is also project-scoped.

  no_explicit_stop_for: "authorship dilution at large; operator chose 'known risk, monitored, no committed disable trigger' beyond the two hard stops above"
```

## damage_recipients

```yaml
damage_recipients:
  - who: "main agent context window"
    cost: "meta-injection wrapper occupies ~150 tokens per user prompt; long-session / large-repo tasks reach context exhaustion earlier"
    silent: true
    affected_users: "long-session users; large refactor tasks"

  - who: "future maintainer (incl. operator)"
    cost: "emergent-loop debug cost > deterministic code debug cost; bug reports change from 'function X' to 'learning trajectory drifted'"
    silent: false
    surfacing_channel: "bug reports about 'weird conventions'"

  - who: "early adopters who valued the 'neutral observer' product narrative"
    cost: "positioning shift to 'observer + directiver'; some may dislike that the tool now writes its own conventions"
    silent: partial
    surfacing_channel: "silent churn possible without explicit complaint"

  - who: "operator (yuyu_liao)"
    cost: "no-human-in-loop axiom binds designer to framework stewardship indefinitely; failure of the loop is a designer stress test, not a roll-back decision"
    silent: false
    chosen: >
      Operator explicitly owns this cost. Framework's mortality is
      tied to designer stamina; if stamina exhausts, feature must
      sunset rather than be inherited unowned.

rejected_candidates:
  - id: secondsight-observability-signal-purity
    reason: >
      Operator earlier framed wrapper-in-events as training signal
      for self-evolution, not pollution. Calling it pollution at
      Q3 would be self-contradiction with that framing.
```

## observable_done_state

1. **Hook layer + Anti-rules:** `~/.secondsight/logs/curl-errors.log`
   no longer accumulates hit-injection timeout entries (because
   B-META never makes the curl call); **simultaneously**
   `src/secondsight/feedback/prompt_evaluator.py` and
   `/hook/injection/user-prompt/{agent}` are removed from the
   codebase (contingent on planning phase confirming the module is
   fully redundant). The Layer 3 RED death test is either deleted
   (alongside the parser) or turns GREEN (with fence-stripping if a
   thin parsing layer survives).

2. **Directive layer:** After N sessions of accumulated usage, the
   directive dashboard surfaces both user-authored AND
   auto-generated conventions, each with provenance metadata
   (`source_flag_type`, `source_sessions`) and lifecycle state
   visible. When the directive count touches the capacity ceiling
   (Change 2 territory), LRU-by-weight eviction is observable in
   the lifecycle audit log, but the "mixed shape" of authored +
   auto-generated remains intact.

3. **UX layer (structural blindspot, intentionally named):** No
   directly observable signal at the user-facing surface — the
   loop's effect on agent execution quality can only be inferred
   via analysis-layer session-end review of the North Star
   metric. This is a structural limit of the closed-loop design;
   recorded here as known-blindspot rather than a defect to be
   fixed.

## scope_rationale_notes

Component triage (full table in `1-kickoff.md`); summary of decision
logic captured here for future readers:

- **#1, #5, #9, #11** are genuinely new work in this change.
- **#4, #6, #10** are *already implemented* in `analysis/aggregator.py`,
  `feedback/lifecycle_automation.py`, `analysis/schemas.py`, and
  `analysis/orchestrator.py`. The earlier draft scope (which
  proposed building them) was an artefact of the dialogue running
  ahead of a codebase audit. The dialogue's "Agent as collaborator"
  pattern produced a "design phantom" — imagined system from
  ambition rather than from ground truth. The phantom was caught
  by operator's instinct to demand "先參考目前 analysis layer 的
  設計" — recorded here as a feedback pattern for future research
  iterations.
- **Cluster C (#7, #8)** moved to Change 2 by operator decision;
  Change 1 ships with no convention lifecycle hygiene, relying on
  `forced_upgrade_trigger` to schedule Change 2 by observable
  conditions rather than calendar.
