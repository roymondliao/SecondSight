"""Pure model selection function for the SecondSight SDK layer.

`select_model()` resolves which LLM model to use for analysis, returning a
primary ModelSpec and an ordered fallback list. It is a pure function — no
side effects, no logging, no network calls. The only external interaction is
one optional call to `events_repo.get_latest_session_agent_type`, which is
passed in (not called internally).

## SD §5.7.1 Adapter-default table (source of truth for _ADAPTER_DEFAULTS)

WARNING: This constant mirrors SD §5.7.1. If that table evolves (e.g. codex
gets a real default after Phase 0), update _ADAPTER_DEFAULTS AND this docstring
in the same commit. The section reference is SD §5.7.1 — search for that string
if you need to find all dependents.

| Adapter      | Default model                  | Provider   |
|--------------|--------------------------------|------------|
| claude_code  | claude-haiku-4-5-20251001      | anthropic  |
| codex        | (raises ModelSelectionError)   | —          |
| opencode     | (raises ModelSelectionError)   | —          |

## Resolution order (5 steps)

1. If `project_config.analysis.model` is non-empty → use it as primary.
2. Else: resolve `agent_type` from `global_config.analysis.default_agent`.
3. If `agent_type == "auto"`: call `events_repo.get_latest_session_agent_type(project_id)`.
   If it returns None or empty → raise ModelSelectionError (NOT a silent fallback).
   If it returns a non-empty string → validate against _KNOWN_AGENT_TYPES before use.
4. Look up `global_config.analysis.models.<agent_type>`. If non-empty and not "auto",
   use it as primary. Else, look up _ADAPTER_DEFAULTS[agent_type] (may raise).
5. fallbacks = `global_config.analysis.models.fallback.fallback_models` → parse each.

## Assumptions

- This function is sync. The events_repo lookup is sync. If a future async
  repo is required, callers must NOT use asyncio.run() inside this function
  (that would make it impure). Instead, callers should await the repo method
  and pass the result as a pre-resolved `agent_type` argument, OR this
  function becomes async and the entire call chain must accommodate that.
  Document this implication before making this function async.

- `project_config.analysis.model` is a string (empty = "not set").
- `global_config.analysis.models.<agent>` is a string (empty = "not set").
- Provider inference for explicit model names: anthropic for claude-*, openai
  for gpt-*, google for gemini-*. Unknown prefixes default to "openai". This
  inference is intentionally minimal — project overrides that use exotic model
  names should configure provider explicitly (not yet supported in v1 schema).

## Agent-type validation

_KNOWN_AGENT_TYPES is the closed set of valid agent_type strings. Any value
from events_repo or config that is not in this set raises ModelSelectionError.
When a new adapter is added, update:
  1. _ADAPTER_DEFAULTS or _ADAPTER_ERROR_CONFIGS (one or both)
  2. ModelsConfig in analysis/config.py
  3. _KNOWN_AGENT_TYPES (implicitly — it is derived from _ADAPTER_DEFAULTS keys
     plus _ADAPTER_ERROR_CONFIGS keys plus "auto")
"""

from __future__ import annotations

from typing import Protocol

from secondsight.sdk._specs import ModelSpec


# ---------------------------------------------------------------------------
# Error types
# ---------------------------------------------------------------------------


class ModelSelectionError(Exception):
    """Raised when model resolution fails — typically because a required config
    key is missing for the resolved agent type.

    Attributes:
        suggested_config: A TOML snippet (ready to paste into config.toml) that
            would resolve this error. Tests assert this verbatim.

    Example:
        >>> raise ModelSelectionError(
        ...     "codex requires explicit configuration",
        ...     suggested_config="[analysis.models]\\ncodex = \\"<model-name>\\"",
        ... )
    """

    def __init__(self, message: str, *, suggested_config: str) -> None:
        super().__init__(message)
        self.suggested_config = suggested_config

    def __str__(self) -> str:
        # Include the config snippet in the string so str(err) contains it
        # (tests may assert on str(err) rather than err.suggested_config)
        return f"{self.args[0]}\n\nSuggested config:\n{self.suggested_config}"


# ---------------------------------------------------------------------------
# EventsRepo Protocol (structural typing — not an ABC)
# ---------------------------------------------------------------------------


