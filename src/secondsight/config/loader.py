"""Unified config loader for SecondSight (config-unification task-2).

Merges TOML layers + env var overrides to produce a SecondSightConfig.

Priority chain (highest → lowest):
    1. Env var (SECONDSIGHT_ANALYSIS_MODEL, SECONDSIGHT_DEFAULT_AGENT)
    2. Per-project config.toml (~/.secondsight/projects/<pid>/config.toml)
    3. Global config.toml (~/.secondsight/config.toml)
    4. Built-in defaults

.env loading:
    load_global_config() loads ~/.secondsight/.env into os.environ before
    reading TOML, using python-dotenv with override=False. This means shell
    environment variables always win over .env file values. This function
    has a side effect on os.environ (process-wide).

${VAR} interpolation:
    After parsing TOML, all string leaf values are scanned for ${VAR_NAME}
    patterns (uppercase only: [A-Z_][A-Z0-9_]*). Matching vars are expanded
    from os.environ. Missing or empty var → SecondSightConfigError.

    Intentional limitation: lowercase patterns like ${my_var} are NOT expanded
    and are returned as-is. This is documented behaviour — only UPPERCASE env
    var names are interpolated. See _interpolate_vars() docstring.

Empty string = not set:
    A field set to "" in TOML means "clear this override, fall through to the
    next config layer". The schema preserves "" transparently; this loader uses
    `if not value` to detect it and skip to the next layer.

Import constraints (must not be violated):
    - This module imports ONLY from config/schema.py, config/env.py, and
      storage/retention.py (for _resolve_ttl_field and related helpers).
    - This module must NOT import from analysis/ or sdk/ (circular import risk).
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from secondsight.config.env import (
    get_env_analysis_model,
    get_env_default_agent,
)
from secondsight.config.schema import (
    BUILTIN_DEFAULT_AGENT,
    FallbackModelsConfig,
    GlobalAnalysisConfig,
    ModelsConfig,
    ProjectAnalysisConfig,
    SecondSightConfig,
    SecondSightConfigError,
)
from secondsight.storage.retention import (
    BUILTIN_DEFAULT_ANALYSIS_TTL_DAYS,
    BUILTIN_DEFAULT_TTL_DAYS,
    RetentionConfig,
    RetentionConfigError,
    _resolve_bool_field,
    _resolve_ttl_field,
)

__all__ = [
    "load_global_config",
    "load_project_config",
    # Internal helpers exposed for testing (prefixed with _)
    "_interpolate_vars",
    "_interpolate_dict",
    "_parse_toml",
    # _build_config_from_docs is intentionally included for testing the merge logic
    # in isolation without exercising filesystem I/O (TOML parsing + .env loading).
    "_build_config_from_docs",
]

# ---------------------------------------------------------------------------
# Regex: matches ${UPPER_CASE_VAR_NAME} — uppercase only (intentional limit)
# ---------------------------------------------------------------------------

_VAR_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _interpolate_vars(value: str, env: dict[str, str]) -> str:
    """Expand ${VAR_NAME} references in a single string using env.

    Only UPPERCASE variable names are matched ([A-Z_][A-Z0-9_]*). Lowercase
    patterns like ${my_var} are returned as-is without raising — they are
    not our interpolation syntax. This is an intentional design limit to
    reduce false-positive expansions of shell template syntax that uses
    lowercase names.

    Args:
        value: A TOML string leaf value, possibly containing ${VAR} patterns.
        env: The environment dict to look up var values in (usually os.environ).

    Returns:
        The string with all matched ${VAR} patterns replaced by their values.

    Raises:
        SecondSightConfigError: A matched var is absent from env, OR its value
            in env is an empty string (empty = misconfiguration when a ${VAR}
            reference is used; the user clearly intends the var to hold a value).
    """

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        val = env.get(var_name)
        if val is None or val == "":
            raise SecondSightConfigError(
                f"config value '${{{var_name}}}' references missing or empty env var {var_name!r}"
            )
        return val

    return _VAR_PATTERN.sub(_replace, value)


def _interpolate_dict(
    doc: dict[str, Any],
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Recursively expand ${VAR} patterns in all string leaf values of doc.

    Non-string values (int, bool, list of non-strings) are preserved as-is.
    Lists are scanned element-by-element; nested dicts are recursed.

    Args:
        doc: A dict tree as produced by tomllib.load().
        env: Environment dict to look up var values. Defaults to os.environ if None.
            Pass an explicit dict in tests to avoid monkeypatching os.environ.

    Returns:
        A new dict with the same structure; all string leaves have ${VAR}
        references expanded.

    Raises:
        SecondSightConfigError: Any ${VAR} reference cannot be resolved
            (see _interpolate_vars for exact conditions).
    """
    import os

    resolved_env: dict[str, str] = os.environ if env is None else env
    result: dict[str, Any] = {}
    for key, val in doc.items():
        if isinstance(val, str):
            result[key] = _interpolate_vars(val, resolved_env)
        elif isinstance(val, dict):
            result[key] = _interpolate_dict(val, resolved_env)
        elif isinstance(val, list):
            result[key] = _interpolate_list(val, resolved_env)
        else:
            result[key] = val
    return result


