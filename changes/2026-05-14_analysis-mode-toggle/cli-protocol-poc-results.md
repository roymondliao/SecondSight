# CLI Protocol PoC Results

**Date:** 2026-05-14
**Task:** Task 4 — CLI mode dispatcher with prompt-quality PoC budget
**Verdict:** PROCEED (both agents)

---

## Feasibility Check

```
which claude → /Users/yuyu_liao/.local/bin/claude (version 2.1.141)
which codex  → /opt/homebrew/bin/codex (version 0.130.0)
ANTHROPIC_API_KEY → not set in env (claude uses browser OAuth / session state)
OPENAI_API_KEY    → not set in env (codex uses ~/.codex/auth.json)
Auth verified: both binaries respond successfully to test prompts
```

Both binaries available and authenticated. REAL_POC path taken.

---

## Invocation Surface

### Claude Code

```
claude --print --output-format json --no-session-persistence [--model <model>] <prompt>
```

Output format: claude wraps the LLM response in a JSON envelope:
```json
{
  "type": "result",
  "subtype": "success",
  "result": "<actual LLM response text>",
  "duration_ms": 3835,
  ...
}
```
The dispatcher must extract `outer["result"]` before parsing `AnalysisOutput`.

### Codex

```
codex exec --ephemeral -o <output_file> [--model <model>] -
```

- Reads prompt from stdin (the `-` sentinel)
- Writes the last agent message to `<output_file>`
- `--ephemeral`: prevents session accumulation on disk
- Dispatcher passes prompt via `process.communicate(input=prompt_bytes)`
- Reads output from file after subprocess exits

---

## Fixture Session Payload

A 10-event session with detectable inefficiencies:
- Two consecutive reads of `src/server.py` → `repeated_operation`
- Read of unrelated `README.md` → `unnecessary_read`

Used for all 20 probes (10 per agent).

---

## Prompt Variant 1 (winning variant — no further variants needed)

Both agents hit 10/10 on Variant 1.

**Prompt structure:**
```
You are a coding agent behavior analysis expert.

[Valid Behavior Flag Types]
- unnecessary_read: <description>
- redundant_exploration: <description>
- missed_shortcut: <description>
- repeated_operation: <description>
- wrong_tool_choice: <description>
- excessive_context_gathering: <description>

[Session Data to Analyze]
<session_payload JSON>

[Task]
Analyze this session. Detect any inefficient or unnecessary operations.
Return ONLY a JSON object matching this schema exactly - no markdown, no
explanation, just the JSON:

<AnalysisOutput.model_json_schema() JSON>

Required field values for this response:
- schema_version: "1.0"
- session_id: "<id>"
- status: "success"
- dispatched_via: "cli"
- cli_agent: "<agent_name>"
- primary_model: null
- fallback_used: false
- retry_count: 0
- error_details: null
```

**Key design decisions in V1:**
1. Embedded full `AnalysisOutput.model_json_schema()` — leaves no ambiguity about required fields
2. Explicit "Required field values" section — pre-fills dispatcher-set fields so LLM doesn't guess
3. Clear instruction "no markdown, no explanation, just the JSON" — prevents fence wrapping
4. Flag type definitions included verbatim from `FLAG_DEFINITIONS` (even though in Chinese)

---

## Probe Results

### Claude Code — Variant 1 (10 probes)

| Probe | Result | Flags Detected |
|-------|--------|---------------|
| 1     | PASS   | 3             |
| 2     | PASS   | 2             |
| 3     | PASS   | 3             |
| 4     | PASS   | 3             |
| 5     | PASS   | 2             |
| 6     | PASS   | 3             |
| 7     | PASS   | 2             |
| 8     | PASS   | 3             |
| 9     | PASS   | 2             |
| 10    | PASS   | 2             |

**Match rate: 10/10 = 100%** ≥ 95% threshold. PROCEED.

### Codex — Variant 1 (10 probes)

| Probe | Result | Flags Detected |
|-------|--------|---------------|
| 1     | PASS   | 3             |
| 2     | PASS   | 2             |
| 3     | PASS   | 2             |
| 4     | PASS   | 3             |
| 5     | PASS   | 2             |
| 6     | PASS   | 2             |
| 7     | PASS   | 2             |
| 8     | PASS   | 2             |
| 9     | PASS   | 2             |
| 10    | PASS   | 2             |

**Match rate: 10/10 = 100%** ≥ 95% threshold. PROCEED.

---

## VERDICT: PROCEED

Both claude_code and codex produced valid `AnalysisOutput` JSON on 100% of probes
using Variant 1. No further variants needed. Variant 2 and Variant 3 budget not consumed.

The winning prompt (Variant 1) was migrated into `src/secondsight/prompts/analysis/cli_dispatch.jinja2`
and is now rendered via `secondsight.prompts._loader.render("analysis/cli_dispatch", context=...)`.
The `_build_poc_variant1_prompt()` helper was removed. The jinja template renders content
byte-equivalent to the original f-string (1-byte difference: trailing newline from trim_blocks).
All content checks match: header, flag definitions block, session payload, schema, required fields.

---

## Known Limitations

1. **FLAG_DEFINITIONS content is in Chinese** — from `schemas.py`. Both agents handled
   Chinese content correctly. Task 3's scar report documents this as accepted debt
   (user direction: "mixed-language prompts OK if surrounding instructions are English").

2. **Schema stability assumption** — the prompt embeds `AnalysisOutput.model_json_schema()`.
   If the schema changes, the prompt auto-updates on each dispatch (schema is injected at
   render time, not hardcoded). The PoC result is valid for the current schema version "1.0".

3. **PoC fixture is minimal** — 10 events, 1 repeated read. Production sessions may be
   much larger (thousands of events). If session payloads exceed the model's context window,
   the dispatcher will get a truncation error or reduced quality. This is a known risk,
   not a blocking condition — it's a prompt engineering concern for production use.

4. **Codex output file cleanup** — the dispatcher creates a temp file in project_root for
   codex output and deletes it after reading. On crash (SIGKILL), cleanup doesn't run.
   Orphaned `codex-output-<uuid>.txt` files may accumulate in project_root.

---

## Next Steps

- Task 5: SDK dispatcher (independent path)
- Task 6: Mode-aware orchestrator that wires CLI and SDK dispatchers