class EventsRepoProtocol(Protocol):
    """Structural interface for the events repository subset used by select_model.

    Callers inject a concrete EventsRepository (or any object satisfying this
    interface). Tests inject a fake.

    Note on events table schema: The production events table (events_table.py)
    does not have an agent_type column as of Phase 1 schema. The concrete
    EventsRepository.get_latest_session_agent_type method returns None until
    the schema is extended. The 'auto' fallback to "claude_code" handles this
    safely. See scar report task-3 for the tracked assumption.
    """

    def get_latest_session_agent_type(self, project_id: str) -> str | None:
        """Return the agent_type string from the most-recent session for this
        project, or None if no sessions exist or agent_type is unavailable.
        """
        ...


class FallbackConfigProtocol(Protocol):
    fallback_models: list[str]


class ModelsConfigProtocol(Protocol):
    claude_code: str
    codex: str
    opencode: str
    fallback: FallbackConfigProtocol


class AnalysisConfigProtocol(Protocol):
    model: str
    default_agent: str
    models: ModelsConfigProtocol


class ProjectConfigProtocol(Protocol):
    analysis: AnalysisConfigProtocol


class GlobalConfigProtocol(Protocol):
    analysis: AnalysisConfigProtocol


# ---------------------------------------------------------------------------
# Adapter defaults — SD §5.7.1
# ---------------------------------------------------------------------------

# Provider inference by model-name prefix. This is intentionally minimal.
# See module docstring for the limitation on exotic model names.
# NOTE: This list is the single source of truth for provider inference.
# sdk_dispatcher.py imports _infer_provider from here (DRY — no copy in sdk_dispatcher).
# When adding a new model prefix, update this list AND the module docstring table.
_PROVIDER_PREFIXES: list[tuple[str, str]] = [
    ("claude-", "anthropic"),
    ("gpt-", "openai"),
    ("gemini-", "google"),
    ("o1-", "openai"),
    ("o3-", "openai"),
    ("o4-", "openai"),
]


def _infer_provider(model_name: str) -> str:
    """Infer the provider from the model name prefix.

    This is the single source of truth for provider inference. Both model_selection.py
    and sdk_dispatcher.py use this function — do NOT duplicate it.

    Raises:
        ValueError: If the model name prefix does not match any known provider.
            This forces explicit configuration rather than silently routing to the
            wrong provider (which would produce a 401-style error at dispatch time,
            harder to diagnose than a ValueError at construction time).

    Covered prefixes (see _PROVIDER_PREFIXES):
        claude-*  → anthropic
        gpt-*     → openai
        gemini-*  → google
        o1-*      → openai
        o3-*      → openai
        o4-*      → openai
    """
    lower = model_name.lower()
    for prefix, provider in _PROVIDER_PREFIXES:
        if lower.startswith(prefix):
            return provider
    raise ValueError(
        f"_infer_provider: unknown model name prefix for {model_name!r}. "
        f"Known prefixes: {[p for p, _ in _PROVIDER_PREFIXES]}. "
        f"For non-standard model names, configure provider explicitly "
        f"or pass a custom agent_factory (D6 escape hatch)."
    )


# SD §5.7.1 — source of truth. Update this dict and the module docstring table
# together when the SD table changes.
_ADAPTER_DEFAULTS: dict[str, ModelSpec] = {
    "claude_code": ModelSpec(
        name="claude-haiku-4-5-20251001",
        provider="anthropic",
    ),
    # codex: no default in Phase 0. Raises ModelSelectionError.
    # opencode: no default. Requires explicit analysis.model. Raises ModelSelectionError.
}

_ADAPTER_ERROR_CONFIGS: dict[str, str] = {
    "codex": (
        '[analysis.models]\ncodex = "<model-name>"  # set analysis.models.codex = "<model-name>"'
    ),
    "opencode": (
        "[analysis.models]\n"
        'opencode = "<model-name>"  '
        '# set analysis.models.opencode = "<model-name>"'
    ),
}

_ADAPTER_ERROR_MESSAGES: dict[str, str] = {
    "codex": (
        "codex model is not configured and has no Phase 0 default (SD §5.7.1). "
        "Configure [analysis.models.codex] in your config.toml. "
        "Key path: analysis.models.codex"
    ),
    "opencode": (
        "opencode requires explicit analysis.model configuration (SD §5.7.1). "
        "Configure [analysis.models.opencode] in your config.toml. "
        "Key path: analysis.models.opencode"
    ),
}

