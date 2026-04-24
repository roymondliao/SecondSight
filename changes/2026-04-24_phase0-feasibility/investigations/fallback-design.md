# Fallback Design: SecondSight Directive Injection

**Date:** 2026-04-24
**Task:** P0-8 — Fallback Design When Injection Infeasible
**Depends on:** Task 4 (injection-feasibility.yaml), Task 6 (directive-comprehension.yaml)
**Phase:** Phase 0 — Protocol-level design (not tested against live agents)

---

## Purpose

This document answers: if SecondSight cannot inject directives at runtime, what does the product become? At what fallback level does SecondSight lose its market differentiation? And when should the team choose each fallback level?

The answer to the third question is the most load-bearing output of this document. A fallback design that does not specify decision criteria is not a design — it is a list of options the team will never explicitly choose between.

---

## Step 1: Injection Path Catalog (from Task 4)

### What Task 4 Found

| Agent | Path | Type | Verdict | Verified? |
|-------|------|------|---------|-----------|
| Claude Code | CLAUDE.md modification | session_start | viable | Yes (official docs + ref source) |
| Claude Code | settings.json system_prompt | session_start | viable | Yes (official docs) |
| Claude Code | MCP tool server | runtime (pull-based) | partially_viable | Protocol confirmed; config steps unprototyped |
| Claude Code | PreToolUse/PostToolUse hooks | runtime | not_viable | Yes (hook contract verified) |
| Claude Code | JSONL transcript append | indirect | partially_viable | High stability risk; not recommended |
| OpenCode | Config file system prompt | session_start | partially_viable | UNVERIFIED — path inferred, not confirmed from official docs |
| OpenCode | AGENTS.md / instruction file | session_start | partially_viable | UNVERIFIED — OpenCode-specific file support not confirmed |
| OpenCode | MCP tool server | runtime (pull-based) | partially_viable | Protocol confirmed; OpenCode MCP impl details unverified |
| OpenCode | Direct SQLite write | runtime | not_viable | Yes (WAL corruption risk; lazyagent uses read-only) |
| Codex | AGENTS.md in project directory | session_start | viable | Partially — AGENTS.md is OpenAI ecosystem standard; exact Codex 1.x behavior unverified |
| Codex | ~/.codex/instructions.md | session_start | viable | Yes (official Codex docs) |
| Codex | --instructions CLI flag | session_start | viable | Yes (official Codex docs) |
| Codex | JSONL transcript append | indirect | not_viable | Yes (confirmed not viable) |

### Key Conclusion from Task 4

**True runtime push-based injection is not achievable for any agent via documented, supported mechanisms.**

What exists at runtime is pull-based: Claude Code and OpenCode can call a SecondSight MCP tool if configured to do so. This means SecondSight cannot push directives mid-session. It can only make directives available for the agent to fetch — and only if the agent decides to call the tool.

Session-start injection is viable for all three agents. The primary cycle is:

```
Analyze session N outputs → Generate directives → Write to CLAUDE.md / AGENTS.md / config
→ Session N+1 begins with directives in context
```

This introduces a **one-session delay** between observing a problem and the agent receiving a correction. A behavior observed in session N is not corrected until session N+1 at the earliest. This is the primary functional gap between runtime injection and session-start-only mode.

---

## Step 2: Fallback Hierarchy — What SecondSight CAN and CANNOT Do

### Fallback Level 1 (FB-1): Runtime Injection — Full Capability

**Condition:** MCP pull-based runtime injection is viable AND directive comprehension >= 50%. (The 50% threshold is an unvalidated assumption — chosen as the midpoint above which more directives are followed than ignored. Phase 1 must determine whether this threshold produces useful behavioral change.)

**What SecondSight CAN do:**
- Observe tool calls, file reads, verification commands in real time
- Analyze patterns mid-session and surface current-session directives via MCP tool
- Deliver feedback within the active session if the agent calls the MCP tool
- Persist session-level behavior improvements for future sessions via session-start injection
- Run the full feedback loop: observe → analyze → direct → re-observe within one session

