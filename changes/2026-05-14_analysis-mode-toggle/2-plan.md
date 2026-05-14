# Plan: analysis-mode-toggle

> 陽面的 spec 定義「系統應該做什麼」。陰面的 spec 先定義「系統會怎麼死」。

This plan operationalizes the Locked Decisions in `1-kickoff.md` (A through E7) and the validated config schema in `config.example.toml`. Cross-reference `problem-autopsy.md` for SD deviations and kill conditions.

## 1. Architecture: Mode-Aware Dispatch Centralization

Mode-awareness lives in **exactly one place**: `ProjectAnalysisRuntime.dispatch()` (`src/secondsight/analysis/runtime.py`). All callers — `session_end` hook, sweeper timeout recovery, manual `secondsight analyze` CLI — go through the same entry point and remain mode-agnostic.

```
caller (session_end / sweeper / manual)
        │
        ▼
ProjectAnalysisRuntime.dispatch(session_id, source)
        │
   reads [general].mode
        │
        ├── mode == "sdk" ──► SDKAnalysisDispatcher
        │                       └── PydanticAIAnalysisAgent (existing)
        │                            └── LLMRouter ──► AnthropicProvider(api_key=resolved_key)
        │
        └── mode == "cli" ──► CLIAnalysisDispatcher (new)
                                └── subprocess: claude / codex
                                     └── stdin: rendered jinja prompt + session payload
                                     └── stdout: structured JSON → AnalysisOutput.parse()
                                     └── (on parse fail, retry ≤ 2 with error feedback)
```

Both dispatchers return `AnalysisOutput` instances (or terminal error). The caller cannot tell which mode produced it.

## 2. I/O Contract: AnalysisOutput (the Shared Schema)

Single pydantic `BaseModel` defined in `src/secondsight/analysis/output.py`. This is the cross-mode contract; deviations on either side are validation failures, not silent shape drift.

```python
# src/secondsight/analysis/output.py
class AnalysisOutput(BaseModel):
    schema_version: Literal["1.0"]
    session_id: str
    behavior_flags: list[BehaviorFlag]
    session_summary: SessionSummary
    dispatched_via: Literal["cli", "sdk"]     # telemetry
    cli_agent: str | None = None              # populated only if dispatched_via == "cli"
    primary_model: str | None = None          # populated only if dispatched_via == "sdk"
    fallback_used: bool = False               # SDK fallback engaged?
    retry_count: int = 0                      # how many parse-retries happened (CLI mode)
```

### Three output states (no two-state fiction)

| State | Meaning | Storage |
|---|---|---|
| `success` | `AnalysisOutput` validates clean | row in `intelligence.db` |
| `failure` | Known terminal error (CLI binary missing, all SDK providers failed, schema mismatch after retries exhausted) | row in `intelligence.db` with `status='failed'` + error details |
| `unknown` | Dispatcher cannot determine outcome (subprocess timeout without exit code, ${VAR} interpolation failed at runtime after passing pre-check, etc.) | row in `intelligence.db` with `status='unknown'` + diagnostic |

`unknown` is **NOT** silently coalesced to `failure`. Sweeper and dashboard must be able to query "how many `unknown` analyses do we have?" as a corruption signature.

## 3. CLI Mode Subprocess Protocol

`CLIAnalysisDispatcher` spawns the user's coding agent CLI as a one-shot subprocess.

### Invocation shape (per-agent adapter)

| Agent | Command | Notes |
|---|---|---|
| `claude_code` | `claude --model {model} --output-format json -p "{prompt}"` | `--model` omitted if `[analysis.cli.models].claude_code == ""`; project mount via `cwd` |
| `codex` | TBD via PoC (Task 4) | Codex CLI flags subject to PoC verification |
| `opencode` | rejected at pre-check | Decision C: out of scope |

