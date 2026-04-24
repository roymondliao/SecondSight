"""Shared test utilities for fallback design investigation."""

import os

FALLBACK_DESIGN_PATH = os.path.join(
    os.path.dirname(__file__),
    "fallback-design.md"
)


def load_fallback_design() -> str:
    """Load the fallback design document. Fails on missing or empty file."""
    with open(FALLBACK_DESIGN_PATH, "r", encoding="utf-8") as f:
        content = f.read()
    assert content.strip(), (
        "fallback-design.md exists but is empty. "
        "An empty document vacuously passes all keyword tests."
    )
    return content