**What SecondSight CANNOT do (even at FB-1):**
- Push directives to agents without agent-initiated MCP tool calls (not push-based)
- Guarantee directive compliance — agent may ignore injected content (DC-2 from Task 4)
- Guarantee directives survive context compaction in long sessions (DC-1 from Task 4)

**PRD features available:**
- Phase 1: Full observation + event schema (unaffected by injection level)
- Phase 2: Full analysis layer (unaffected by injection level)
- Phase 3A: Directive generation + session-start injection (full)
- Phase 3A: MCP runtime delivery (conditional on agent calling the tool)
- Phase 3B: Cross-session learning loop (full — directives accumulate across sessions)

**Assumption:** FB-1 requires MCP integration to be set up and the agent to be prompted or configured to call the SecondSight MCP tool. This has not been prototyped. The "partially_viable" verdict from Task 4 applies. This level is aspirational for Phase 3A delivery — it requires Phase 1 and Phase 2 to be complete first.

---

### Fallback Level 2 (FB-2): Session-Start Injection Only — Post-Run Feedback Loop

**Condition:** Runtime injection is infeasible or unreliable AND session-start injection remains viable.

This is the baseline achievable architecture given current Task 4 findings. Session-start paths are verified for Claude Code and Codex. **OpenCode's session-start path is UNVERIFIED** — the config injection mechanism was inferred from architecture, not confirmed from official documentation. FB-2 viability for OpenCode is conditional on Phase 1 verification of this path.

**What SecondSight CAN do:**
- Observe all tool calls and events from session N (via hooks)
- Analyze session N to identify waste patterns (over-reading, redundant verification, scope drift)
- Generate directives targeting identified patterns
- Write directives to CLAUDE.md / AGENTS.md / config before session N+1 starts
- Deliver a post-run analysis report (CLI or file output) after each session
- Accumulate directive history across sessions (cross-session learning)

**What SecondSight CANNOT do at FB-2:**
- Correct agent behavior within an active session — no mid-session feedback
- React to agent mistakes as they happen — only after session completes
- Provide real-time guidance on decision branches the agent is currently making
- Guarantee that directives injected at session start survive context compaction in long sessions (DC-1 unresolved)

**Phase 3 features lost or degraded at FB-2:**

| Feature | Status at FB-2 | Notes |
|---------|---------------|-------|
| Real-time waste detection | LOST | Requires runtime injection or MCP pull |
| Mid-session directive delivery | LOST | Session boundary blocks delivery |
| MCP-based runtime advisor | LOST | Runtime pull-based — cannot inject at session-start only level |
| Post-session directive injection | AVAILABLE | Core feature, unchanged |
| Session-to-session learning | AVAILABLE | Unaffected by injection timing |
| Waste pattern analysis | AVAILABLE | Unaffected (observation + analysis layer) |
| Cross-session directive accumulation | AVAILABLE | Unaffected |

**Directive lifecycle at FB-2:**

```
[Session N]
  Agent runs → hooks capture tool events → events written to storage
  Session ends

[SecondSight Analysis]
  Load session N events → run analysis pipeline → score waste patterns
  → generate directives targeting top patterns

[Pre-Session N+1]
  Write directives to CLAUDE.md (Claude Code) or AGENTS.md (Codex)
  or OpenCode config (UNVERIFIED path — may fail silently)

[Session N+1]
  Agent reads directives at session start → (if compliant) applies directives
```

**One-session delay implication:** If an agent makes a costly mistake in session N (e.g., re-reads the same file 20 times), the directive to reduce redundant reads arrives in session N+1. The mistake in session N is not corrected. This is acceptable for learning loops but unacceptable for tasks where session N's behavior has immediate consequences (e.g., destructive operations, irreversible changes). The team must evaluate whether the target workflow tolerates one-session delay.

**Market position at FB-2:**
- SecondSight is still differentiated from LangSmith, Langfuse, and OpenTelemetry: those tools observe but do not generate and inject behavioral directives. The feedback loop exists — it is simply asynchronous (post-session) rather than real-time.
- Differentiation is weaker than FB-1 but still present. The question is whether users value post-session feedback loops. This is an assumption that requires Phase 1 user validation.

---

