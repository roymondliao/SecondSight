# Technical Plan: Phase 0 — Exploration & Risk Validation

## Undocumented Assumptions (from Planning Pre-thinking)

- Task output stored under `changes/2026-04-24_phase0-feasibility/`
- All three agents investigated at equal depth in parallel, using official docs as primary source
- `reference_opensoure/` projects used as starting point, cross-validated with official docs
- P0-12 (Event Schema) and P0-13 (Storage spike) produce POC-level working code, not just paper design
- Python 3.14 + uv as project toolchain

## Architecture Overview

Phase 0 is an **investigation phase**, not a production build phase. The architecture is:

```
Investigation Tasks (parallel where possible)
├── Observation Feasibility (P0-1, P0-2, P0-3)
│   └── Per-agent: hook mechanism → event format → coverage analysis
├── Directive Feasibility (P0-5, P0-6, P0-8)
│   └── Per-agent: injection path → comprehension test → fallback design
├── Event Schema POC (P0-12)
│   └── Unified schema draft based on investigation findings
├── Storage POC (P0-13)
│   └── Filesystem + SQLite dual-layer prototype
└── Session Identity Design (P0-15)
    └── Identity model linking sessions across agents
```

### Dependency Graph (simplified for must-haves)

```
P0-1,2,3 (hook investigation) ──┬──→ P0-12 (event schema POC)
                                │
P0-5 (injection feasibility) ───┤──→ P0-8 (fallback design)
                                │
P0-6 (comprehension test) ──────┘
                                
P0-12 (event schema POC) ──────→ P0-13 (storage POC)
                                
P0-15 (session identity) ──────→ (independent, can parallel with P0-12/13)
```

### Execution Order

**Wave 1 (parallel):** P0-1 + P0-2 + P0-3 + P0-5 + P0-15
**Wave 2 (depends on Wave 1):** P0-6 + P0-12
**Wave 3 (depends on Wave 2):** P0-8 + P0-13

## Tech Stack

- **Language:** Python 3.14
- **Package manager:** uv
- **Testing:** pytest
- **Storage POC:** SQLite (stdlib sqlite3) + filesystem
- **Schema definition:** JSON Schema (for event schema v0.1)
- **Investigation output:** Markdown technical notes + YAML structured findings

## I/O Specifications

### Task Output: Investigation Report

Each investigation task (P0-1, P0-2, P0-3, P0-5, P0-6) produces:

```yaml
output:
  success:
    - feasibility_report.md: structured findings with evidence
    - findings.yaml: machine-readable summary
  failure:
    - report with explicit "infeasible" verdict and evidence
  unknown:
    - report with "inconclusive" verdict, listing what couldn't be determined and why
    - must NOT be silently treated as feasible
```

### Task Output: POC (P0-12, P0-13)

```yaml
output:
  success:
    - working code under src/secondsight/poc/
    - tests under tests/poc/
    - design_notes.md documenting decisions and limitations
  failure:
    - code that demonstrates the failure mode
    - report explaining why the approach doesn't work
  unknown:
    - partial implementation with explicit markers for unresolved areas
    - must NOT ship untested paths as "working"
```

### Task Output: Design Document (P0-15)

```yaml
output:
  success:
    - design document with identity model + linking strategy
    - example data showing linked sessions
  failure:
    - document explaining why linking is infeasible + degradation strategy
  unknown:
    - document with partial model + explicit gaps
```

## Death Cases

### DC-1: Hook investigation reports "feasible" but event data is too shallow

- **Trigger:** Agent exposes hook events, but the event payload doesn't contain tool call details (e.g., only event type + timestamp, no arguments/results)
- **The lie:** Hook coverage rate looks high (many event types)
- **The truth:** Events lack the fields needed for action classification in Phase 2
- **Detection:** Cross-validate hook findings against P0-12's schema requirements. Each event type must be mapped to specific Analysis Layer needs.

### DC-2: Injection "works" but agent ignores the content