def _interpolate_list(
    lst: list[Any],
    env: dict[str, str] | None = None,
) -> list[Any]:
    """Expand ${VAR} in string elements of a list. Recurses for nested structures.

    Args:
        lst: A list from a TOML-decoded value.
        env: Environment dict. Defaults to os.environ if None.
    """
    import os

    resolved_env: dict[str, str] = os.environ if env is None else env
    out: list[Any] = []
    for item in lst:
        if isinstance(item, str):
            out.append(_interpolate_vars(item, resolved_env))
        elif isinstance(item, dict):
            out.append(_interpolate_dict(item, resolved_env))
        elif isinstance(item, list):
            out.append(_interpolate_list(item, resolved_env))
        else:
            out.append(item)
    return out


def _load_dotenv_if_exists(dotenv_path: Path) -> None:
    """Load a .env file into os.environ, if the file exists.

    Uses override=False, meaning existing os.environ values take priority
    over values in the .env file. This is the safe default: an operator
    who sets a var in their shell should always win.

    Side effect: modifies os.environ for the entire process.

    Args:
        dotenv_path: Full path to the .env file. If absent, does nothing.
    """
    if dotenv_path.is_file():
        load_dotenv(dotenv_path=dotenv_path, override=False)


def _parse_toml(path: Path, env: dict[str, str] | None = None) -> dict[str, Any] | None:
    """Read and parse a TOML file, then interpolate ${VAR} patterns.

    Args:
        path: Path to the TOML file.
        env: Environment dict for ${VAR} interpolation. Defaults to os.environ if None.
            Pass an explicit dict in tests to avoid monkeypatching os.environ. This
            parameter is threaded through to _interpolate_dict() and all recursive
            helpers — os.environ is never accessed directly when env is supplied.

    Returns:
        Parsed and interpolated dict, or None if the file does not exist.
        None is the fresh-install path — not an error.

    Raises:
        SecondSightConfigError: The file exists but cannot be parsed (malformed
            TOML), or ${VAR} interpolation fails (missing or empty env var).
    """
    if not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            doc = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise SecondSightConfigError(f"malformed TOML in config ({path}): {exc}") from exc
    return _interpolate_dict(doc, env=env)


def _build_global_analysis_config(doc: dict[str, Any]) -> GlobalAnalysisConfig:
    """Build GlobalAnalysisConfig from the parsed global TOML doc.

    Reads [analysis] and [analysis.models] and [analysis.models.fallback] sections.
    Applies SECONDSIGHT_DEFAULT_AGENT env var overlay (highest priority for this field).

    Empty string = not set: if [analysis].default_agent is "", the loader falls
    through to the built-in default (BUILTIN_DEFAULT_AGENT).

    Args:
        doc: Parsed global config.toml dict (already interpolated). May be empty {}.

    Returns:
        GlobalAnalysisConfig with all fields resolved.
    """
    analysis_section = doc.get("analysis")
    if not isinstance(analysis_section, dict):
        analysis_section = {}

    # --- default_agent: env var > TOML > builtin ---
    default_agent: str = BUILTIN_DEFAULT_AGENT

    toml_agent = analysis_section.get("default_agent", "")
    if toml_agent:  # empty = not set
        default_agent = toml_agent

    # env var overlay (highest priority for default_agent)
    env_agent = get_env_default_agent()
    if env_agent:
        default_agent = env_agent

    # --- [analysis.models] ---
    models_section = analysis_section.get("models")
    if not isinstance(models_section, dict):
        models_section = {}

    claude_code = models_section.get("claude_code", "") or ""
    codex = models_section.get("codex", "") or ""
    opencode = models_section.get("opencode", "") or ""

    # --- [analysis.models.fallback] ---
    fallback_section = models_section.get("fallback")
    if not isinstance(fallback_section, dict):
        fallback_section = {}

    fallback_models_raw = fallback_section.get("fallback_models")
    if isinstance(fallback_models_raw, list):
        fallback_config = FallbackModelsConfig(fallback_models=list(fallback_models_raw))
    else:
        fallback_config = FallbackModelsConfig()

    models_config = ModelsConfig(
        claude_code=claude_code,
        codex=codex,
        opencode=opencode,
        fallback=fallback_config,
    )

    return GlobalAnalysisConfig(
        default_agent=default_agent,
        models=models_config,
    )


