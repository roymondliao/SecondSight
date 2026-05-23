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

from secondsight.config.constants import (
    BUILTIN_ANALYSIS_MAX_RETRY_COUNT_CAP,
    BUILTIN_ANALYSIS_OUTPUT_REPAIR_MAX_ATTEMPTS,
    BUILTIN_ANALYSIS_RETRY_FEEDBACK_MAX_CHARS,
)

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
    # New in config-unification task-1 / analysis-mode-toggle task-1
    "SecondSightConfig",
    "SecondSightConfigError",
    # analysis-mode-toggle task-1: [general], [providers.*], [analysis.cli/sdk]
    "GeneralConfig",
    "ProviderAnthropicConfig",
    "ProviderOpenAIConfig",
    "ProviderCustomConfig",
    "ProvidersConfig",
    "AnalysisCLIModelsConfig",
    "AnalysisCLIConfig",
    "AnalysisSDKConfig",
    "AnalysisRetryConfig",
    "AnalysisConfig",
    "FeedbackConfig",
    "DirectiveLifecycleConfig",
    # SDK model defaults — named constants (single source of truth for schema + loader)
    "BUILTIN_SDK_PRIMARY_MODEL",
    "BUILTIN_SDK_FALLBACK_MODEL",
    "BUILTIN_ANALYSIS_TIMEOUT_SECONDS",
    "BUILTIN_ANALYSIS_OUTPUT_REPAIR_MAX_ATTEMPTS",
    "BUILTIN_ANALYSIS_RETRY_FEEDBACK_MAX_CHARS",
    "BUILTIN_ANALYSIS_MAX_RETRY_COUNT_CAP",
    "BUILTIN_FEEDBACK_CONVENTION_INJECTION_BUDGET",
    "BUILTIN_FEEDBACK_CONVENTION_TOP_N",
    "BUILTIN_FEEDBACK_HIT_INJECTION_ENABLED",
    "BUILTIN_DIRECTIVE_LIFECYCLE_CAPACITY_CEILING",
    "BUILTIN_DIRECTIVE_LIFECYCLE_BOOST_DELTA",
    "BUILTIN_DIRECTIVE_LIFECYCLE_DECAY_DELTA",
    "BUILTIN_DIRECTIVE_LIFECYCLE_MISS_GRACE",
    "BUILTIN_DIRECTIVE_LIFECYCLE_OBSOLETE_THRESHOLD",
    "BUILTIN_DIRECTIVE_LIFECYCLE_REVISION_CAP",
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

# SDK model defaults — single source of truth referenced by both AnalysisSDKConfig
# defaults and _build_analysis_config fallbacks in loader.py. Updating one of these
# constants changes both the schema default and the loader fallback simultaneously.
#
# Both defaults are empty strings on purpose (2026-05-15 revision):
# SDK mode calls provider APIs directly, so the operator's provider/model id is
# not predictable from SecondSight's side. Shipping a non-empty default would
# silently bind every SDK user to one specific provider's model id — wrong by
# construction. Empty default → precheck (config/precheck.py:279) rejects sdk mode
# until the operator explicitly sets a model. Explicit failure beats silent bind.
BUILTIN_SDK_PRIMARY_MODEL: str = ""
BUILTIN_SDK_FALLBACK_MODEL: str = ""
BUILTIN_ANALYSIS_TIMEOUT_SECONDS: int = 300
BUILTIN_FEEDBACK_CONVENTION_INJECTION_BUDGET: int = 2000
BUILTIN_FEEDBACK_CONVENTION_TOP_N: int = 15
BUILTIN_FEEDBACK_HIT_INJECTION_ENABLED: bool = True
BUILTIN_DIRECTIVE_LIFECYCLE_CAPACITY_CEILING: int = 15
BUILTIN_DIRECTIVE_LIFECYCLE_BOOST_DELTA: float = 0.15
BUILTIN_DIRECTIVE_LIFECYCLE_DECAY_DELTA: float = 0.10
BUILTIN_DIRECTIVE_LIFECYCLE_MISS_GRACE: int = 2
BUILTIN_DIRECTIVE_LIFECYCLE_OBSOLETE_THRESHOLD: float = 0.20
BUILTIN_DIRECTIVE_LIFECYCLE_REVISION_CAP: int = 3


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
# analysis-mode-toggle task-1: new section dataclasses
# ---------------------------------------------------------------------------
# These dataclasses represent the canonical schema documented in the repository
# root config.example.toml. Field names, defaults, and nesting must match that
# file.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GeneralConfig:
    """Config for [general] section.

    Attributes:
        mode: Analysis dispatch path. "cli" (default) spawns the coding agent CLI.
              "sdk" uses PydanticAI + direct LLM provider API calls.
              Only "cli" and "sdk" are valid values; loader raises SecondSightConfigError
              for any other value.
        log_level: Global loguru verbosity. Valid: "debug", "info", "warning", "error".
    """

    mode: str = "cli"
    log_level: str = "info"


