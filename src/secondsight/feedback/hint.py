"""Hint module interface — reserved for future use (GUR-108, P3B-4).

Hints are context-sensitive, session-scoped directives that fire when a
trigger pattern matches the current session state. Unlike conventions
(project-scoped, always-injected), hints are selectively injected based
on pattern matching against recent session behavior.

This module provides the empty-impl class that preserves the ``match`` /
``inject`` interface contract for future implementation. All methods
return no-op results. The adapter layer's ``inject_hint`` stub (P3B-5)
calls ``inject`` on Hint instances returned by ``match``.

Design assumptions:
    - Hint is a frozen dataclass (not Pydantic) because it is a
      transient DTO, never persisted directly. The Directive model
      with type=HINT is the persistence form.
    - ``match`` returns an empty list until the matching engine ships.
    - ``inject`` returns an empty string until the formatting engine ships.

Silent failure conditions:
    - Calling ``match`` always returns [] — no hints fire. This is
      correct for Phase 3B; the stub exists to lock the interface.
    - Calling ``inject`` on a Hint always returns "" — no content
      injected. Same rationale.

Ref: SD §4.2 (inject_hint reserved), §5.9.3
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Hint:
    """Adapter-facing DTO for a hint selected for injection.

    Mirrors Convention's role but for session-scoped directives.
    Fields are placeholders until the matching engine ships.
    """

    id: str
    instruction: str
    trigger_pattern: str
    confidence: float


class HintSelector:
    """Selects hints by matching trigger patterns against session context.

    Empty implementation for Phase 3B — all methods return no-op results.
    The interface is locked so that future implementation does not require
    adapter-layer changes.
    """

    def match(self, project_id: str, session_context: dict) -> list[Hint]:
        """Match trigger patterns against session context.

        Returns hints whose trigger_pattern matches the current session
        state. Always returns [] in this stub implementation.

        Args:
            project_id: the project to match hints for.
            session_context: dict of session state (event history,
                current segment, etc.). Shape TBD in future phase.

        Returns:
            Empty list (stub).
        """
        return []

    def inject(self, hint: Hint) -> str:
        """Format a hint for system prompt injection.

        Returns the formatted hint string for adapter consumption.
        Always returns "" in this stub implementation.

        Args:
            hint: the Hint to format.

        Returns:
            Empty string (stub).
        """
        return ""


__all__ = [
    "Hint",
    "HintSelector",
]
