"""Shared dispatch Protocol for CLI and SDK analysis dispatchers (Task 5 revision).

Both CLIAnalysisDispatcher and SDKAnalysisDispatcher conform to this Protocol.
Task 6's ProjectAnalysisRuntime.dispatch() calls EITHER via this uniform surface.

Design contract:
  - project_root is required by the CLI path (subprocess cwd) but ignored by the SDK path.
  - Passing project_root=None to CLIAnalysisDispatcher raises ValueError at entry.
  - Passing project_root=anything to SDKAnalysisDispatcher is ignored (not an error).
  - This asymmetry is an honest Protocol-level fact: callers always pass project_root;
    the SDK silently ignores it rather than requiring callers to branch on mode.

Death case this closes:
  - Without a shared Protocol, Task 6 would be forced to branch on dispatcher type
    (if isinstance(dispatcher, CLIAnalysisDispatcher): ...) — the exact mode-awareness-
    leak pattern the architecture forbids. The Protocol makes both dispatchers uniform
    to Task 6's caller without leaking mode knowledge upward.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from secondsight.analysis.output import AnalysisOutput


class AnalysisDispatcher(Protocol):
    """Cross-mode dispatch contract. Both CLI and SDK dispatchers conform to this.

    Callers (Task 6's ProjectAnalysisRuntime) call dispatch() uniformly regardless
    of which mode is active. The mode difference is encapsulated inside each dispatcher.

    Args:
        session_id: ID of the session being analyzed.
        session_payload: The session data dict to include in the prompt/request.
        project_root: Absolute path to the project root.
            - CLI dispatcher: REQUIRED. Raises ValueError if None.
            - SDK dispatcher: IGNORED. Accepted but not used (SDK has no subprocess cwd).
            Callers should always pass project_root; dispatchers own the interpretation.

    Returns:
        AnalysisOutput. Never raises (exception-free dispatch contract).
    """

    async def dispatch(
        self,
        session_id: str,
        session_payload: dict,
        project_root: Path | None = None,
    ) -> AnalysisOutput: ...


__all__ = ["AnalysisDispatcher"]
