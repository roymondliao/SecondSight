# Problem Autopsy: Memory Palace — Human Model Layer

## original_statement

「SecondSight 是讓 agent 可以更好的跟 human co-work，就像團隊配合。在 interactive mode 下，first citizens file 不一定每個 user 都會很好的去維護，會有 user 指令跟內容是衝突，而且 first citizens file 是放到 system prompt，會有遺忘風險。在 auto mode 下，agent 只能從 files 跟最初的 user 給予的指令來了解該如何處理 task，這過程中不會有任何 user 干預，最終 user 的期望跟 agent 的產出無法 alignment，而產生偏差。SecondSight 引進這新 feature 是想要解決這兩個問題，不需要一定要 100% alignment，但可以更接近 alignment。」

## reframed_statement

Build an automatically-maintained user model derived from session observation. The model captures the human's working patterns — tech stack preferences, instruction style, thinking approach, response preferences — and stores them as a hierarchical file structure (memory palace). At session start, a signal-triggered mechanism loads the contextually-relevant subset of the model into the agent's context, supplementing the first citizens file without replacing it. This reduces both interactive-mode explanation overhead and auto-mode deviation, in both cases by giving the agent pre-loaded knowledge of this specific human before any instruction is issued.

## translation_delta

```yaml
translation_delta:
  - original: "讓 agent 可以更好的跟 human co-work，就像團隊配合"
    reframed: "agent builds a persistent, evolving model of the human — the same implicit knowledge a human teammate accumulates over repeated collaboration"
    delta: "Original is aspirational; reframed makes it mechanistic. The mechanism is observation-derived knowledge, not just better tooling."

  - original: "first citizens file 不一定每個 user 都會很好的去維護"
    reframed: "the alignment gap scales inversely with how well the user maintains their first citizens file — users who maintain it well see little benefit; users who don't maintain it see the most"
    delta: "Original frames it as a user discipline problem; reframed reveals it is also a system design problem — the agent should not depend entirely on user-maintained files."

  - original: "first citizens file 是放到 system prompt，會有遺忘風險"
    reframed: "long-session forgetting is a symptom of a deeper problem: the knowledge is not durable across sessions, and the agent re-derives it from scratch each time"
    delta: "Forgetting is not just a context-window problem — it is a persistence problem. Memory palace solves persistence, not context window size."

  - original: "auto mode 下，最終 user 的期望跟 agent 的產出無法 alignment"
    reframed: "auto mode deviation is fundamentally a cold-start problem: agent begins each task with no model of the human, relying solely on the task instruction and project files"
    delta: "Original describes the symptom (deviation); reframed names the cause (cold start with no user model)."

  - original: "不需要一定要 100% alignment，但可以更接近 alignment"
    reframed: "target is measurable reduction in explanation overhead and correction rate, not perfect alignment — perfect alignment would require 100% accurate inference which is not achievable"
    delta: "Crucial scope limiter: this is an improvement problem, not a correctness problem. The success bar is directional progress, not a hard threshold."
```

## kill_conditions

```yaml
kill_conditions:
  - condition: "Extracted user model inference quality is systematically low — the patterns extracted do not represent the user's actual preferences"
    rationale: "Injecting wrong context is worse than injecting nothing. An agent that acts on incorrect user knowledge does unwanted things confidently. This is harder to detect than an agent that asks clarifying questions."

  - condition: "The signal-to-palace mapping cannot reliably detect task context — wrong palace is triggered more often than right palace"
    rationale: "In auto mode especially, a bad context injection has no correction mechanism. The deviation introduced by wrong palace loading may exceed the deviation the feature was meant to reduce."

  - condition: "Cold start is never solved — users do not generate enough sessions to build a meaningful model before the project's nature changes"
    rationale: "A user model built on 2 sessions of data may be worse than no model. If the minimum viable observation window exceeds the typical project lifecycle, the feature has no real deployment surface."

  - condition: "Analysis pipeline complexity doubles but session analysis latency becomes unacceptable"
    rationale: "SecondSight's value depends on low-friction observation. If adding human-pattern extraction makes analysis slow enough that users disable it, both the new feature and the existing directives system are damaged."
```

## damage_recipients

```yaml
damage_recipients:
  - who: "User (privacy)"
    cost: "Behavioral patterns, thinking style, and instruction habits are extracted and stored as readable files. If ~/.secondsight/ is ever synced to cloud or shared, this constitutes unintended behavioral profiling."

  - who: "User (over-fit risk)"
    cost: "A user whose patterns change (new project type, new collaborator, growth in expertise) will have a stale user model that actively misleads the agent. The user may not know the model is wrong until they notice the agent making wrong assumptions."

  - who: "Analysis pipeline"
    cost: "Session analysis currently has one extraction target (agent behavior). Adding a second target (human behavior) increases prompt complexity, output schema surface, and failure modes. Each analysis run now has two ways to produce garbage output."

  - who: "Agent (injection budget)"
    cost: "Every token used for palace injection is a token not available for task context. In long auto-mode sessions with large codebases, the injection may compete with file context for limited context window space."
```

## observable_done_state

In interactive mode, user instructions across sessions become progressively shorter as context no longer needs to be re-established. In auto mode, the agent's initial approach to a task matches the user's expected approach without explicit specification, reducing correction cycles after output review. The measurable signal: "explanation overhead" per session decreases over the first N sessions after the user model is active.
