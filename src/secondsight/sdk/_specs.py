"""ModelSpec — single source of truth for LLM model identity in the SDK layer.

Single-module definition shared by:
- sdk/model_selection.py (produces ModelSpec from config)
- sdk/router.py (consumes ModelSpec to construct provider clients)
- sdk/agent.py (receives ModelSpec list from router)

Do NOT redefine ModelSpec in any other module. Import from here.

If router.py is implemented before this file exists, it should import from
here and NOT define its own ModelSpec.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModelSpec:
    """Identity of one LLM model for the SDK router.

    Attributes:
        name: Provider-specific model identifier string (e.g. "claude-haiku-4-5-20251001").
        provider: Routing provider key (e.g. "anthropic", "openai", "google").
            The router uses this to construct the appropriate PydanticAI model instance.

    Frozen: ModelSpec instances are immutable. Two specs with the same
    (name, provider) are equal and hashable — safe to use in sets and as dict keys.
    """

    name: str
    provider: str
    api_key_env: str | None = None