@dataclass(frozen=True)
class ProviderAnthropicConfig:
    """Config for [providers.anthropic] section.

    Attributes:
        ANTHROPIC_API_KEY: API key for Anthropic. Empty string = unset (per Decision E1:
            no implicit env fallback). Use "${ANTHROPIC_API_KEY}" in TOML to inject from env.
    """

    ANTHROPIC_API_KEY: str = ""


@dataclass(frozen=True)
class ProviderOpenAIConfig:
    """Config for [providers.openai] section."""

    OPENAI_API_KEY: str = ""


@dataclass(frozen=True)
class ProviderCustomConfig:
    """Config for [providers.custom] — OpenAI-compatible custom endpoint.

    Attributes:
        API_KEY: API key for the custom endpoint. Empty = unset.
        base_url: Base URL for the endpoint. Empty = unset.
    """

    API_KEY: str = ""
    base_url: str = ""


@dataclass(frozen=True)
class ProvidersConfig:
    """Config for [providers.*] sections aggregate.

    All providers default to empty credentials (unset).
    SDK mode pre-check (Task 6) validates at least one provider is resolvable.
    """

    anthropic: ProviderAnthropicConfig = field(default_factory=ProviderAnthropicConfig)
    openai: ProviderOpenAIConfig = field(default_factory=ProviderOpenAIConfig)
    custom: ProviderCustomConfig = field(default_factory=ProviderCustomConfig)


@dataclass(frozen=True)
class AnalysisCLIModelsConfig:
    """Config for [analysis.cli.models] section.

    Per-agent model override. Empty string = let the coding agent use its own
    default model (Decision E5). Non-empty = pass --model <value> to the agent CLI.

    Attributes:
        claude_code: Model override for the claude CLI. "" = use claude's own default.
        codex: Model override for the codex CLI. "" = use codex's own default.
        opencode: Schema slot preserved; CLI dispatch is out of scope for this effort.
    """

    claude_code: str = ""
    codex: str = ""
    opencode: str = ""


@dataclass(frozen=True)
class AnalysisCLIConfig:
    """Config for [analysis.cli] section.

    Read only when [general].mode == "cli".

    Attributes:
        default_agent: Which coding agent to spawn. "auto" resolves to the agent
            selected at `secondsight init` time (via ~/.secondsight/state.json).
            Other values: "claude_code", "codex". "opencode" is rejected by Task 6.
        models: Per-agent model overrides.
    """

    default_agent: str = "auto"
    models: AnalysisCLIModelsConfig = field(default_factory=AnalysisCLIModelsConfig)