### Fallback Level 3 (FB-3): No Injection — Observation + Analysis Only

**Condition:** Session-start injection is also infeasible (e.g., all paths fail verification, or directive comprehension is below 30% making injection futile).

**What SecondSight CAN do:**
- Observe all tool calls and events from agent sessions
- Analyze sessions to identify waste patterns
- Produce reports: per-session analysis, trend reports, waste breakdown
- Deliver findings via external channel: CLI report, file output, dashboard (future), human review

**What SecondSight CANNOT do at FB-3:**
- Influence agent behavior directly (no injection path)
- Automate the feedback loop — requires human to read the report and act on it
- Close the loop without a human intermediary

**Alternative delivery mechanisms at FB-3:**
- **CLI report:** `secondsight report --session <id>` prints a structured analysis with recommended directives that a developer can manually paste into CLAUDE.md or AGENTS.md
- **File output:** SecondSight writes a `secondsight-recommendations.md` file that a developer can review and merge into project instruction files
- **Dashboard:** (deferred to Phase 3B) A web view of session analysis and trends, readable by framework maintainers and tool architects
- **Human review gate:** SecondSight flags sessions with high waste scores; a human reviewer approves or rejects the generated directive before it is applied

**Phase 3 features lost at FB-3:**

| Feature | Status at FB-3 | Notes |
|---------|---------------|-------|
| Automated directive injection | LOST | Human must apply manually |
| Closed feedback loop | LOST | Loop requires human intermediary |
| Real-time feedback | LOST | Not feasible without injection |
| Post-session automated feedback | LOST | Cannot auto-write to config files safely |
| Observation + analysis | AVAILABLE | Core, unaffected |
| Waste pattern detection | AVAILABLE | Unaffected |
| Session trend reports | AVAILABLE | Available at all levels |

**Product viability at FB-3 — market differentiation assessment:**

At FB-3, SecondSight competes directly with established observation tools:

- **LangSmith** (LangChain): observation, tracing, evaluation. Provides analysis. No automated directive injection. Target: LangChain users.
- **Langfuse**: open-source observability. Tracing, scoring, analytics. No behavioral directive feedback. Target: production LLM app teams.
- **OpenTelemetry with LLM semantic conventions**: standardized tracing. No analysis or feedback. Target: infrastructure-heavy teams.
- **Google Cloud Agent Optimizer** (announced 2026): closest alternative — provides recommendations but requires manual application and does not target CLI coding agents.

**At FB-3, SecondSight's differentiation is narrow.** Remaining differentiation is:
1. Specific focus on CLI coding agents (Claude Code, OpenCode, Codex) vs production apps
2. Directive generation output format tailored to agent instruction files (CLAUDE.md / AGENTS.md style)
3. Cross-session learning and pattern accumulation (vs single-session analysis in competing tools)

This is differentiated enough to build a niche product but not the transformative "closed-loop" positioning from the market analysis. FB-3 is not the intended product. It is an acceptable interim state if injection is being developed, but it should not be the final product architecture.

**Decision to avoid FB-3 as permanent state:** If Phase 1 testing confirms that directive comprehension is below 30% for all three agents and no injection path works reliably, the team should re-evaluate whether the full Phase 3 investment is justified. FB-3 as permanent architecture requires re-pricing the investment against the narrower differentiation.

---

## Step 3: Session-Start-Only Directive Lifecycle (FB-2 Detail)

Defined in the lifecycle diagram in Step 2 above. Key lifecycle properties:

**Generation timing:** SecondSight generates directives immediately after session N completes. If the analysis pipeline takes > 5 minutes, directives may not be ready before session N+1 starts if sessions are run in rapid succession. This is an assumption: the analysis pipeline completes before the next session begins.

**Injection atomicity:** The write to CLAUDE.md or AGENTS.md is a file overwrite. If this fails (disk error, permissions issue, race condition with concurrent agent start), the directive is silently dropped. No injection confirmation mechanism exists at session-start level.

**Directive merge strategy (skeletal design):** Each analysis cycle may add, update, or retire directives. Writing new directives should not blindly overwrite previous ones and should not append indefinitely. The following merge strategy is proposed for Phase 3A implementation:

- SecondSight maintains a `# SecondSight Directives` section in CLAUDE.md/AGENTS.md, delimited by `<!-- secondsight:start -->` and `<!-- secondsight:end -->` markers.
- Content outside these markers (user-written content) is never modified.
- At each injection cycle, SecondSight rewrites only the content between its markers.
- Directives carry a `generated_at` timestamp and a `session_ref` pointing to the session that generated them.
- Directives older than N sessions (configurable, default 5) are expired and removed from the section. (Note: no direct confirmation signal exists — expiration is time-based only. Phase 3B behavioral monitoring may provide a behavioral proxy for directive effectiveness, but this is not available at FB-2 launch.)
- If the marker section does not exist in CLAUDE.md, SecondSight appends it at the end of the file.

This strategy requires Phase 3A to implement a marker-based file editor. It is not prototyped in Phase 0 but the design is specific enough to implement. Risk: if the user edits content inside the markers, SecondSight will overwrite those edits at the next injection cycle. This must be documented for users.

**Injection confirmation gap:** No direct confirmation signal exists. The only observable proxy is behavioral: if session N+1 shows the waste pattern being targeted by the directive has reduced, the directive was likely followed. If the pattern does not change, either the injection failed or the agent ignored the directive (DC-2). SecondSight's Phase 3B cross-session learning must track directive effectiveness via behavioral change, not delivery confirmation.

**Scope:** CLAUDE.md modification can be at global (~/.claude/CLAUDE.md), project, or subdirectory level. SecondSight must decide the target scope. Project-level injection is safest (avoids affecting other projects) but requires identifying the correct project directory.

---

## Step 4: Alternative Delivery for No-Injection Mode (FB-3 Detail)

At FB-3, the deliverable is a structured report rather than an injected directive. SecondSight produces a machine-readable report (YAML or JSON) and a human-readable summary (Markdown). The report is written to:

- **`secondsight-report.md`** in the project directory: human-readable summary, top waste patterns, recommended directives for manual copy-paste
- **`secondsight-recommendations.md`**: ready-to-paste directives formatted as CLAUDE.md or AGENTS.md sections, for easy manual application by a developer
- **CLI output:** `secondsight analyze` prints the summary to stdout, readable in terminal

**Report format (skeletal spec):** `secondsight-recommendations.md` is formatted as a valid CLAUDE.md/AGENTS.md section so that a developer can apply it by copy-paste with no reformatting:

```markdown
# SecondSight Recommendations (session: <session_id>, generated: <timestamp>)
<!-- Apply by copying the section below into your CLAUDE.md or AGENTS.md -->

## SecondSight Behavioral Directives

- When you have read the same file more than 2 times in this session, check your
  notes before reading again. (Confidence: high — 18 repeat reads observed in
  session <session_id>)

- Before making changes outside the explicitly requested file scope, ask the user
  whether those changes are wanted. (Confidence: medium — 3 divergent actions
  observed)
```

This format is immediately actionable and requires no further processing by the user. Each directive includes a confidence label and the evidence that generated it.

**Human review viability assessment:** The target users (framework maintainers, tool architects, developers running Claude Code or Codex as part of their workflow) are capable of reading and applying these recommendations manually. This is a reasonable interim workflow. However, it adds friction that the automated path removes. For end users who are not developers, human review is not viable — but those users are not the Phase 3 target.

---

## Step 5: Product Viability Assessment per Fallback Level

| Fallback Level | Differentiation vs Observation Tools | Viability Assessment |
|---------------|--------------------------------------|---------------------|
| FB-1 (Runtime injection via MCP) | Strong — closed-loop real-time feedback | Viable product (Phase 3A goal) — assumption: MCP compliance > 50% |
| FB-2 (Session-start injection only) | Moderate — post-session feedback loop, automated injection | Viable product — differentiated by automation even if delayed |
| FB-3 (Observation + analysis only) | Narrow — niche focus and directive format are differentiators | Viable niche product; does NOT achieve closed-loop positioning |