def _build_retention_config(
    global_doc: dict[str, Any],
    project_doc: dict[str, Any],
    *,
    global_path: Path,
    project_path: Path,
) -> RetentionConfig:
    """Build RetentionConfig by merging global and per-project [retention] sections.

    Delegates resolution to _resolve_ttl_field and _resolve_bool_field from
    storage/retention.py — those helpers own the per-project → global → builtin
    fallthrough logic and type validation.

    Args:
        global_doc: Parsed global config.toml dict. May be empty {}.
        project_doc: Parsed per-project config.toml dict. May be empty {}.
        global_path: Path to global config.toml (for error messages in retention helpers).
        project_path: Path to per-project config.toml (for error messages).

    Returns:
        A frozen RetentionConfig with TTLs and source attributions resolved.

    Raises:
        SecondSightConfigError: A retention field in any config layer has an invalid type or
            value (e.g. TTL is a string, a boolean, or non-positive integer). RetentionConfigError
            from the underlying helpers is caught here and re-raised as SecondSightConfigError
            so callers only need to handle one exception type at the public API boundary.
    """
    try:
        global_retention = global_doc.get("retention")
        if not isinstance(global_retention, dict):
            global_retention = None

        project_retention = project_doc.get("retention")
        if not isinstance(project_retention, dict):
            project_retention = None

        raw_ttl, raw_source = _resolve_ttl_field(
            field_name="raw_traces_ttl_days",
            per_project_section=project_retention,
            global_section=global_retention,
            project_path=project_path,
            global_path=global_path,
            builtin_default=BUILTIN_DEFAULT_TTL_DAYS,
        )

        analysis_ttl, analysis_source = _resolve_ttl_field(
            field_name="analysis_ttl_days",
            per_project_section=project_retention,
            global_section=global_retention,
            project_path=project_path,
            global_path=global_path,
            builtin_default=BUILTIN_DEFAULT_ANALYSIS_TTL_DAYS,
        )

        cleanup_after = _resolve_bool_field(
            field_name="cleanup_after_analysis",
            per_project_section=project_retention,
            global_section=global_retention,
            project_path=project_path,
            global_path=global_path,
            builtin_default=False,
        )

        return RetentionConfig(
            raw_traces_ttl_days=raw_ttl,
            raw_traces_source=raw_source,
            analysis_ttl_days=analysis_ttl,
            analysis_ttl_source=analysis_source,
            cleanup_after_analysis=cleanup_after,
        )
    except RetentionConfigError as exc:
        raise SecondSightConfigError(f"retention config error: {exc}") from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def _build_config_from_docs(
    *,
    global_doc: dict[str, Any],
    project_doc: dict[str, Any],
    global_path: Path,
    project_path: Path,
) -> SecondSightConfig:
    """Build a SecondSightConfig from already-parsed TOML dicts.

    Internal helper used by both load_global_config() and load_project_config()
    to avoid parsing the global TOML twice. The .env loading and TOML parsing
    are the callers' responsibility; this function only builds the config objects.

    Args:
        global_doc: Parsed global config.toml dict (may be empty {}).
        project_doc: Parsed per-project config.toml dict (may be empty {}).
        global_path: Path to global config.toml (for retention error messages).
        project_path: Path to per-project config.toml (for retention error messages).

    Returns:
        A fully resolved SecondSightConfig.
    """
    # Retention: per-project → global → builtin (independent per-field)
    retention = _build_retention_config(
        global_doc=global_doc,
        project_doc=project_doc,
        global_path=global_path,
        project_path=project_path,
    )

    # Global analysis config (default_agent, models, fallback)
    analysis = _build_global_analysis_config(global_doc)

    # Per-project model override
    project_analysis_section = project_doc.get("analysis")
    if not isinstance(project_analysis_section, dict):
        project_analysis_section = {}

    project_model = project_analysis_section.get("model", "") or ""
    # empty string = not set — preserved as "" to signal "fall through" to caller

    # Env var overlay: SECONDSIGHT_ANALYSIS_MODEL wins over all TOML layers
    env_model = get_env_analysis_model()
    if env_model:
        project_model = env_model

    return SecondSightConfig(
        retention=retention,
        analysis=analysis,
        project_analysis=ProjectAnalysisConfig(model=project_model),
    )