@dataclass(frozen=True)
class AnalysisSDKConfig:
    """Config for [analysis.sdk] section.

    Read only when [general].mode == "sdk".

    Attributes:
        primary_model: Primary model for PydanticAI agent. Required when mode == "sdk".
            Default "" — see BUILTIN_SDK_PRIMARY_MODEL for why we ship no default.
        fallback_model: Single fallback model. Empty = no fallback (Decision E3:
            collapsed from list to single string).
            Default "" — same rationale as primary_model.

    Defaults reference BUILTIN_SDK_PRIMARY_MODEL and BUILTIN_SDK_FALLBACK_MODEL constants
    so the schema default and loader fallback remain in sync (single source of truth).
    Both constants are empty strings post-2026-05-15: SDK mode requires explicit
    operator choice because provider/model is not predictable; precheck enforces.
    """

    primary_model: str = BUILTIN_SDK_PRIMARY_MODEL
    fallback_model: str = BUILTIN_SDK_FALLBACK_MODEL


@dataclass(frozen=True)
class AnalysisRetryConfig:
    """Config for [analysis.retry] section.

    Attributes:
        enabled: Master kill switch for output-repair retry logic.
        output_repair_max_attempts: How many model re-tries are allowed for
            output/validation failures after local normalization is exhausted.
            This is a policy value and must be bounded by the loader against
            BUILTIN_ANALYSIS_MAX_RETRY_COUNT_CAP.
        feedback_max_chars: Max number of characters the retry feedback builder
            may feed back into the model prompt.
    """

    enabled: bool = True
    output_repair_max_attempts: int = BUILTIN_ANALYSIS_OUTPUT_REPAIR_MAX_ATTEMPTS
    feedback_max_chars: int = BUILTIN_ANALYSIS_RETRY_FEEDBACK_MAX_CHARS

    def __post_init__(self) -> None:
        """Validate retry policy even when callers construct config in memory."""

        if type(self.enabled) is not bool:
            raise SecondSightConfigError(
                f"[analysis.retry].enabled must be a boolean; got {type(self.enabled).__name__!r}"
            )
        if type(self.output_repair_max_attempts) is not int:
            raise SecondSightConfigError(
                "[analysis.retry].output_repair_max_attempts must be an integer; "
                f"got {type(self.output_repair_max_attempts).__name__!r}"
            )
        if self.output_repair_max_attempts < 0:
            raise SecondSightConfigError(
                "[analysis.retry].output_repair_max_attempts must be >= 0; "
                f"got {self.output_repair_max_attempts!r}"
            )
        if self.output_repair_max_attempts > BUILTIN_ANALYSIS_MAX_RETRY_COUNT_CAP:
            raise SecondSightConfigError(
                "[analysis.retry].output_repair_max_attempts exceeds the hard cap "
                f"{BUILTIN_ANALYSIS_MAX_RETRY_COUNT_CAP}; "
                f"got {self.output_repair_max_attempts!r}"
            )
        if type(self.feedback_max_chars) is not int:
            raise SecondSightConfigError(
                "[analysis.retry].feedback_max_chars must be an integer; "
                f"got {type(self.feedback_max_chars).__name__!r}"
            )
        if self.feedback_max_chars <= 0:
            raise SecondSightConfigError(
                f"[analysis.retry].feedback_max_chars must be > 0; got {self.feedback_max_chars!r}"
            )


@dataclass(frozen=True)
class AnalysisConfig:
    """Config for [analysis] section aggregate (new, analysis-mode-toggle task-1).

    NOTE: This is a DIFFERENT class from secondsight.analysis.config.AnalysisConfig.
    That class is the per-project TOML reader with .load().
    This class is the global config aggregate covering [analysis], [analysis.cli],
    and [analysis.sdk] sections.

    Attributes:
        timeout_seconds: Max wall-clock time for a single analysis run (both modes).
            Default references BUILTIN_ANALYSIS_TIMEOUT_SECONDS (single source of truth).
        cli: Config for CLI dispatch mode (read when general.mode == "cli").
        sdk: Config for SDK dispatch mode (read when general.mode == "sdk").
        retry: Shared output-repair retry policy.
    """

    timeout_seconds: int = BUILTIN_ANALYSIS_TIMEOUT_SECONDS
    cli: AnalysisCLIConfig = field(default_factory=AnalysisCLIConfig)
    sdk: AnalysisSDKConfig = field(default_factory=AnalysisSDKConfig)
    retry: AnalysisRetryConfig = field(default_factory=AnalysisRetryConfig)


