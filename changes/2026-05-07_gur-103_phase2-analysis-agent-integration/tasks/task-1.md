# Task 1 (P2-11): `analysis/tools.py` — AnalysisTools

## Context

Read: `overview.md`, `2-plan.md` §3 (DC-1, DC-2), §6 (config schema).

This task ships the framework-agnostic tool layer that every
analysis agent (current SDK + hypothetical future CLI mode) calls.
Closes DC-1 (sandbox bypass via symlink) and DC-2 (denylist case
bypass + ancestor-directory variant).

The four tools mirror SD §5.4 names. Their bodies do thin
adapter work over existing repositories — except
`read_project_file`, which is the security-critical method.

## Files

- Create: `src/secondsight/analysis/tools.py`
- Create: `src/secondsight/analysis/config.py` (TOML loader for
  `[analysis]` and `[analysis.read_project_file]`; modeled on
  `storage/retention_config.py` from GUR-147)
- Test: `tests/analysis/test_tools.py`
- Test: `tests/analysis/test_config.py`

## Death Test Requirements

Write these BEFORE implementation:

- **DT-1.1 sandbox rejects symlink escape (DC-1).** Create a
  tmp-path project with a symlink pointing outside; assert
  `ProjectFileToolError` raised; assert WARN log records
  resolved path.
- **DT-1.2 denylist case-insensitive (DC-2).** Project contains
  `.ENV`; pattern `*.env` matches; tool raises
  `ProjectFileToolError`; original case preserved in log.
- **DT-1.3 denylist match on ancestor directory (DC-2).** Project
  contains `.ssh/id_rsa`; ancestor `.ssh/*` denylist pattern
  blocks the read; raises before reaching `id_rsa*` ultimate-
  filename check.
- **DT-1.4 size cap.** A 512 KiB file returns first 256 KiB +
  truncation marker (`<truncated: original size 524288 bytes>`).
- **DT-1.5 binary file placeholder.** A file containing
  `\x00\x01\x02` returns `<binary file: N bytes>`, never raw
  bytes that could break LLM JSON encoding.
- **DT-1.6 query_structured_store rejects unknown kind.**
  `{"kind":"DROP TABLE"}` raises `ValueError`; never reaches
  any repo method; assertion via mock — repo methods recorded
  zero invocations.

## Implementation Steps

- [ ] Step 1: Write death tests (5 above) — they fail because
      `analysis/tools.py` does not exist.
- [ ] Step 2: Run death tests — verify they fail.
- [ ] Step 3: Write happy-path tests (HP-1.4 read_project_file
      happy path; HP for read_traces; HP for query_structured_store
      with both valid `kind` values; HP for read_historical_flags).
- [ ] Step 4: Run happy-path tests — verify they fail.
- [ ] Step 5: Implement `AnalysisTools` class:
      - `read_traces(session_id)` → pass-through to events_repo
      - `read_project_file(project_id, relative_path)` —
        the load-bearing method:
        1. Look up project root via `project_config.root_path`;
           if missing, raise `ProjectFileToolError`.
        2. Compute denylist by merging built-in + project-config
           additions. Built-in: `[".env", ".env.*",
           "*credentials*", "*secret*", "*.pem", "id_rsa*",
           ".aws", ".aws/*", ".ssh", ".ssh/*"]`.
        3. Resolve via `Path(project_root, relative_path).resolve(
           strict=True)`. Catch `FileNotFoundError` → wrap as
           `ProjectFileToolError`.
        4. Re-check `resolved.is_relative_to(project_root.resolve())`.
           If False → `ProjectFileToolError`, WARN-log resolved.
        5. Walk every component of `resolved.relative_to(
           project_root)` and the resolved filename; for each,
           check (case-insensitive) against denylist patterns
           via `fnmatch.fnmatchcase(component.lower(),
           pattern.lower())`. Match → raise + WARN log.
        6. Read with `await asyncio.to_thread(path.read_bytes)`.
           Size > 256 KiB → truncate with marker.
        7. Try `bytes.decode('utf-8')`; on UnicodeDecodeError,
           return binary placeholder.
      - `query_structured_store(query)` — accept exactly two
        `kind` values; raise on others; map to repo methods.
      - `read_historical_flags(project_id, limit=200)` —
        pass-through to behavior_flags repo, grouped by flag_type.
- [ ] Step 6: Run all tests — verify they pass.
- [ ] Step 7: Implement `AnalysisConfig` Pydantic model + TOML
      loader (mirror RetentionConfig pattern); add a config test.
- [ ] Step 8: Write scar report.
- [ ] Step 9: Commit.

## Expected Scar Report Items

- `Path.resolve(strict=True)` raises `FileNotFoundError` on
  non-existent paths; we wrap to `ProjectFileToolError` so the
  failure mode is uniform — verify we don't accidentally let
  `FileNotFoundError` escape.
- Denylist match on EVERY ancestor component duplicates work for
  deep paths; accept for v1 — the cost is bounded by path depth.
- `query_structured_store` v1 vocabulary is small (2 shapes);
  document expansion process at module top: each new shape
  requires a typed dataclass + repo method + test; no free-form
  expansion.
- The async signature on `read_project_file` (because of
  `asyncio.to_thread`) means callers must `await`; PydanticAI
  tool registration handles this transparently in task-4, but
  document the contract.

## Acceptance Criteria

Covers from `acceptance.yaml`:
- DT-1.1, DT-1.2, DT-1.3, DT-1.4, DT-1.5, DT-1.6 (all silent-
  failure scenarios)
- HP-1.4 (read_project_file happy path)