`opencode` selection rejected at the dispatcher entry with actionable error, not at subprocess level — `claude` / `codex` binaries are exec'd via `asyncio.create_subprocess_exec` with:
- explicit `env=` (inherits `$PATH`, `$HOME`, the coding agent's own auth env; **excludes** SecondSight-internal env to avoid pollution)
- `cwd=` set to the project root
- `stdin=PIPE`, `stdout=PIPE`, `stderr=PIPE`
- bounded `wait_for(timeout=[analysis].timeout_seconds)` — exceeding it → kill subprocess + return `unknown`

### Prompt + payload delivery

stdin is a single rendered jinja2 template (from Task 3) containing:
- Analysis instructions (English, jinja-templated)
- Session payload (events, segments, conventions context) inlined as JSON
- Output schema description (instructs the coding agent to emit JSON matching `AnalysisOutput.json_schema()`)

### Retry loop (runtime, transient failures only)

`AnalysisOutput.parse_raw(stdout)` fails → retry up to **2** times by re-spawning with augmented prompt that includes the previous validation error message. After 2 retries → terminal `failure` with `retry_count=2`. **Not** unbounded retry.

This **runtime** retry budget (≤ 2) is for compensating residual ~5% transient mismatch on a prompt that has already passed Task 4's PoC threshold (≥ 95%). It is **NOT** the lever for fixing systematic schema mismatch — that's prompt-iteration's job (dev-time, in Task 4 PoC). If runtime retry rate climbs above ~5% in production, that signals prompt rot and triggers a Task-4-style re-iteration, not raising the retry ceiling.

## 4. SDK Mode Key Injection Fix

Root cause of the production `ANTHROPIC_API_KEY` error: pydantic-ai's `AnthropicProvider` reads `os.environ["ANTHROPIC_API_KEY"]` implicitly when no key is passed. Loader currently does not pass the resolved key.

### Fix

`LLMRouter.__init__` accepts a resolved-keys dict (passed in from loader after `${VAR}` interpolation). When constructing `AnthropicProvider` / `OpenAIProvider`:

```python
# Before (silent env read):
agent = Agent(model_id)

# After (explicit resolution):
provider = AnthropicProvider(api_key=resolved_keys["anthropic"])
model = AnthropicModel(model_id, provider=provider)
agent = Agent(model)
```

If `resolved_keys["anthropic"]` is empty → that provider is **not** instantiated; router skips to next. If ALL providers are empty → `RouterTerminalError` at router init time, not at first request.

Pre-check (Task 6) ensures we never reach dispatch with all-empty keys, so this is defense-in-depth.

## 5. State.json Schema (for `"auto"` resolution)

```json
// ~/.secondsight/state.json
{
  "schema_version": "1.0",
  "init_agent": "claude_code",            // "claude_code" | "codex"; "opencode" rejected at init
  "init_at": "2026-05-14T13:42:18+08:00",
  "secondsight_version": "<version-at-init>"
}
```

Written by `secondsight init --agent <X>` (Task 2). Read by `ProjectAnalysisRuntime` only when `default_agent == "auto"`. Missing / unparseable → actionable error from dispatcher: `"Run 'secondsight init --agent <claude_code|codex>' to initialize"`.

## 6. Prompts: Jinja2 Refactor

Current state: `src/secondsight/analysis/prompts/{behavior,summary,aggregate}.py` hold Python string constants. Refactor:

```
src/secondsight/prompts/                    (new top-level for English templates)
├── analysis/
│   ├── behavior.jinja2                     (segment behavior analysis)
│   ├── summary.jinja2                      (session summary)
│   └── aggregate.jinja2                    (cross-session aggregation)
└── _loader.py                              (resolves + renders templates)
```

`src/secondsight/analysis/prompts/*.py` become thin shims that load + render the jinja templates. Both CLI and SDK dispatchers consume the same rendered string — single source of truth.

`_loader.py` validates template context strictness: undeclared variable → `jinja2.UndefinedError` raised at render time (death-test target — silent empty render is forbidden).

## 7. Pre-Check Validation (Server Startup Only)

Lives in a new helper `src/secondsight/config/precheck.py`. Called by `secondsight serve` (server bootstrap), **not** by `secondsight init` (init only writes state + installs hooks, does not validate analysis dependencies).

```python
def precheck(config: SecondSightConfig, state: SecondSightState) -> PrecheckResult:
    mode = config.general.mode
    if mode == "cli":
        agent = _resolve_default_agent(config, state)  # "auto" → state.init_agent
        if agent == "opencode":
            return PrecheckResult.fail("opencode CLI mode out of scope this release")
        if shutil.which(_binary_for_agent(agent)) is None:
            return PrecheckResult.fail(f"`{agent}` CLI not found in PATH")
    elif mode == "sdk":
        primary = config.analysis.sdk.primary_model
        if not primary:
            return PrecheckResult.fail("[analysis.sdk].primary_model is required when mode=sdk")
        resolved = _resolve_providers(config.providers)  # applies ${VAR} interpolation
        if not any(resolved.values()):
            return PrecheckResult.fail("mode=sdk requires at least one provider key resolvable")
    return PrecheckResult.ok()
```

Server startup on `PrecheckResult.fail()` → log actionable error → exit with non-zero status. **Not** start in degraded mode.

## 8. Config Schema (Locked — Reference Only)

See `config.example.toml` for the authoritative TOML form. Dataclass changes in `src/secondsight/config/schema.py`:

```python
@dataclass(frozen=True)
class GeneralConfig:
    mode: Literal["cli", "sdk"] = "cli"
    log_level: str = "info"

@dataclass(frozen=True)
class ProviderAnthropicConfig:
    ANTHROPIC_API_KEY: str = ""

@dataclass(frozen=True)
class ProviderOpenAIConfig:
    OPENAI_API_KEY: str = ""

@dataclass(frozen=True)
class ProviderCustomConfig:
    API_KEY: str = ""
    base_url: str = ""

@dataclass(frozen=True)
class ProvidersConfig:
    anthropic: ProviderAnthropicConfig
    openai: ProviderOpenAIConfig
    custom: ProviderCustomConfig

@dataclass(frozen=True)
class AnalysisCLIConfig:
    default_agent: str = "auto"
    models: AnalysisCLIModelsConfig

@dataclass(frozen=True)
class AnalysisCLIModelsConfig:
    claude_code: str = ""
    codex: str = ""
    opencode: str = ""

@dataclass(frozen=True)
class AnalysisSDKConfig:
    primary_model: str = ""
    fallback_model: str = ""

@dataclass(frozen=True)
class AnalysisConfig:
    timeout_seconds: int = 300
    cli: AnalysisCLIConfig
    sdk: AnalysisSDKConfig
```

`GlobalAnalysisConfig` (existing flat form) → loader emits **WARN + ignore** if encountered (E5/migration path); does not raise.

`ProjectAnalysisConfig` (per-project) — frozen as-is per E6; loader still reads it but no new fields added.

## 9. Death Cases (the Truth Behind the Lies)

| ID | Trigger | The Lie | The Truth | Detection |
|---|---|---|---|---|
| **DC1** | CLI subprocess hangs (no output, no exit) | Analysis appears "in progress" forever | Future never resolves; orchestrator slot leaks | `asyncio.wait_for(timeout)` + kill subprocess; emit `unknown` state |
| **DC2** | CLI subprocess writes valid JSON with wrong schema (e.g., extra field, missing `session_summary`) | Analysis appears successful | `intelligence.db` row contains malformed data | Pydantic `AnalysisOutput` strict validation; retry ≤ 2; failure if persists |
| **DC3** | CLI subprocess writes JSON with `behavior_flags=[]` and the model just refused to analyze | Analysis appears successful (empty result is valid shape) | Silent under-analysis; downstream feedback layer has nothing to inject | Tag with `retry_count=0` + log warn if `behavior_flags=[] AND session has >N events`; surface in dashboard |
| **DC4** | SDK primary fails, fallback also fails | `RouterTerminalError` raised | Both providers were unreachable; user blames SecondSight | `fallback_used=true` in `AnalysisOutput`; on both-fail, emit `failure` with `provider_errors=[...]` (both included) |
| **DC5** | `mode=cli` + `default_agent="auto"` + `state.json` missing | Dispatcher hangs trying to resolve | Resolver returns no agent; first call dies with unclear error | Pre-check at server start (Task 6) — server refuses to start; manual `analyze` returns actionable "run init first" |
| **DC6** | `state.json.init_agent="claude_code"` but `claude` binary removed from PATH | Pre-check at server start passes (state.json says OK); first dispatch fails | Pre-check was at startup, env changed after | Pre-check re-runs `shutil.which()` per dispatch (cheap) OR at minimum logs the resolved binary path at startup for forensics |
| **DC7** | User has `$ANTHROPIC_API_KEY` in shell env, config has `ANTHROPIC_API_KEY = ""` (no `${VAR}`) | User assumes SecondSight reads env (SD §8.5.3 mental model); pre-check fails | E1 deviation: SecondSight does NOT implicitly read env; user must write `"${ANTHROPIC_API_KEY}"` | Pre-check error message must say: "config has no providers set; if you intended to use $ANTHROPIC_API_KEY from shell env, write `ANTHROPIC_API_KEY = \"${ANTHROPIC_API_KEY}\"`" |
| **DC8** | Pre-check passes, then mid-flight `${VAR}` resolves to empty (env removed between server start and dispatch) | Dispatcher tries to call with empty key → terminal error | Cached `resolved_keys` at startup; mid-flight env change has no effect | Loader resolves `${VAR}` **once** at config load; dispatch uses cached resolved values; env mutation after start has zero effect (documented) |
| **DC9** | Jinja template renders with missing context variable | CLI gets prompt with literal `{{ session_id }}` or empty string | Coding agent does nothing meaningful, returns empty `behavior_flags` | Strict undefined: jinja env configured with `StrictUndefined` → `UndefinedError` at render → dispatcher returns `failure` |
| **DC10** | Two concurrent dispatches on same session_id (race between session_end and sweeper) | Two `intelligence.db` rows or one row twice updated | Wasted LLM tokens; user sees two analyses | Orchestrator-level lock (existing? verify in Task 6); if absent, dispatch with `INSERT ... ON CONFLICT` semantic + skip duplicates |
| **DC11** | User runs `secondsight init --agent claude_code` then later `secondsight init --agent codex` | state.json silently overwritten; `"auto"` now resolves to codex but user thought it was still claude_code | No warning on re-init | Re-init prompts confirmation if state.json exists with different agent; `--force` bypass for scripts |
| **DC12** | User upgrades from old version with `[analysis] default_agent = "claude_code"` (flat) and no `[general]` section | Loader warn-and-ignores the flat field; mode defaults to `"cli"`; user thinks their old config still in effect | New default kicks in unexpectedly | Loader logs WARN with exact line: "found legacy `[analysis] default_agent`, ignored (now lives under `[analysis.cli].default_agent`); mode defaulted to 'cli'" |

## 10. Documented Assumptions (Carried-Forward Gaps from Research)

These were ACCEPTED gaps at the Pre-thinking gate. They are NOT silent — if any prove false, this plan must be revised.

- **DA-1 (Codex CLI protocol AND prompt iteration responsibility)** — The exact `codex` invocation flags and structured-output mechanism is verified by Task 4 PoC. **Schema mismatch is treated as a prompt-quality problem, not a CLI-capability problem** (per user direction 2026-05-14). Task 4 PoC has a bounded prompt-iteration budget: 3 prompt variants × 10 probes per agent (= 30 probes per agent). If schema-match rate remains < 95% after that budget for either agent, Task 4 STOPS and escalates an explicit decision packet to user; it does NOT auto-drop scope. User chooses among (a) extend budget, (b) add post-processing repair, (c) lower threshold + lean on runtime retry, (d) drop adapter. The schema slot for `[analysis.cli.models].codex` is preserved regardless.
- **DA-2 (Claude Code `--output-format json`)** — Assumed available based on Claude Code docs. Task 4 verifies. If absent, the prompt iteration loop (DA-1) is the path — engineer the prompt to elicit clean JSON without depending on a specific CLI flag, using tool-call convention or explicit schema embedding in the prompt body.
- **DA-3 (jinja `StrictUndefined` viability)** — Assumes all current prompt strings can be migrated without losing context variables silently. Audit during Task 3.
- **DA-4 (No cross-mode storage divergence)** — Both modes write to the SAME `intelligence.db` schema (current schema is sufficient). `dispatched_via` / `cli_agent` / `primary_model` / `retry_count` may need to be added to the analysis row table (Task 7 verifies).
- **DA-5 (Sweeper has existing dispatch entry)** — Assumes sweeper currently calls `ProjectAnalysisRuntime` or equivalent. If it has its own dispatch path, Task 6 must consolidate.
- **DA-6 (Init re-run UX)** — DC11 mitigation is opt-in confirmation. If product wants stronger guarantee (e.g., refuse to overwrite, force user to delete `state.json` first), revise after first user feedback.

## 11. File Map

### New files

| Path | Purpose |
|---|---|
| `src/secondsight/analysis/output.py` | `AnalysisOutput` pydantic model (Task 2) |
| `src/secondsight/analysis/cli_dispatcher.py` | `CLIAnalysisDispatcher` + per-agent adapter (Task 4) |
| `src/secondsight/analysis/sdk_dispatcher.py` | Thin wrapper around existing PydanticAI path (Task 5) |
| `src/secondsight/prompts/analysis/{behavior,summary,aggregate}.jinja2` | Jinja templates (Task 3) |
| `src/secondsight/prompts/_loader.py` | Template loader with `StrictUndefined` (Task 3) |
| `src/secondsight/config/precheck.py` | `precheck()` function (Task 6) |
| `src/secondsight/state.py` | `SecondSightState` + `state.json` read/write (Task 1) |

### Modified files

| Path | Change |
|---|---|
| `src/secondsight/config/schema.py` | New dataclasses: `GeneralConfig`, `ProvidersConfig`, nested `AnalysisCLIConfig`/`AnalysisSDKConfig` (Task 1) |
| `src/secondsight/config/loader.py` | Parse new sections; `${VAR}` interpolation; warn-and-ignore legacy `[analysis] default_agent`; resolve provider keys once at load (Task 1) |
| `src/secondsight/analysis/runtime.py` | `ProjectAnalysisRuntime.dispatch()` branches by `mode` (Task 6) |
| `src/secondsight/analysis/prompts/{behavior,summary,aggregate}.py` | Replace string constants with jinja loader calls (Task 3) |
| `src/secondsight/sdk/router.py` | `LLMRouter.__init__` accepts `resolved_keys`; explicit `AnthropicProvider(api_key=...)` (Task 5) |
| `src/secondsight/cli/init.py` | Write `~/.secondsight/state.json` on init (Task 1) |
| `src/secondsight/cli/serve.py` | Call `precheck()` at startup (Task 6) |
| `config.example.toml` (repo root) | Create/update with locked schema |

## 12. Sequencing Summary

```
Task 1 (Config + State + Loader)  ──┬─► Task 5 (SDK key fix) ──┐
                                    │                          │
Task 2 (AnalysisOutput schema) ─────┼─► Task 4 (CLI PoC) ──────┼─► Task 6 (Dispatch + Precheck) ──► Task 7 (E2E)
                                    │                          │
Task 3 (Jinja prompts) ─────────────┘                          │
                                                               │
                                    (Task 4 KILL CONDITION ────┘
                                     gates Task 6 CLI path)
```

Task 4 carries the highest risk and must run early. If its kill condition fires, scope reduces to "SDK mode toggle + bug fix only", and Tasks 6/7 reshape accordingly.