@dataclass(frozen=True)
class FeedbackConfig:
    """Config for [feedback] section.

    Attributes:
        convention_injection_budget: SessionStart convention token budget.
        convention_top_n: Max active conventions considered by future aggregation paths.
        hit_injection_enabled: Whether to run the agent-native hit injection path via
            the UserPromptSubmit hook.  Defaults to True.  Set to False to disable
            the executability self-check meta-instruction without removing the hook.
    """

    convention_injection_budget: int = BUILTIN_FEEDBACK_CONVENTION_INJECTION_BUDGET
    convention_top_n: int = BUILTIN_FEEDBACK_CONVENTION_TOP_N
    hit_injection_enabled: bool = BUILTIN_FEEDBACK_HIT_INJECTION_ENABLED

    def __post_init__(self) -> None:
        if type(self.convention_injection_budget) is not int:
            raise SecondSightConfigError(
                "[feedback].convention_injection_budget must be an integer; "
                f"got {type(self.convention_injection_budget).__name__!r}"
            )
        if self.convention_injection_budget <= 0:
            raise SecondSightConfigError(
                "[feedback].convention_injection_budget must be > 0; "
                f"got {self.convention_injection_budget!r}"
            )
        if type(self.convention_top_n) is not int:
            raise SecondSightConfigError(
                "[feedback].convention_top_n must be an integer; "
                f"got {type(self.convention_top_n).__name__!r}"
            )
        if self.convention_top_n <= 0:
            raise SecondSightConfigError(
                f"[feedback].convention_top_n must be > 0; got {self.convention_top_n!r}"
            )
        if type(self.hit_injection_enabled) is not bool:
            raise SecondSightConfigError(
                "[feedback].hit_injection_enabled must be a boolean; "
                f"got {type(self.hit_injection_enabled).__name__!r}"
            )


@dataclass(frozen=True)
class DirectiveLifecycleConfig:
    """Config for [directive_lifecycle] section."""

    capacity_ceiling: int = BUILTIN_DIRECTIVE_LIFECYCLE_CAPACITY_CEILING
    boost_delta: float = BUILTIN_DIRECTIVE_LIFECYCLE_BOOST_DELTA
    decay_delta: float = BUILTIN_DIRECTIVE_LIFECYCLE_DECAY_DELTA
    miss_grace: int = BUILTIN_DIRECTIVE_LIFECYCLE_MISS_GRACE
    obsolete_threshold: float = BUILTIN_DIRECTIVE_LIFECYCLE_OBSOLETE_THRESHOLD
    revision_cap: int = BUILTIN_DIRECTIVE_LIFECYCLE_REVISION_CAP

    def __post_init__(self) -> None:
        if type(self.capacity_ceiling) is not int:
            raise SecondSightConfigError(
                "[directive_lifecycle].capacity_ceiling must be an integer; "
                f"got {type(self.capacity_ceiling).__name__!r}"
            )
        if self.capacity_ceiling <= 0:
            raise SecondSightConfigError(
                f"[directive_lifecycle].capacity_ceiling must be > 0; got {self.capacity_ceiling!r}"
            )
        if type(self.boost_delta) not in (int, float):
            raise SecondSightConfigError(
                "[directive_lifecycle].boost_delta must be numeric; "
                f"got {type(self.boost_delta).__name__!r}"
            )
        if float(self.boost_delta) <= 0:
            raise SecondSightConfigError(
                f"[directive_lifecycle].boost_delta must be > 0; got {self.boost_delta!r}"
            )
        if type(self.decay_delta) not in (int, float):
            raise SecondSightConfigError(
                "[directive_lifecycle].decay_delta must be numeric; "
                f"got {type(self.decay_delta).__name__!r}"
            )
        if float(self.decay_delta) <= 0:
            raise SecondSightConfigError(
                f"[directive_lifecycle].decay_delta must be > 0; got {self.decay_delta!r}"
            )
        if type(self.miss_grace) is not int:
            raise SecondSightConfigError(
                "[directive_lifecycle].miss_grace must be an integer; "
                f"got {type(self.miss_grace).__name__!r}"
            )
        if self.miss_grace <= 0:
            raise SecondSightConfigError(
                f"[directive_lifecycle].miss_grace must be > 0; got {self.miss_grace!r}"
            )
        if type(self.obsolete_threshold) not in (int, float):
            raise SecondSightConfigError(
                "[directive_lifecycle].obsolete_threshold must be numeric; "
                f"got {type(self.obsolete_threshold).__name__!r}"
            )
        threshold = float(self.obsolete_threshold)
        if threshold <= 0 or threshold >= 1:
            raise SecondSightConfigError(
                "[directive_lifecycle].obsolete_threshold must be between 0 and 1; "
                f"got {self.obsolete_threshold!r}"
            )
        if type(self.revision_cap) is not int:
            raise SecondSightConfigError(
                "[directive_lifecycle].revision_cap must be an integer; "
                f"got {type(self.revision_cap).__name__!r}"
            )
        if self.revision_cap <= 0:
            raise SecondSightConfigError(
                f"[directive_lifecycle].revision_cap must be > 0; got {self.revision_cap!r}"
            )