# Closed set of valid agent_type values. Derived from the union of adapters with
# defaults, adapters that raise (but are still valid config choices), and "auto".
# _verify_adapter_registry_consistency() checks this stays consistent with ModelsConfig.
_KNOWN_AGENT_TYPES: frozenset[str] = (
    frozenset(_ADAPTER_DEFAULTS.keys())
    | frozenset(_ADAPTER_ERROR_CONFIGS.keys())
    | frozenset({"auto"})
)

# _AUTO_NO_EVENTS_SUGGESTED_CONFIG: TOML snippet shown when auto-mode finds no sessions.
_AUTO_NO_EVENTS_SUGGESTED_CONFIG: str = (
    "[analysis]\n"
    'default_agent = "claude_code"  # or "codex" or "opencode"\n'
    "# auto-mode requires sessions to be observed first;\n"
    "# set an explicit default_agent in the meantime."
)


def _verify_adapter_registry_consistency() -> None:
    """Sanity check: ModelsConfig fields must match _ADAPTER_DEFAULTS + _ADAPTER_ERROR_CONFIGS.

    Run at module import to catch drift early. ModelsConfig and _ADAPTER_DEFAULTS /
    _ADAPTER_ERROR_CONFIGS are two sources-of-truth for the closed adapter type set;
    if they diverge, model selection silently misroutes new adapters.

    Raises:
        RuntimeError: If the sets diverge. This fires at import time — fail fast,
            never silently.
    """
    from dataclasses import fields as dc_fields

    from secondsight.analysis.config import ModelsConfig

    config_field_names = frozenset(f.name for f in dc_fields(ModelsConfig) if f.name != "fallback")
    # Known adapter keys: those with defaults + those that intentionally raise
    all_adapter_keys = frozenset(_ADAPTER_DEFAULTS.keys()) | frozenset(
        _ADAPTER_ERROR_CONFIGS.keys()
    )

    if config_field_names != all_adapter_keys:
        diff_msg = (
            f"_ADAPTER_DEFAULTS+_ADAPTER_ERROR_CONFIGS keys ({sorted(all_adapter_keys)}) "
            f"!= ModelsConfig fields ({sorted(config_field_names)}). "
            "When adding a new adapter, update BOTH ModelsConfig in analysis/config.py "
            "AND _ADAPTER_DEFAULTS (if it has a default) or _ADAPTER_ERROR_CONFIGS "
            "(if it raises without explicit config)."
        )
        raise RuntimeError(diff_msg)


_verify_adapter_registry_consistency()


def _resolve_adapter_default(agent_type: str) -> ModelSpec:
    """Return the SD §5.7.1 default ModelSpec for agent_type, or raise.

    Returns the default ModelSpec for known agent types that have one (claude_code).
    Raises ModelSelectionError for adapter types without a configured default
    (codex, opencode) — those adapters require explicit model configuration.

    Note: callers must validate agent_type against _KNOWN_AGENT_TYPES BEFORE
    calling this function. An unvalidated agent_type that is not in
    _ADAPTER_DEFAULTS and not in _ADAPTER_ERROR_CONFIGS will get a generic
    error message with no source information.
    """
    if agent_type in _ADAPTER_DEFAULTS:
        return _ADAPTER_DEFAULTS[agent_type]

    message = _ADAPTER_ERROR_MESSAGES.get(
        agent_type,
        (
            f"Unknown agent type '{agent_type}' has no model default. "
            f"Configure analysis.models.{agent_type} in your config.toml."
        ),
    )
    suggested = _ADAPTER_ERROR_CONFIGS.get(
        agent_type,
        (
            f"[analysis.models]\n"
            f'{agent_type} = "<model-name>"  '
            f'# set analysis.models.{agent_type} = "<model-name>"'
        ),
    )
    raise ModelSelectionError(message, suggested_config=suggested)


# ---------------------------------------------------------------------------
# Fallback model parsing
# ---------------------------------------------------------------------------


