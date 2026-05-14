# Task 4: CLI mode dispatcher — Claude Code + Codex (KILL CONDITION)

## Context

Read: `overview.md`, `2-plan.md` §3, §9 (DC1-DC3, DC6), §10 (DA-1, DA-2).

**This task carries the highest risk in the plan and owns the cross-mode reliability of CLI dispatch.** It implements the `CLIAnalysisDispatcher` that spawns the user's coding agent CLI (`claude` or `codex`) as a one-shot subprocess, feeds it the rendered jinja2 prompt + session payload via stdin, parses structured JSON from stdout, and bounded-retries on validation failure (≤ 2 retries at runtime, separate from dev-time prompt iteration below).

**Critical framing (per user direction 2026-05-14)**: this task owns **prompt quality**, not just dispatcher mechanics. If the coding agent CLI fails to produce schema-conformant output, the assumption is the **prompt is the variable that needs work** — not that the CLI is incapable. Modern coding agent CLIs follow structured-output instructions when prompted precisely; failure to do so points back at us.

The escalation condition (per `problem-autopsy.md` revised kill_condition #1 and `2-plan.md` revised DA-1): if, after a **bounded prompt iteration budget of 3 prompt variants × 10 probes per agent (= 30 probes per agent)**, schema-match rate remains < 95%, **STOP and escalate to user** with the specific failure modes documented. Escalation is NOT auto-drop — the user decides: (a) extend prompt iteration budget, (b) add per-agent prompt specialization, (c) accept lower threshold with explicit retry compensation, (d) drop the failing adapter. Task 4 does not unilaterally drop scope.

Subprocess invocation: `asyncio.create_subprocess_exec` with explicit `env=` (NO SecondSight-internal env leaked), `cwd=` set to project root, `stdin/stdout/stderr=PIPE`, `wait_for(timeout=config.analysis.timeout_seconds)`. On timeout: SIGTERM, 1s grace, SIGKILL. On parse failure: re-spawn with augmented prompt containing the validation error message.

`opencode` selection is rejected at the dispatcher entry with actionable error (Decision C: out of scope this effort).

## Files

- Create: `src/secondsight/analysis/cli_dispatcher.py`
- Create: `src/secondsight/analysis/cli_adapters/__init__.py`
- Create: `src/secondsight/analysis/cli_adapters/claude_code.py` — Claude Code invocation adapter (flags, env, stdin/stdout shape)
- Create: `src/secondsight/analysis/cli_adapters/codex.py` — Codex invocation adapter
- Create: `changes/2026-05-14_analysis-mode-toggle/cli-protocol-poc-results.md` — PoC log: actual commands run, raw stdout samples, AnalysisOutput parse rate per adapter, kill-condition verdict
- Test: `tests/analysis/test_cli_dispatcher.py` — uses subprocess mock fixtures
- Test: `tests/analysis/test_cli_adapters.py` — per-adapter unit tests for command building
- Test: `tests/analysis/test_cli_dispatcher_e2e.py` — opt-in real-CLI test (gated by env var `SECONDSIGHT_TEST_REAL_CLI=1`); CI runs only the mocked tests

## Death Test Requirements

Before any implementation:

- Test: subprocess writes nothing and sleeps past `timeout_seconds` → dispatcher kills with SIGTERM, waits 1s, then SIGKILL if still alive; returns `AnalysisOutput(status="unknown", error_details={"reason": "timeout", "stderr": "..."})`
- Test: subprocess exits 0 with invalid JSON `not-json-at-all` → dispatcher catches `json.JSONDecodeError`, retries once with augmented prompt; if second attempt also fails, retries once more; total retry_count ≤ 2; final result is `status="failure"` with `error_details={"reason": "json_decode", "attempts": 2}`
- Test: subprocess exits 0 with valid JSON but `AnalysisOutput.model_validate_json` fails → same retry flow; `error_details["reason"] = "schema_mismatch"`
- Test: subprocess exits 0 with empty stdout → treated same as `json_decode` failure
- Test: subprocess exits NON-zero (e.g., 1, 127 for binary-not-found) → dispatcher returns `status="failure"` with `error_details["reason"] = "subprocess_exit", "exit_code": N, "stderr": "..."` without retry
- Test: `default_agent="opencode"` → dispatcher raises `OpencodeNotSupportedError` (or returns `failure` with actionable message) at entry, never invokes subprocess
- Test: env passed to subprocess does NOT contain `SECONDSIGHT_*` variables (verify with a probe subprocess that prints `os.environ` and dispatcher captures it)
- Test: subprocess `cwd` equals the project root passed into dispatcher (verify via a probe subprocess that prints `os.getcwd()`)
- Test: stderr is captured into `error_details["stderr"]` even on success (for forensics)
- Test: retry's augmented prompt includes the validation error message verbatim (assert by inspecting the rendered prompt passed to mock subprocess on second invocation)

## PoC Phase: Prompt-Quality Iteration (BEFORE writing the dispatcher)

This is a **bounded prompt-engineering iteration**, not a one-shot capability test. Task 3 produced first-draft jinja2 prompts; this phase iterates on them inside real CLI subprocess invocations until schema-match rate ≥ 95% per agent, OR the iteration budget is exhausted.

### Budget (locked 2026-05-14)

- **Per agent**: up to 3 prompt variants × 10 probes per variant = 30 probes per agent
- **Pass criterion**: any single variant achieves ≥ 95% schema-match (≥ 9 of 10 probes return JSON that validates against `AnalysisOutput`)
- **Variants are cumulative refinements**: variant 2 builds on variant 1 (incorporating failure modes seen), variant 3 on variant 2 — NOT three independent re-rolls

### Phase steps

- [ ] PoC-1 (Claude Code, variant 1): use Task 3's first-draft jinja template rendered with a fixture session. Invocation: try `claude --output-format json -p "<rendered prompt>"` first; fall back to `claude -p "<rendered prompt>"` with explicit "output ONLY JSON" instruction if `--output-format` not supported. Run 10 probes. Compute schema-match rate.
- [ ] PoC-2 (Claude Code, variant 2 — only if variant 1 < 95%): identify dominant failure mode from variant 1 output (e.g., "model wrapped JSON in markdown fences", "session_summary missing", "extra fields injected"). Adjust the jinja template — add explicit instruction targeting that mode, embed `AnalysisOutput.model_json_schema()` text more prominently, or add 1-2 few-shot example outputs. Run 10 probes on variant 2.
- [ ] PoC-3 (Claude Code, variant 3 — only if variant 2 < 95%): same iteration on dominant failure mode from variant 2.
- [ ] PoC-4..6: same 3-variant flow for Codex. Codex CLI flag surface differs (find headless one-shot equivalent of `claude -p`); the prompt CONTENT may need agent-specific specialization, captured as `behavior.codex.jinja2` if needed.
- [ ] PoC-7 (Verdict): for each agent record final-variant schema-match rate. If ≥ 95%, PROCEED to dispatcher implementation. If < 95% after 3 variants for either agent, **STOP and escalate to user** — do NOT auto-drop.

### Documentation deliverable

Record in `changes/2026-05-14_analysis-mode-toggle/cli-protocol-poc-results.md`:
- Per agent, per variant: the exact prompt content (or diff from previous variant), exact invocation command, raw stdout samples (3 representative — 1 success, 1 schema-mismatch, 1 other failure if any), 10-probe match rate
- Final-variant winning prompt → committed back into `src/secondsight/prompts/analysis/*.jinja2` (Task 4 may modify what Task 3 produced)
- Verdict: PROCEED (both agents ≥ 95%) / ESCALATE_CLAUDE (claude < 95%) / ESCALATE_CODEX (codex < 95%) / ESCALATE_BOTH

### Escalation packet (if triggered)

If escalating, the packet to user must include:
- Which agent(s) failed and at what final rate
- Top 3 failure modes observed across all 30 probes for that agent
- Sample raw outputs demonstrating each failure mode
- Three concrete next-step options for user choice:
  - (a) Extend budget by N additional variants (and what specific direction to try)
  - (b) Add post-processing repair step (e.g., strip markdown fences, coerce common malformations) — explicit list of repairs
  - (c) Lower threshold to X% with explicit reliance on the 2-retry runtime loop
  - (d) Drop the failing adapter from this effort, schema slot preserved, follow-up issue created

The user picks; Task 4 does not pick for them.

## Implementation Steps

- [ ] Step 1: Execute PoC phase above; do not proceed if kill condition fires (escalate to user)
- [ ] Step 2: Write death tests
- [ ] Step 3: Run death tests — verify import failure / assertion failure
- [ ] Step 4: Write happy-path unit tests (subprocess mock returns valid AnalysisOutput JSON → dispatcher returns parsed instance)
- [ ] Step 5: Run unit tests — verify they fail
- [ ] Step 6: Implement `cli_adapters/claude_code.py` per PoC findings — function `build_command(model: str | None, prompt: str, project_root: Path) -> list[str]` and `build_env(parent_env: dict) -> dict`
- [ ] Step 7: Implement `cli_adapters/codex.py` similarly (skip if PoC dropped Codex)
- [ ] Step 8: Implement `CLIAnalysisDispatcher` in `cli_dispatcher.py`:
  - `__init__(config: AnalysisConfig, state: SecondSightState, prompt_loader)`
  - `async dispatch(session_id: str, project_root: Path, session_payload: dict) -> AnalysisOutput`
  - Internal: resolve `default_agent` (via state.json if "auto"), reject opencode, pick adapter, render prompt via prompt_loader, spawn subprocess, timeout-bounded await, parse with retry loop, return AnalysisOutput
- [ ] Step 9: Write E2E test (opt-in via env var) that actually invokes a real `claude` and a real `codex` against a fixture session
- [ ] Step 10: Run all tests
- [ ] Step 11: Run `pre-commit run --all-files`
- [ ] Step 12: Write scar report (must reference the PoC result)
- [ ] Step 13: Commit

## Expected Scar Report Items

- Potential shortcut: if a prompt variant shows ~50% schema match, "just enable runtime retry — it'll work eventually." **Don't.** Runtime retry budget (≤ 2) compensates for ~5% residual mismatch, not 50%. Prompt quality is the lever; iterate the prompt.
- Potential shortcut: declaring variant 1 "good enough" at 88% to skip variant 2. **Don't.** Pass criterion is 95%; the iteration is not optional below that.
- Potential shortcut: writing three radically different variants instead of cumulative refinements. **Don't.** The point is to converge on a winning prompt by addressing dominant failure modes; three random re-rolls don't extract that signal.
- Potential shortcut: when escalating, picking option (b) or (c) yourself without consulting user. **Don't.** Escalation packet goes to user; user decides.
- Potential shortcut: passing the parent shell's full env to subprocess (just `env=os.environ.copy()`). **Don't.** SecondSight-internal env (anything `SECONDSIGHT_*`, internal logging vars) must be filtered out to avoid the coding agent reading SecondSight state by accident.
- Potential shortcut: combining stderr into stdout (`stderr=STDOUT`). **Don't.** Forensics need them separate; some coding agents log progress to stderr while emitting JSON to stdout.
- Potential shortcut: making "auto" resolve in the dispatcher. **No** — `default_agent` resolution to a concrete agent must happen ONCE at dispatch entry; the dispatcher does not re-read state.json on retry.
- Assumption to verify: `asyncio.create_subprocess_exec` SIGKILL behavior across macOS / Linux is consistent. Document if different.
- Assumption to verify: Claude Code's output (`--output-format json` or whatever PoC settles on) contains a wrapper envelope or is the raw analysis JSON. If wrapped, adapter must unwrap before passing to `AnalysisOutput.model_validate_json`.
- Watch for: Codex's stdin handling. Some agents wait for EOF before responding; others stream. The adapter must close stdin after writing the prompt (don't leave it open hoping for interactive mode).
- Watch for: very long prompts. The session payload may be large (segments × events). If stdin write blocks because subprocess hasn't started reading yet, you'll deadlock. Use `process.communicate(input=prompt.encode())` instead of separate write+read.

## Acceptance Criteria

Covers from `acceptance.yaml`:
- DC1 (subprocess timeout)
- DC2 (schema mismatch retry)
- DC3 (empty behavior_flags happy path)
- DC6 (CLI binary disappearance)
- DC10 partial (caller-side race resolution is Task 6)
- "CLI mode retry recovers on second attempt" (degradation)
- "Happy path — mode=cli + claude_code"