# ---------------------------------------------------------------------------
# SecondSightConfig root — aggregates all sections
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SecondSightConfig:
    """Root config object for one resolution context (global + project merged).

    The loader produces a SecondSightConfig by:
    1. Reading global ~/.secondsight/config.toml
    2. Reading per-project ~/.secondsight/projects/<pid>/config.toml
    3. Overlaying env var overrides (SECONDSIGHT_ANALYSIS_MODEL, SECONDSIGHT_DEFAULT_AGENT)
    4. Falling back to built-in defaults for any unset field

    Subsystem consumers that only need one section can access it directly:
        config.retention        → RetentionConfig (resolved TTLs + sources)
        config.general          → GeneralConfig (mode, log_level)
        config.providers        → ProvidersConfig (API keys for SDK mode)
        config.analysis         → AnalysisConfig (new: cli + sdk subsections + timeout)
        config.project_analysis → ProjectAnalysisConfig (per-project model override)

    Backward-compat note:
        The old `analysis: GlobalAnalysisConfig` field was the flat model-selection
        config from GUR-103. It is REPLACED here by `analysis: AnalysisConfig` (new).
        The GlobalAnalysisConfig class is preserved for warn-and-ignore detection
        of legacy flat configs (DC12) and for backward-compat imports.
        Callers that previously accessed `config.analysis.default_agent` must now
        access `config.analysis.cli.default_agent`.

    Attributes:
        retention: Resolved retention policy (TTLs + source attributions).
        general: General config (mode, log_level). NEW.
        providers: Provider API keys. NEW.
        analysis: Analysis config aggregate (cli + sdk subsections + timeout). NEW.
        analysis_global: GlobalAnalysisConfig preserved for backward compat.
            Used by select_model() and other GUR-103 consumers until Task 6 migrates them.
            Deprecated: Task 6 will replace these uses with analysis.cli.* / analysis.sdk.*
            access paths. After Task 6 lands, this field should be removed.
            After warn-and-ignore detection runs in the loader, analysis_global.default_agent
            and analysis.cli.default_agent will not diverge for the same input config.
        project_analysis: Per-project analysis config (model override, empty = not set).
        feedback: Feedback config for convention injection.
        directive_lifecycle: Config for autonomous directive lifecycle policy.
    """

    retention: RetentionConfig
    general: GeneralConfig
    providers: ProvidersConfig
    analysis: AnalysisConfig
    analysis_global: GlobalAnalysisConfig
    project_analysis: ProjectAnalysisConfig
    feedback: FeedbackConfig = field(default_factory=FeedbackConfig)
    directive_lifecycle: DirectiveLifecycleConfig = field(default_factory=DirectiveLifecycleConfig)