**Market differentiation threshold:** SecondSight loses its core positioning at FB-3 permanent state. The feedback loop is the product. If the feedback loop requires manual intervention (human review + copy-paste), SecondSight becomes a workflow tool, not an automated agent optimizer.

**FB-2 is the minimum acceptable level for the intended product.** The Phase 3 investment is justified at FB-2. At FB-3 permanent, the team must reassess scope and investment.

---

## Step 6: Decision Criteria — When to Choose Each Fallback Level

### Criterion for FB-1 (Runtime/MCP Path)

Adopt FB-1 if all of the following are true after Phase 1 testing:
- MCP server setup is verifiable for at least two agents (Claude Code + one other)
- Phase 1 experiments show the agent calls the MCP tool at least once per session in test scenarios
- Phase 1 directive comprehension (from Task 6 protocol) shows >= 50% compliance rate for natural language directives
- No evidence that MCP tool calls are suppressed or ignored in production-length sessions

If MCP compliance is unverifiable or < 50%, do not invest in FB-1 runtime path. Fall to FB-2.

### Criterion for FB-2 (Session-Start Only)

Adopt FB-2 if:
- Session-start injection paths are confirmed viable for at least two agents (Task 4 verdicts hold)
- Directive comprehension is between 30% and 50% (or above 50% in session-start-only conditions)
- Users in Phase 1 testing indicate post-session feedback loops provide value (qualitative signal)

FB-2 is the current best-evidence path given Task 4 findings. Unless Phase 1 testing shows session-start injection fails or comprehension is below 30%, FB-2 should be the default architecture assumption going into Phase 2.

### Criterion for FB-3 (Observation + Analysis Only)

Fall to FB-3 if:
- Session-start injection fails for all three agents in Phase 1 testing (file writes succeed but behavioral compliance is 0%)
- Directive comprehension is below 30% for all agents and all phrasing styles
- MCP integration fails for all agents (not configurable or agent refuses to call tool)

FB-3 is a temporary operational mode, not a product architecture. If the team reaches FB-3, it means Phase 3A must be redesigned before launch.

### Compliance Threshold Scenario: 30-50% Comprehension

The acceptance criteria require explicit coverage of this intermediate zone.

At 30-50% compliance, the feedback loop produces a mixed signal:
- Some directives are followed — waste reduction is real but partial
- Other directives are ignored — noise in the signal
- The agent may follow directive A and ignore directive B in the same session, producing inconsistent behavior

**What SecondSight does at 30-50% compliance:**
1. Prioritize the highest-confidence directives (those with clearest trigger conditions and evidence from Task 6 protocol)
2. Reduce directive volume — send 2-3 high-priority directives instead of 10, reducing noise
3. Track which directive types have higher compliance rates (from Phase 1 experiment data) and preferentially generate those
4. Accept that Phase 3A delivers partial value, not guaranteed waste elimination
5. Continue gathering compliance data in Phase 1 to determine if 30-50% stabilizes or trends upward

At 30-50% compliance, FB-2 remains the viable operating mode. Do not fall to FB-3 based on compliance alone — 30-50% partial feedback is still differentiated from zero feedback (which is what FB-3 delivers). The condition for falling to FB-3 is compliance below 30% (making the feedback loop unreliable) combined with injection path failures.

---

## Step 7: Phase-by-Phase Impact

### Phase 1: Observation Layer

Phase 1 is entirely about hooking into agents and capturing events. **Injection level has no impact on Phase 1.** All three fallback levels require Phase 1 to be complete. Phase 1 produces the event data that enables all downstream analysis.

**Phase 1 is not affected by fallback level choice.**

### Phase 2: Analysis Layer

Phase 2 builds the pipeline that classifies actions, identifies waste patterns, and generates directive candidates. **Injection level has minimal impact on Phase 2.** The analysis pipeline is identical whether directives are ultimately injected at runtime, session-start, or handed to a human reviewer.

**One Phase 2 variation at FB-3:** The analysis pipeline must produce human-readable output in addition to machine-structured directives. This adds presentation work but does not change the analysis logic.

**Phase 2 is not significantly affected by fallback level choice, except for FB-3 requiring human-readable report format.**

