"""Feedback layer — convention injection + directive lifecycle (GUR-105, GUR-108).

This package owns:
- Convention selection with token budget enforcement (convention.py)
- Directive lifecycle state machine (lifecycle.py)
- Effectiveness tracking for conventions (effectiveness.py)
- Hint module interface — reserved for future use (hint.py, GUR-108 P3B-4)

The `Convention` and `Hint` types are adapter-facing DTOs. They carry only
the fields needed for injection formatting — not the full Directive row.
This prevents the adapter layer from depending on storage-layer details.
"""

from secondsight.feedback.convention import Convention, ConventionSelector
from secondsight.feedback.hint import Hint, HintSelector

__all__ = [
    "Convention",
    "ConventionSelector",
    "Hint",
    "HintSelector",
]
