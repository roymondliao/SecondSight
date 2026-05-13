"""Env var accessors for SecondSight config (config-unification task-1).

Only two env vars are supported. Scope is intentionally minimal — adding
new env var overrides should be done deliberately, not by default.

Supported env vars:
    SECONDSIGHT_ANALYSIS_MODEL  — per-invocation model override (highest priority)
    SECONDSIGHT_DEFAULT_AGENT   — override for [analysis].default_agent

Empty string semantics:
    An env var set to "" (e.g. ``export SECONDSIGHT_ANALYSIS_MODEL=""``) is
    treated as "not set" and the helper returns None. This is consistent with
    the schema's empty-string-as-not-set contract (see config/schema.py).

    Rationale: shell scripts often clear overrides via ``VAR=""`` rather than
    ``unset VAR``. If we returned "" from the helper, the loader in task-2
    would see a non-None value and treat it as a valid model name, silently
    ignoring all TOML configuration. Normalizing "" → None prevents this
    class of silent misconfiguration.

    This normalization is the env layer's ONLY responsibility — it reads the
    OS environment and normalizes, nothing more.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Constants — the canonical env var names
# ---------------------------------------------------------------------------

ENV_ANALYSIS_MODEL: str = "SECONDSIGHT_ANALYSIS_MODEL"
"""Env var for per-invocation model override. Takes priority over all TOML layers."""

ENV_DEFAULT_AGENT: str = "SECONDSIGHT_DEFAULT_AGENT"
"""Env var for default_agent override. Takes priority over global config.toml."""

# CO-UPDATE CONTRACT: If you add a new env var constant here, you MUST also update
# _collect_sourced_values() in src/secondsight/cli/config_cmd.py to attribute the
# correct source label for fields driven by that env var. Failure to do so means
# `secondsight config show` silently misreports the source layer — a DC-1 regression.
__all__ = [
    "ENV_ANALYSIS_MODEL",
    "ENV_DEFAULT_AGENT",
    "get_env_analysis_model",
    "get_env_default_agent",
]


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------


def get_env_analysis_model() -> str | None:
    """Return the SECONDSIGHT_ANALYSIS_MODEL env var value, or None if unset/empty.

    Returns:
        The model name string if the env var is set and non-empty (after strip).
        None if the env var is absent OR set to an empty/whitespace-only string.

    Empty string normalization:
        ``export SECONDSIGHT_ANALYSIS_MODEL=""`` → returns None (not "").
        ``export SECONDSIGHT_ANALYSIS_MODEL="  "`` → returns None (whitespace stripped).
        This prevents silent promotion of an empty or whitespace-only string as a
        model override. The loader (task-2) can use ``if value:`` uniformly.
    """
    raw = os.environ.get(ENV_ANALYSIS_MODEL)
    if not raw:
        return None
    value = raw.strip()
    return value if value else None


def get_env_default_agent() -> str | None:
    """Return the SECONDSIGHT_DEFAULT_AGENT env var value, or None if unset/empty.

    Returns:
        The agent name string if the env var is set and non-empty (after strip).
        None if the env var is absent OR set to an empty/whitespace-only string.

    Empty string normalization:
        Same contract as get_env_analysis_model — "" and whitespace-only → None.
    """
    raw = os.environ.get(ENV_DEFAULT_AGENT)
    if not raw:
        return None
    value = raw.strip()
    return value if value else None
