# Task 1: Test scaffold — tests/integration/ package + prereq guard

## Context

Read: `overview.md` for full architecture.

GUR-99 adds end-to-end integration tests at `tests/integration/test_phase1_e2e.py`. This task ONLY scaffolds the package and prereq-skip helper; no MH scenarios yet. Subsequent tasks (MH-1 through MH-5) all depend on this scaffold.

The fixtures `real_secondsight_server`, `hook_script`, `run_hook`, `build_env`, and constants `EXPECTED_VERSION`, `FALLBACK_FILENAME` already exist in `tests/scripts/conftest.py` and will be **imported directly** by the new test module. No fixture lifting; no shared conftest changes.

The pyproject.toml may need `[tool.pytest.ini_options].testpaths` extended if it does not already cover `tests/`. Inspect first; modify only if needed.

## Files

- Create: `tests/integration/__init__.py` (empty)
- Create: `tests/integration/_prereqs.py` (PATH-tool guard helper)
- Create: `tests/integration/test_phase1_e2e.py` (module skeleton with imports + module-level prereq call; no MH classes yet)
- Modify: `pyproject.toml` ONLY IF `testpaths` does not already cover `tests/`

## Death Test Requirements

- **DT-1.1** Module import skips loudly when `bash` is absent from PATH (skip message must contain the literal string `"bash"`)
- **DT-1.2** Module import skips loudly when `curl` is absent from PATH
- **DT-1.3** Module import skips loudly when `jq` is absent from PATH
- **DT-1.4** Module import does NOT skip when all three are present — collection succeeds, no false skips

## Implementation Steps

- [ ] Step 1: Inspect `pyproject.toml` for current `testpaths`. If absent or doesn't cover `tests/`, plan edit; otherwise leave alone.
- [ ] Step 2: Write death tests for `_prereqs.ensure_e2e_prereqs()` in `tests/integration/test_prereqs.py` (a sibling test file that exercises the helper with monkeypatched `shutil.which`). All four death tests must fail red.
- [ ] Step 3: Run death tests — verify red.
- [ ] Step 4: Implement `_prereqs.py` with a single function `ensure_e2e_prereqs() -> None` that calls `pytest.skip(allow_module_level=True, reason=...)` with a message naming the missing tool. Use `shutil.which` for detection.
- [ ] Step 5: Implement `test_phase1_e2e.py` skeleton: imports + module-level call to `ensure_e2e_prereqs()` + a single `test_scaffold_collects()` placeholder that asserts `True` (proves module collected without error).
- [ ] Step 6: Run all death tests — must be green.
- [ ] Step 7: Run `pytest tests/integration/ -v` — confirm 1 placeholder test passes when prereqs present.
- [ ] Step 8: Write scar report. Commit.

## Specifics

### `tests/integration/_prereqs.py`

```python
"""Module-level prereq guard for tests/integration/test_phase1_e2e.py."""
from __future__ import annotations

import shutil

import pytest

_REQUIRED_TOOLS: tuple[str, ...] = ("bash", "curl", "jq")


def ensure_e2e_prereqs() -> None:
    """Skip the entire module if any required tool is missing from PATH.

    NEVER fall through silently. Skip messages must name the missing tool
    so an operator can install it and re-run.
    """
    for tool in _REQUIRED_TOOLS:
        if shutil.which(tool) is None:
            pytest.skip(
                f"GUR-99 e2e tests require {tool!r} on PATH; not found",
                allow_module_level=True,
            )
```

### `tests/integration/test_phase1_e2e.py` (skeleton only)

```python
"""GUR-99 — Phase 1 end-to-end integration tests.

Five must-have scenarios (MH-1..MH-5) close seam-level gaps that
component-level unit tests cannot catch. See changes/2026-05-05_gur-99_*
for the full plan.
"""
from __future__ import annotations

from tests.integration._prereqs import ensure_e2e_prereqs

ensure_e2e_prereqs()  # module-level skip — must precede all imports below


def test_scaffold_collects() -> None:
    """Placeholder: scaffold collects without error when prereqs are met."""
    assert True
```

## Expected Scar Report Items

- Potential shortcut: skipping `ensure_e2e_prereqs()` and just letting tests fail with "command not found" — rejected; non-named failure is silent loss.
- Potential shortcut: putting `_prereqs.py` in `tests/conftest.py` to apply globally — rejected; only `tests/integration/` needs jq, lifting it pollutes other test modules.
- Assumption to verify: `pyproject.toml` already includes `tests/` in `testpaths` (typical pytest setup) — if not, the modification scope grows and may need a separate review.

## Acceptance Criteria

- Covers: enables MH-1..MH-5 (no MH scenarios yet — this is pure scaffold)
- Covers: "Skip-with-named-tool when prereqs missing" (Step 0 commitment 2)
