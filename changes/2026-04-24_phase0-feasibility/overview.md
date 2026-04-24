# Overview: Phase 0 — Exploration & Risk Validation

## Goal

Validate that SecondSight can obtain execution events from Claude Code, OpenCode, and Codex, and can inject directives back into these agents — producing POC-level evidence for event schema and storage architecture.

## Architecture

Phase 0 is an investigation phase organized in three waves. Wave 1 investigates hook mechanisms and injection feasibility across all three agents in parallel. Wave 2 uses those findings to draft a unified event schema and run directive comprehension tests. Wave 3 designs fallback paths and builds the storage POC. Two tasks (P0-12 event schema, P0-13 storage) produce working POC code; the rest produce structured investigation reports.

## Tech Stack

- Python 3.14, uv, pytest
- SQLite (stdlib sqlite3) for structured storage POC
- Filesystem for raw trace storage POC
- JSON Schema for event schema definition
- Markdown + YAML for investigation reports

## Key Decisions

- **Equal investigation depth for all agents:** All three agents investigated in parallel at equal depth, using official docs as primary source and reference_opensoure/ as starting point
- **POC-level code for schema and storage:** P0-12 and P0-13 produce working prototypes (not paper designs) so Phase 1 can build on them
- **Official docs as source of truth:** reference_opensoure/ projects cross-validated against official documentation
  - Claude Code: https://code.claude.com/docs/en/overview
  - OpenCode: https://opencode.ai/docs
  - Codex: https://developers.openai.com/codex
- **Investigation reports include machine-readable findings:** Each report has a companion .yaml for downstream automation

## Death Cases Summary

1. **Hook investigation reports "feasible" but event payloads lack fields needed for action classification** — detection: map each event type to Analysis Layer requirements, check field completeness
2. **Injection "works" but agent ignores directive content** — detection: behavioral before/after comparison, not just acknowledgment
3. **Event schema hides incompatibility behind untyped `metadata: Any` fields** — detection: typed_field_percentage per agent must be >= 50%

## File Map

### Investigation Reports
- `changes/2026-04-24_phase0-feasibility/investigations/claude-code-hooks.md` — Claude Code hook findings
- `changes/2026-04-24_phase0-feasibility/investigations/claude-code-hooks.yaml` — Machine-readable
- `changes/2026-04-24_phase0-feasibility/investigations/opencode-hooks.md` — OpenCode hook findings
- `changes/2026-04-24_phase0-feasibility/investigations/opencode-hooks.yaml` — Machine-readable
- `changes/2026-04-24_phase0-feasibility/investigations/codex-hooks.md` — Codex hook findings
- `changes/2026-04-24_phase0-feasibility/investigations/codex-hooks.yaml` — Machine-readable
- `changes/2026-04-24_phase0-feasibility/investigations/injection-feasibility.md` — Injection findings
- `changes/2026-04-24_phase0-feasibility/investigations/injection-feasibility.yaml` — Machine-readable
- `changes/2026-04-24_phase0-feasibility/investigations/directive-comprehension.md` — Comprehension experiment
- `changes/2026-04-24_phase0-feasibility/investigations/directive-comprehension.yaml` — Machine-readable
- `changes/2026-04-24_phase0-feasibility/investigations/fallback-design.md` — Fallback strategies
- `changes/2026-04-24_phase0-feasibility/investigations/session-identity-design.md` — Identity model

### POC Code
- `src/secondsight/poc/event_schema.py` — Unified Event Schema v0.1
- `src/secondsight/poc/event_schema.json` — JSON Schema export
- `src/secondsight/poc/storage.py` — Dual-layer storage prototype
- `src/secondsight/poc/storage_schema.sql` — SQLite DDL
- `tests/poc/test_event_schema.py` — Schema validation tests
- `tests/poc/test_storage.py` — Storage tests
- `tests/poc/conftest.py` — Shared fixtures

### Design Documents
- `changes/2026-04-24_phase0-feasibility/event-schema-design.md` — Schema design notes
- `changes/2026-04-24_phase0-feasibility/storage-design.md` — Storage design notes