### Phase 3A: Directive Delivery (Feedback Layer)

This is where fallback level has maximum impact.

| Feature | FB-1 | FB-2 | FB-3 |
|---------|------|------|------|
| Directive generation | Full | Full | Full |
| CLAUDE.md / AGENTS.md writer | Full | Full | CLI output only (human-applied) |
| MCP tool server | Full | Not implemented | Not implemented |
| Session-start injection pipeline | Full | Full | Removed |
| Real-time feedback within session | Partial (MCP pull) | Not available | Not available |
| Injection confirmation / verification | Not designed at any level | Not designed | N/A |

**Phase 3A delivery scope at FB-2 is estimated at roughly 70-80% of FB-1 scope** (unvalidated estimate — not backed by feature-level count or story points; use as orientation only). The missing portion is the MCP runtime delivery path.

**Phase 3A delivery scope at FB-3 is estimated at roughly 40-50% of FB-1 scope** (same caveat — rough orientation figure, not a planning input). The core directive injection pipeline is removed; only analysis + report generation remains.

### Phase 3B: Cross-Session Learning

Phase 3B accumulates directives across sessions, learns which directives work for which agents and codebases, and refines the directive generation model.

**FB-2 supports Phase 3B fully.** Session-start injection provides a closed (if delayed) loop for cross-session learning.

**FB-3 partially supports Phase 3B.** The learning pipeline still runs on observation data. But if directives are never applied (because there is no injection), the learning loop has no signal for what actually changed behavior. Phase 3B at FB-3 becomes "analysis of what we would have recommended" rather than "learning from what actually worked." This is a significant reduction in Phase 3B value.

**Phase 3B at FB-3 loses most of its value. Cross-session learning requires a closed loop to be meaningful.**

---

## Step 8: Recommendations

### Primary Recommendation: Design for FB-2, Build Toward FB-1

Given Task 4 findings (runtime injection not achievable via push; session-start viable for all agents) and Task 6 findings (comprehension unknown but protocol designed), the team should:

1. **Assume FB-2 as the baseline architecture** for Phase 3A design and development. Session-start injection is the primary delivery mechanism. No Phase 3A code should be written that assumes runtime push injection.

2. **Build MCP infrastructure as a separate, optional module.** Do not let FB-1 ambition delay FB-2 delivery. MCP integration is a Phase 3A enhancement that can be added after FB-2 is stable.

3. **Validate FB-2 viability in Phase 1.** The most critical Phase 1 deliverable for fallback strategy is evidence about directive comprehension. Run the Task 6 protocol early in Phase 1. If compliance is below 30% for all agents, escalate to re-evaluate Phase 3A scope before building the injection pipeline.

4. **Do not permanently accept FB-3.** If Phase 1 testing shows session-start injection fails, treat this as a blocker for Phase 3A — not as a reason to redesign around observation-only. FB-3 is not the product SecondSight was designed to be.

### Handling OpenCode's Unverified Injection Path

OpenCode's config injection path was NOT confirmed from official documentation in Task 4 — it was inferred. Before Phase 3A development, the team must verify:
- Exact config file location (`~/.config/opencode/config.json` or alternative)
- Whether OpenCode reads a per-project instruction file (analogous to CLAUDE.md)
- Whether config changes take effect at session start without restarting the application

If OpenCode injection is not verifiable, Phase 3A should initially ship with Claude Code + Codex injection support and treat OpenCode as a follow-on.

### Directive Merge Strategy (Debt Item)

The directive lifecycle at FB-2 requires a merge strategy for CLAUDE.md/AGENTS.md writes. This has not been designed. Before Phase 3A directive injection code is written, the team must decide:
- Append vs overwrite strategy
- Directive versioning (how to expire outdated directives)
- Conflict resolution when new directives contradict existing content

This is registered as a debt item. It must be resolved before Phase 3A injection code is implemented.

### Compliance Floor for Phase 3A Investment Justification

The Phase 3A investment (directive generation + session-start injection pipeline) is justified if and only if Phase 1 testing shows directive comprehension above 30% for at least two agents. If all three agents show < 30% compliance across all phrasing styles, the Phase 3A investment must be re-scoped. The team should use Phase 1 comprehension results as a go/no-go gate for Phase 3A delivery scope.