- **Trigger:** Directive is successfully injected into session context, but agent's behavior shows no change
- **The lie:** Injection mechanism verified as working
- **The truth:** Agent treats injected content as low-priority context that gets dropped during attention/compaction
- **Detection:** P0-6 comprehension test must include behavioral verification, not just acknowledgment. Test must compare agent output with and without directive.

### DC-3: Storage POC passes benchmarks on synthetic data but fails on real traces

- **Trigger:** SQLite query latency < 500ms on test data, but real agent traces have unpredictable sizes and nested structures
- **The lie:** Storage architecture validated
- **The truth:** Performance degrades non-linearly with real data
- **Detection:** Storage POC must be tested with actual trace data from reference_opensoure projects, not synthetic data.

### DC-4: Event schema appears unified but hides agent-specific workarounds

- **Trigger:** Schema technically covers all three agents, but relies heavily on `metadata: any` or `extension_fields: {}` to absorb differences
- **The lie:** Unified schema achieved
- **The truth:** Schema is a thin wrapper over three incompatible formats
- **Detection:** Count the percentage of typed vs untyped fields per agent. If > 50% of an agent's data lands in untyped fields, the schema is not truly unified.

### DC-5: Session identity design works for single-agent but breaks for cross-agent scenarios

- **Trigger:** Identity linking works when the same agent runs consecutive sessions, but fails when sessions span different agents on the same project
- **The lie:** Session identity model complete
- **The truth:** Identity linking is agent-scoped, not project-scoped
- **Detection:** Design document must include a cross-agent identity scenario and explicitly state whether it's supported or out of scope.

## File Map

### Investigation Reports (Wave 1-3)
- `changes/2026-04-24_phase0-feasibility/investigations/claude-code-hooks.md` — Claude Code hook mechanism findings
- `changes/2026-04-24_phase0-feasibility/investigations/claude-code-hooks.yaml` — Machine-readable findings
- `changes/2026-04-24_phase0-feasibility/investigations/opencode-hooks.md` — OpenCode hook mechanism findings
- `changes/2026-04-24_phase0-feasibility/investigations/opencode-hooks.yaml` — Machine-readable findings
- `changes/2026-04-24_phase0-feasibility/investigations/codex-hooks.md` — Codex hook mechanism findings
- `changes/2026-04-24_phase0-feasibility/investigations/codex-hooks.yaml` — Machine-readable findings
- `changes/2026-04-24_phase0-feasibility/investigations/injection-feasibility.md` — Runtime injection findings (all agents)
- `changes/2026-04-24_phase0-feasibility/investigations/injection-feasibility.yaml` — Machine-readable findings
- `changes/2026-04-24_phase0-feasibility/investigations/directive-comprehension.md` — Comprehension experiment report
- `changes/2026-04-24_phase0-feasibility/investigations/directive-comprehension.yaml` — Machine-readable findings
- `changes/2026-04-24_phase0-feasibility/investigations/fallback-design.md` — Fallback design when injection infeasible
- `changes/2026-04-24_phase0-feasibility/investigations/session-identity-design.md` — Session identity linking model

### POC Code
- `src/secondsight/poc/__init__.py` — POC package
- `src/secondsight/poc/event_schema.py` — Unified Event Schema v0.1 (Python dataclasses/Pydantic)
- `src/secondsight/poc/event_schema.json` — JSON Schema export
- `src/secondsight/poc/storage.py` — Dual-layer storage prototype (filesystem + SQLite)
- `src/secondsight/poc/storage_schema.sql` — SQLite schema DDL
- `tests/poc/__init__.py` — Test package
- `tests/poc/test_event_schema.py` — Schema validation tests
- `tests/poc/test_storage.py` — Storage read/write/query tests
- `tests/poc/conftest.py` — Shared fixtures (sample trace data)

### Design Documents
- `changes/2026-04-24_phase0-feasibility/event-schema-design.md` — Design notes for event schema decisions
- `changes/2026-04-24_phase0-feasibility/storage-design.md` — Design notes for storage architecture