def _parse_fallback_models(model_names: list[str]) -> list[ModelSpec]:
    """Convert fallback model name strings to ModelSpec instances.

    Provider is inferred from name prefix. See module docstring.
    """
    return [ModelSpec(name=n, provider=_infer_provider(n)) for n in model_names]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def select_model(
    project_id: str,
    project_config: ProjectConfigProtocol,
    global_config: GlobalConfigProtocol,
    events_repo: EventsRepoProtocol,
) -> tuple[ModelSpec, list[ModelSpec]]:
    """Resolve the primary and fallback model specs for one analysis run.

    Pure function — exactly one I/O path: events_repo.get_latest_session_agent_type,
    called at most once (only when default_agent='auto' AND no project override).
    No side effects, no logging, no network.

    Args:
        project_id: The project identifier. Used only for the events_repo lookup.
        project_config: Per-project config object. Must have
            `.analysis.model: str` (empty = not set).
        global_config: Global config object. Must have:
            - `.analysis.default_agent: str` ("claude_code" | "auto" | ...)
            - `.analysis.models.<agent>: str` (empty = use adapter default)
            - `.analysis.models.fallback.fallback_models: list[str]`
        events_repo: Repository for querying which agent type was last used
            for this project. Injected; not constructed internally.

    Returns:
        (primary, fallbacks): primary is the resolved ModelSpec; fallbacks is
        an ordered list of ModelSpec instances for chain-fallback.

    Raises:
        ModelSelectionError: The resolved agent type has no configured model
            and no SD §5.7.1 default. Includes `suggested_config` attribute
            with the TOML snippet that would resolve the issue.

    Assumptions (see module docstring for full detail):
        - project_config.analysis.model and global_config.analysis.models.*
          are strings, never None.
        - fallback_models is a list[str], never None (empty list is valid).
    """
    # Step 1: Per-project model override takes highest precedence.
    project_model = project_config.analysis.model
    if project_model:
        primary = ModelSpec(
            name=project_model,
            provider=_infer_provider(project_model),
        )
        fallback_names = global_config.analysis.models.fallback.fallback_models
        return primary, _parse_fallback_models(fallback_names)

    # Step 2: Resolve agent_type from global default_agent.
    agent_type = global_config.analysis.default_agent

    # Step 3: If 'auto', detect from most-recent session.
    if agent_type == "auto":
        detected = events_repo.get_latest_session_agent_type(project_id)
        if not detected:
            # No sessions observed yet — refuse to silently substitute claude_code.
            # A codex-only project with auto configured would otherwise be billed
            # for Anthropic with no signal. Raise with actionable config guidance.
            raise ModelSelectionError(
                "default_agent='auto' but no session has been observed for this "
                "project; auto-inference returned None. "
                "Set an explicit default_agent until sessions are available.",
                suggested_config=_AUTO_NO_EVENTS_SUGGESTED_CONFIG,
            )
        agent_type = detected

    # Validate agent_type against the closed set BEFORE using it in getattr.
    # Values from events_repo may be garbage (wrong case, unknown adapter name).
    # A bad value here causes silent misrouting — catch it explicitly.
    if agent_type not in _KNOWN_AGENT_TYPES:
        raise ModelSelectionError(
            f"Unknown agent_type {agent_type!r} (got from events_repo or config). "
            f"Valid values: {sorted(_KNOWN_AGENT_TYPES)}. "
            "If this value came from events_repo, the events table may contain "
            "stale or corrupted data. Check events_repo.get_latest_session_agent_type.",
            suggested_config=(
                "[analysis]\n"
                f'default_agent = "claude_code"  '
                f"# replace with a valid value from: "
                f"{sorted(_KNOWN_AGENT_TYPES - {'auto'})}"
            ),
        )

    # Step 4: Look up global per-agent model override, or fall back to adapter default.
    agent_model: str = getattr(
        global_config.analysis.models,
        agent_type,
        "",
    )
    if agent_model and agent_model != "auto":
        primary = ModelSpec(
            name=agent_model,
            provider=_infer_provider(agent_model),
        )
    else:
        primary = _resolve_adapter_default(agent_type)

    # Step 5: Resolve fallbacks from global config.
    fallback_names = global_config.analysis.models.fallback.fallback_models
    return primary, _parse_fallback_models(fallback_names)
