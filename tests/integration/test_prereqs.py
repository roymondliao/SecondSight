"""Death tests for tests/integration/_prereqs.require_e2e_prereqs_or_skip.

DT-1.1  Missing 'bash' triggers pytest.skip with the literal token "bash".
DT-1.2  Missing 'curl' triggers pytest.skip with the literal token "curl".
DT-1.3  Missing 'jq'   triggers pytest.skip with the literal token "jq".
DT-1.4  All three present → no skip raised.

The skip helper exists so that when an operator runs the e2e suite on a
machine without one of these tools, the failure mode is a NAMED skip,
never a silent green-pass and never an obscure subprocess error deep
inside a test. Each death test checks both halves of that contract:
the skip happens AND it carries the missing tool's name.

LIMITATION: these tests invoke the helper from inside test scope, where
``allow_module_level=True`` is a no-op. The flag's actual behavior at
module-import scope is documented as a BOUNDARY CONTRACT in _prereqs.py
itself; that docstring is the regression guard for the flag.
"""
from __future__ import annotations

from typing import Callable

import pytest

from tests.integration import _prereqs


def _patched_which(missing: set[str]) -> Callable[[str], str | None]:
    """Return a shutil.which replacement that reports `missing` as absent."""
    def _which(tool: str) -> str | None:
        if tool in missing:
            return None
        return f"/fake/path/to/{tool}"
    return _which


# --- DT-1.1..DT-1.3: each missing tool produces a named skip ---

@pytest.mark.parametrize("missing_tool", ["bash", "curl", "jq"])
def test_named_skip_on_missing_tool(
    monkeypatch: pytest.MonkeyPatch, missing_tool: str
) -> None:
    """A missing tool must raise pytest.skip whose message names that tool.

    Anti-silent-loss: if the helper raised a generic 'prerequisite missing'
    skip, an operator wouldn't know which tool to install. The named token
    is part of the contract, not cosmetic.
    """
    # See COUPLING CONTRACT in _prereqs.py — patching _prereqs.shutil.which
    # depends on _prereqs holding shutil as a module attribute.
    monkeypatch.setattr(
        _prereqs.shutil, "which", _patched_which({missing_tool})
    )

    with pytest.raises(pytest.skip.Exception) as excinfo:
        _prereqs.require_e2e_prereqs_or_skip()

    msg = str(excinfo.value)
    assert missing_tool in msg, (
        f"Skip message must name the missing tool {missing_tool!r}; "
        f"got message: {msg!r}"
    )


# --- DT-1.4: no skip when all tools present ---

def test_no_skip_when_all_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """All tools present → require_e2e_prereqs_or_skip returns None.

    Failure mode this guards against: a regression where the helper
    over-eagerly skips even on a fully-equipped machine, which would
    silently drop e2e coverage.
    """
    monkeypatch.setattr(_prereqs.shutil, "which", _patched_which(set()))
    assert _prereqs.require_e2e_prereqs_or_skip() is None
