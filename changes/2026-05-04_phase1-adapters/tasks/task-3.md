# Task 3: Migrate Normalizer → AgentAdapter (P1-9-migration)

## Context

Read: `2-plan.md` §1 (decision 2 — single-PR migration, no shim), §3 (file map — Modify + Delete sections), §6 AC-1 + AC-10.

This is the migration that prevents parallel scaffolding. Until this task lands, both `api/normalizer.py` and `adapters/base.py` exist on disk. After this task lands, only `adapters/` exists; `api/normalizer.py` is deleted; all callers and tests use the new symbols.

**Plan refs:** P1-9 (migration side)
**Depends on:** task-1 (the new ABC must exist before we can migrate to it)

## Files

- Create: `src/secondsight/adapters/identity.py` — `IdentityAdapter` (renamed from `IdentityNormalizer`)
- Create: `tests/adapters/test_identity.py` — tests migrated from prior `IdentityNormalizer` tests
- Modify: `src/secondsight/adapters/__init__.py` — add `IdentityAdapter`, `AdapterRegistry`, `AgentAdapter`, `NoAdapterError` exports (some lifted from task-1)
- Modify: `src/secondsight/api/server.py` — replace `from secondsight.api.normalizer import …` with `from secondsight.adapters import …`. Update construction sites.
- Modify: `src/secondsight/api/hooks.py` — same.
- Modify: `src/secondsight/api/__init__.py` — drop normalizer re-exports.
- Modify: any `tests/api/test_*.py` files importing the old symbols — update imports.
- Delete: `src/secondsight/api/normalizer.py`

## Migration mapping

| Old | New |
|-----|-----|
| `secondsight.api.normalizer.Normalizer` (Protocol) | `secondsight.adapters.AgentAdapter` (ABC) |
| `secondsight.api.normalizer.NormalizerRegistry` | `secondsight.adapters.AdapterRegistry` |
| `secondsight.api.normalizer.IdentityNormalizer` | `secondsight.adapters.IdentityAdapter` |
| `secondsight.api.normalizer.NoNormalizerError` | `secondsight.adapters.NoAdapterError` |

`IdentityAdapter` keeps the same `agent="test"` scope and the same `normalize` body. The only behavior changes:

- It is now an ABC subclass (vs. duck-typed Protocol implementer).
- It implements `supported_event_types()` — returns the full `EventType` value set.
- It inherits `inject_convention` and `inject_hint` defaults (loud `NotImplementedError`).

## Death tests

DT-1: `import secondsight.api.normalizer` raises `ModuleNotFoundError`. (AC-1 in acceptance.md)
DT-2: `grep -r "from secondsight.api.normalizer" src/` returns zero matches. Implemented as a pytest collect-time check that walks `src/` files. (AC-10)
DT-3: `IdentityAdapter` is an `AgentAdapter` instance: `assert isinstance(IdentityAdapter(), AgentAdapter)`.
DT-4: All previously-green `IdentityNormalizer` tests pass with no behavior change beyond the rename.
DT-5: Hook server still serves `agent="test"` events end-to-end after migration. Existing `tests/api/test_*` route tests stay green with updated imports only.

## Unit tests

- `IdentityAdapter().supported_event_types() == {e.value for e in EventType}` (full coverage for the test agent).
- `IdentityAdapter().supports("test", "user_prompt") is True`.
- `IdentityAdapter().supports("claude_code", "user_prompt") is False` (scope unchanged).
- `IdentityAdapter().inject_hint(...)` raises `NotImplementedError`.

## Implementation steps

- [ ] STEP 0
- [ ] Write death tests (DT-1..DT-5) → red (DT-1 will fail because the file still exists; that's expected)
- [ ] Move `IdentityNormalizer` → `IdentityAdapter` in `adapters/identity.py`, inheriting from `AgentAdapter`
- [ ] Update `src/secondsight/api/server.py` and `api/hooks.py` import paths and construction sites
- [ ] Move/rename tests: `tests/api/test_normalizer*.py` content → `tests/adapters/test_identity.py` (with class rename + ABC structural assertions)
- [ ] Delete `src/secondsight/api/normalizer.py`
- [ ] Run all tests → green (full suite, not just adapters)
- [ ] mypy clean
- [ ] Verify AC-1, AC-3, AC-10 directly (commands in acceptance.md)

## Acceptance for this task

- AC-1, AC-3, AC-10 pass
- Full test suite passes (no regression in count or any `api/` test)
- Task-3 scar report committed
- No commit yet (per phase-1 implement rules — bundle commit after Wave 3)
