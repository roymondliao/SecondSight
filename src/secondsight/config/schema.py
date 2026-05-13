"""Canonical config schema dataclasses for SecondSight (config-unification task-1).

All config sections (retention, analysis, model-selection) are defined here.
This module is the single source of truth for schema shape.

Import rules (MUST NOT be violated):
    - This module must NOT import from secondsight.analysis or secondsight.sdk.
    - This module MAY import from secondsight.storage for RetentionConfig re-export.
    - secondsight.analysis.config and secondsight.storage.retention may import FROM
      this module (one direction only — no cycles).

Design notes:

Empty string = not set (applies to all `model` and `default_agent` string fields):
    A value of "" in a TOML file is valid TOML but semantically means
    "clear this override, fall through to the next config layer". The schema
    preserves the empty string transparently — it does NOT reject it and does
    NOT coerce it to None. The loader (task-2) is responsible for detecting
    `if not value` to skip to the next layer. This contract is essential:
    if the schema coerced "" to None, the loader couldn't distinguish
    "user explicitly cleared the override" from "field never set".

    Affected fields: ProjectAnalysisConfig.model, ModelsConfig.claude_code,
    ModelsConfig.codex, ModelsConfig.opencode.

SecondSightConfig root:
    Aggregates all config sections for one resolution context (global + project).
    The loader in task-2 produces a SecondSightConfig by merging TOML layers
    and env var overlays. Individual sections can be accessed directly for
    subsystem consumers that don't need the full config.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Re-export RetentionConfig from storage.retention.
# This keeps RetentionConfig as the single class object — isinstance() checks
# across module boundaries will pass because it is the SAME class, not a copy.
# Only the schema portion is re-exported; purge logic stays in storage.retention.
from secondsight.storage.retention import RetentionConfig

__all__ = [
    # Model-selection schema (from analysis/config.py GUR-103 task-3)
    "FallbackModelsConfig",
    "ModelsConfig",
    "GlobalAnalysisConfig",
    "ProjectAnalysisConfig",
    "BUILTIN_DEFAULT_AGENT",
    "BUILTIN_FALLBACK_MODELS",
    # Re-exported from storage.retention (schema only, not purge logic)
    "RetentionConfig",
    # New in config-unification task-1
    "SecondSightConfig",
    "SecondSightConfigError",
]


# ---------------------------------------------------------------------------
# Unified config error
# ---------------------------------------------------------------------------


class SecondSightConfigError(Exception):
    """Raised when any config file is present but unreadable or has invalid values.

    This is the unified error class for the config layer. Individual subsystem
    errors (AnalysisConfigError in analysis.config, etc.) remain for backward
    compatibility, but the loader in task-2 may raise SecondSightConfigError for
    cross-section validation failures.

    NOT raised when files are absent — absent files → built-in defaults.
    """


# ---------------------------------------------------------------------------
# Model-selection schema (from analysis/config.py GUR-103 task-3)
# ---------------------------------------------------------------------------

BUILTIN_DEFAULT_AGENT: str = "claude_code"
BUILTIN_FALLBACK_MODELS: list[str] = ["gpt-4o-mini", "gemini-2.0-flash"]


@dataclass(frozen=True)
class FallbackModelsConfig:
    """Config for [analysis.models.fallback] section.

    Attributes:
        fallback_models: Ordered list of fallback model name strings.
            Empty list is valid (D13: strict-mode — no fallback).
            Default: ["gpt-4o-mini", "gemini-2.0-flash"] (SD §5.7.2 / D11).
    """

    fallback_models: list[str] = field(default_factory=lambda: list(BUILTIN_FALLBACK_MODELS))


@dataclass(frozen=True)
class ModelsConfig:
    """Config for [analysis.models] section.

    Attributes:
        claude_code: Model name for claude_code adapter. Empty = use SD §5.7.1 default.
        codex: Model name for codex adapter. Empty = raises ModelSelectionError.
        opencode: Model name for opencode adapter. Empty = raises ModelSelectionError.
        fallback: Fallback chain config.

    Empty string semantics:
        "" for any model field means "not explicitly configured" — the loader
        (task-2) uses `if not value` to detect this and falls through to the
        adapter default or raises ModelSelectionError as appropriate.
        The schema preserves "" transparently; it does NOT coerce to None.
    """

    claude_code: str = ""
    codex: str = ""
    opencode: str = ""
    fallback: FallbackModelsConfig = field(default_factory=FallbackModelsConfig)


@dataclass(frozen=True)
class GlobalAnalysisConfig:
    """Config for the [analysis] section in global config.toml.

    Combines model-selection keys (D7, D11) for the [analysis] section.

    Attributes:
        default_agent: Which agent type to use by default. "auto" is opt-in (D7).
            Default: "claude_code".
        models: Per-adapter model name overrides and fallback chain.
    """

    default_agent: str = BUILTIN_DEFAULT_AGENT
    models: ModelsConfig = field(default_factory=ModelsConfig)


@dataclass(frozen=True)
class ProjectAnalysisConfig:
    """Config for the [analysis] section in per-project config.toml.

    Attributes:
        model: Per-project model override. Non-empty beats global default_agent
            entirely (DT-3.3 / HP-1.3). Empty = use global resolution.

    Empty string semantics:
        model = "" means "not set at project scope — fall through to global
        resolution". The schema does NOT reject empty string. The loader
        (task-2) detects `if not cfg.model` to determine whether to apply
        the project-level override. If the schema were to coerce "" to None,
        the loader could not distinguish "user explicitly cleared the override
        by writing model = ''" from "field was never written to the TOML file".
        Both cases should fall through — but the schema must not make that
        decision on behalf of the loader.
    """

    model: str = ""


# ---------------------------------------------------------------------------
# SecondSightConfig root — aggregates all sections
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecondSightConfig:
    """Root config object for one resolution context (global + project merged).

    The loader in task-2 produces a SecondSightConfig by:
    1. Reading global ~/.secondsight/config.toml
    2. Reading per-project ~/.secondsight/projects/<pid>/config.toml
    3. Overlaying env var overrides (SECONDSIGHT_ANALYSIS_MODEL, SECONDSIGHT_DEFAULT_AGENT)
    4. Falling back to built-in defaults for any unset field

    Subsystem consumers that only need one section can access it directly:
        config.retention   → RetentionConfig (resolved TTLs + sources)
        config.analysis    → GlobalAnalysisConfig (default_agent, models)
        config.project_analysis → ProjectAnalysisConfig (per-project model override)

    Attributes:
        retention: Resolved retention policy (TTLs + source attributions).
        analysis: Global analysis config (default_agent, model overrides, fallback chain).
        project_analysis: Per-project analysis config (model override, empty = not set).
    """

    retention: RetentionConfig
    analysis: GlobalAnalysisConfig
    project_analysis: ProjectAnalysisConfig