**Gate enforcement mechanism:** To prevent the gate from being bypassed silently, the team should:
1. Complete the Task 6 experiment protocol in Phase 1 before beginning Phase 3A planning.
2. Record the per-agent comprehension results in `directive-comprehension.yaml` (updating the `not_tested` entries to actual results).
3. Make Phase 3A kickoff explicitly conditional on the comprehension results: if any team member proposes starting Phase 3A before the comprehension results are in, the fallback is to use FB-3 scope for Phase 3A, not FB-2. This removes the incentive to skip the gate.

The gate is a planning commitment, not a code gate. It must be enforced by the team's planning process (kickoff review, scope confirmation) rather than by automation.

### What "Confirmed Viable" Means for Injection

To avoid the silent failure where "injection confirmed" means only "file was written" and not "agent used the directives," the team must use this two-part definition:

- **Technical confirmation:** The target file (CLAUDE.md, AGENTS.md, or config) was written without error and the content is present in the file.
- **Behavioral confirmation:** Session N+1 shows measurably different behavior on the targeted waste pattern compared to session N without directives (baseline comparison from Task 6 protocol).

Only when both conditions are met is injection "confirmed viable" for an agent. If only technical confirmation is achievable (behavioral data not available), the injection path is labeled "technically confirmed, behaviorally unverified" in Phase 1 reporting.

---

## Assumptions Made in This Design

All of the following are assumptions that require Phase 1 validation. They are stated explicitly because the fallback analysis is only as reliable as these assumptions.

1. **Session-start injection delivers content to agent before turn 1 (assumption, verified for Claude Code and Codex, UNVERIFIED for OpenCode)**
   - If false for Claude Code or Codex: those agents cannot be supported at FB-2 until an alternative path is found.
   - If false for OpenCode: OpenCode support is deferred.

2. **Directive comprehension is above 30% for at least one agent and phrasing style (assumption, not tested)**
   - If false (all agents < 30%): FB-2 loses its value proposition. Phase 3A investment must be re-evaluated.
   - This is the highest-stakes unverified assumption in this document.

3. **Post-session analysis pipeline completes before the next session begins (assumption, not tested)**
   - If false (user starts session N+1 before analysis of session N completes): directives for session N+1 are not ready. Injection is skipped for that session silently.
   - Mitigation: analysis pipeline latency must be measured in Phase 1.

4. **Human review delivery (FB-3 fallback) is actionable by target users (framework maintainers and coding agent users)**
   - Partially supported: target users are technical and can read/apply CLAUDE.md content manually.
   - Risk: adds friction that reduces actual directive adoption even when technically available.

5. **One-session delay (FB-2) is acceptable for the target workflow (agent coding tasks)**
   - Not validated. Must be confirmed with target users in Phase 1.
   - If the agent is used for tasks where session-N mistakes have real costs, one-session delay may be unacceptable.

---

## Known Limitations of This Design

1. **No live data from Tasks 4 or 6.** All injection verdicts are based on documentation analysis and reference source code. Live agent behavior may differ.

2. **OpenCode injection path is the weakest link.** Any fallback design that includes OpenCode is carrying unverified assumptions. OpenCode support should be treated as Phase 3A beta until the config path is confirmed.

3. **Directive merge strategy is undesigned.** The FB-2 lifecycle assumes a merge strategy exists. Without it, session-start injection either overwrites all previous directives (losing history) or appends indefinitely (growing CLAUDE.md unboundedly).

4. **MCP pull-based path feasibility depends on agent prompt engineering.** To make FB-1 work, Claude Code must be prompted or instructed to call the SecondSight MCP tool. This requires careful prompt design that is not yet done. FB-1's viability is conditional on solving this prompt engineering challenge.

5. **No injection confirmation signal exists at any level.** SecondSight cannot verify that an injected directive was received and loaded by the agent. The injection pipeline is fire-and-forget. Silent injection failures (file write succeeded, agent didn't read it) are undetectable without behavioral monitoring.
