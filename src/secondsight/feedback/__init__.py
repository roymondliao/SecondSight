"""Feedback layer — convention injection + directive lifecycle (GUR-105).

This package owns:
- Convention selection with token budget enforcement (convention.py)
- Directive lifecycle state machine (lifecycle.py)
- Effectiveness tracking for conventions (effectiveness.py)

The `Convention` type is the adapter-facing DTO. It carries only the fields
needed for injection formatting — not the full Directive row. This prevents
the adapter layer from depending on storage-layer details.
"""

from secondsight.feedback.convention import Convention, ConventionSelector

__all__ = [
    "Convention",
    "ConventionSelector",
]
