"""Agent adapter layer (P1-9, P1-10) — single source of truth for SD §4.2.

The adapter layer owns BOTH observation (normalize hook payload → PartialEvent)
and feedback (inject_convention, reserved inject_hint) per SD §4.2. As of
task-3 of phase1-adapters (GUR-97), this package is the only home for the
agent-adapter abstraction; the legacy `secondsight.api.normalizer` module is
deleted and the duck-typed `Normalizer` Protocol is replaced by `AgentAdapter`
(ABC). Re-export of legacy names is intentionally NOT provided (plan G4 — no
shim, single-PR migration) so a stale import fails loudly at import-time.
"""

from secondsight.adapters.base import (
    AdapterRegistry,
    AgentAdapter,
    NoAdapterError,
)
from secondsight.adapters.claude_code import ClaudeCodeAdapter
from secondsight.adapters.identity import IdentityAdapter

__all__ = [
    "AdapterRegistry",
    "AgentAdapter",
    "ClaudeCodeAdapter",
    "IdentityAdapter",
    "NoAdapterError",
]
