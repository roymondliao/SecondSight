"""Module-level prereq guard for tests/integration/test_phase1_e2e.py.

The e2e suite shells out to ``bash`` to run the real hook scripts, uses
``curl`` from inside those scripts to talk to the live server, and uses
``jq`` to construct envelope JSON. If any of these is missing, the suite
must skip with a NAMED message — never silently green-pass.

Silent green-pass on missing prereqs is the worst failure mode here: it
gives the appearance of e2e coverage while exercising nothing. The
named-skip contract makes the failure mode loud and operator-actionable
("install <tool>") instead of mysterious.
"""
from __future__ import annotations

# COUPLING CONTRACT: tests/integration/test_prereqs.py patches
# ``_prereqs.shutil.which`` (the module attribute, not the imported
# name). This works because we ``import shutil`` and reference
# ``shutil.which(...)``. If a future refactor changes this to
# ``from shutil import which``, the test monkeypatch target becomes
# stale and the death tests pass vacuously. Keep the import form.
import shutil

import pytest

_REQUIRED_TOOLS: tuple[str, ...] = ("bash", "curl", "jq")


def require_e2e_prereqs_or_skip() -> None:
    """Skip the calling module if any required CLI tool is missing from PATH.

    Returns None when every tool is present. If any tool is missing,
    raises ``pytest.skip`` with ``allow_module_level=True`` and a
    message that names every missing tool — not just the first — so an
    operator fixes one round of installs instead of fix-and-rerun chains.

    BOUNDARY CONTRACT: ``allow_module_level=True`` is load-bearing.
    This helper is invoked at module import time from
    ``test_phase1_e2e.py``. Removing the flag converts a clean
    ``Skipped`` outcome into a confusing ``Failed: Using pytest.skip
    outside of a test`` error. The death tests in test_prereqs.py
    invoke this helper from inside test scope (where the flag is a
    no-op), so this docstring IS the regression guard for the flag.
    """
    missing = [tool for tool in _REQUIRED_TOOLS if shutil.which(tool) is None]
    if missing:
        names = ", ".join(missing)
        pytest.skip(
            f"GUR-99 e2e tests require {names} on PATH; not found",
            allow_module_level=True,
        )
    return None