def load_global_config(home: Path) -> SecondSightConfig:
    """Load the global SecondSight config from home directory.

    Steps:
    1. Load ~/.secondsight/.env into os.environ (override=False).
    2. Parse ~/.secondsight/config.toml (None if absent — that's OK).
    3. Build GlobalAnalysisConfig with env var overlay.
    4. Build RetentionConfig (project_doc = {} at global scope).
    5. Return SecondSightConfig with empty ProjectAnalysisConfig (no project override).

    Side effect: may modify os.environ if .env file is present. The modification is
    permanent for the process lifetime (os.environ is process-wide shared state).

    Args:
        home: The SecondSight home directory (e.g. Path.home() / ".secondsight").
            Need not exist; absent directory → built-in defaults with no error.

    Returns:
        SecondSightConfig with all sections resolved.

    Raises:
        SecondSightConfigError: config.toml exists but is malformed, or a ${VAR}
            reference in the TOML points to a missing or empty env var, or a
            retention field has an invalid value (RetentionConfigError is caught
            and re-raised as SecondSightConfigError at the _build_retention_config
            boundary).
    """
    home = Path(home)
    _load_dotenv_if_exists(home / ".env")

    global_path = home / "config.toml"
    global_doc = _parse_toml(global_path) or {}

    # Sentinel project_path for retention helper error messages (no per-project at global level)
    _sentinel_project_path = home / "projects" / "<global-scope>" / "config.toml"

    return _build_config_from_docs(
        global_doc=global_doc,
        project_doc={},  # no per-project override at global scope
        global_path=global_path,
        project_path=_sentinel_project_path,
    )


def load_project_config(home: Path, project_id: str) -> SecondSightConfig:
    """Load the merged config for a specific project.

    Steps:
    1. Load ~/.secondsight/.env into os.environ (override=False, via load_global_config).
    2. Parse global config.toml (once).
    3. Parse per-project TOML at home/projects/<project_id>/config.toml.
    4. Merge all layers via _build_config_from_docs().
    5. Apply SECONDSIGHT_ANALYSIS_MODEL env var overlay (highest priority, inside helper).

    The per-project TOML file being absent is not an error (fresh project path).

    Args:
        home: The SecondSight home directory.
        project_id: Project identifier. Used to locate the per-project config.

    Returns:
        SecondSightConfig with all layers merged.

    Note:
        ``project_analysis.model`` may be ``""`` if no model override is configured at
        any layer. An empty string means "use the adapter's built-in default" and is
        intentionally preserved for downstream consumers (e.g. ``select_model()``).
        Callers must not treat ``""`` as a configured model name.

    Raises:
        SecondSightConfigError: A TOML file is present but malformed, or a ${VAR}
            reference cannot be resolved, or a retention field has an invalid value.
    """
    home = Path(home)

    # Load .env first (side effect on os.environ, override=False)
    _load_dotenv_if_exists(home / ".env")

    # Parse global TOML once
    global_path = home / "config.toml"
    global_doc = _parse_toml(global_path) or {}

    # Parse per-project TOML
    project_path = home / "projects" / project_id / "config.toml"
    project_doc = _parse_toml(project_path) or {}

    return _build_config_from_docs(
        global_doc=global_doc,
        project_doc=project_doc,
        global_path=global_path,
        project_path=project_path,
    )
