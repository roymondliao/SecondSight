"""`secondsight config` — show effective config with source attribution + validate format.

Two subcommands implementing DC-1 and DC-5 from the config-unification plan:
    DC-1: "Env var overrides TOML but operator doesn't know which layer."
    DC-5: "TOML model typo not caught at load time."

Subcommands:
    secondsight config show [--project PROJECT_ID] [--secondsight-home DIR]
        Print every config field with the source layer that provided it.
        Sources: [env_var] | [env_var_interpolation] | [per_project_config] |
                 [global_config] | [builtin_default]

    secondsight config validate [--project PROJECT_ID] [--secondsight-home DIR]
        Load and type-check all config files, run model format validation.
        Exit 0 if clean or warnings only; exit 1 if any errors.

Implementation assumptions (explicit per Samsara rules):
    - Assumption A1: The only env var that overrides a model field is SECONDSIGHT_ANALYSIS_MODEL.
      If future env vars are added without updating _collect_sourced_values(), they will be
      attributed to the wrong source layer.
    - Assumption A2: Shell env vars and .env file contents are both shown as [env_var].
      The distinction between "shell-set" vs ".env file-set" is not tracked (Scar-2).
    - Assumption A3: ${VAR} interpolation source detection is done by scanning the raw TOML
      bytes for the pattern BEFORE interpolation. If TOML is written without ${} syntax
      but the value happens to equal an env var value, it will NOT be marked interpolation.

Silent failure conditions (declared per Samsara):
    1. If a new env var is added to env.py but not to _collect_sourced_values(), it will
       silently misattribute that field's source.
    2. _parse_toml_both() must be called after _load_dotenv_if_exists(). If a future caller
       skips the .env loading step, ${VAR} references to .env-only vars will raise
       SecondSightConfigError even though the runtime loader would resolve them successfully.
    3. The model validator does not know which model names are currently valid (no API call);
       it only validates format patterns. A syntactically valid but deprecated model name
       passes validate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import typer
from rich.console import Console

from secondsight.cli._home import secondsight_home as resolve_secondsight_home
from secondsight.config.env import (
    ENV_ANALYSIS_MODEL,
    ENV_DEFAULT_AGENT,
    get_env_analysis_model,
    get_env_default_agent,
)
from secondsight.config.loader import _VAR_PATTERN, _load_dotenv_if_exists, _parse_toml_both
from secondsight.config.schema import (
    BUILTIN_DEFAULT_AGENT,
    BUILTIN_FALLBACK_MODELS,
    SecondSightConfigError,
)
from secondsight.storage.retention import (
    BUILTIN_DEFAULT_ANALYSIS_TTL_DAYS,
    BUILTIN_DEFAULT_TTL_DAYS,
)

app = typer.Typer(
    name="config",
    help="Show effective configuration with source attribution, or validate config files.",
    invoke_without_command=False,
)

_console = Console()

# ---------------------------------------------------------------------------
# Source types
# ---------------------------------------------------------------------------

ConfigSourceLabel = Literal[
    "env_var",
    "env_var_interpolation",
    "per_project_config",
    "global_config",
    "builtin_default",
]

_SOURCE_DISPLAY: dict[ConfigSourceLabel, str] = {
    "env_var": "[env_var]",
    "env_var_interpolation": "[env_var_interpolation]",
    "per_project_config": "[per_project_config]",
    "global_config": "[global_config]",
    "builtin_default": "[builtin_default]",
}


@dataclass
class SourcedValue:
    """A config value with its source layer identified.

    Attributes:
        value: The effective resolved value (after interpolation and env var overlay).
        source: Which config layer provided this value.
    """

    value: Any
    source: ConfigSourceLabel


def _has_var_interpolation(raw_value: Any) -> bool:
    """Return True if the raw TOML value contains a ${VAR} pattern."""
    if isinstance(raw_value, str):
        return bool(_VAR_PATTERN.search(raw_value))
    if isinstance(raw_value, list):
        return any(_has_var_interpolation(item) for item in raw_value)
    return False


def _get_nested(doc: dict[str, Any], *keys: str) -> Any:
    """Safely retrieve a nested dict value, returning None if any key is absent."""
    current: Any = doc
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


# ---------------------------------------------------------------------------
# Source detection
# ---------------------------------------------------------------------------


def _determine_source(
    *,
    raw_global: dict[str, Any] | None,
    raw_project: dict[str, Any] | None,
    global_keys: tuple[str, ...],
    project_keys: tuple[str, ...] | None,
    env_var_name: str | None,
) -> ConfigSourceLabel:
    """Determine the source layer for a single config field.

    Priority (highest → lowest):
    1. env_var: env_var_name is set and non-empty
    2. env_var_interpolation: raw TOML string contains ${VAR_NAME}
    3. per_project_config: project_keys path exists in raw_project doc
    4. global_config: global_keys path exists in raw_global doc
    5. builtin_default: none of the above

    Args:
        raw_global: Un-interpolated section dict from the global config (or None if absent).
            Callers slice to the relevant section before passing (e.g. raw_global.get("retention")).
        raw_project: Un-interpolated section dict from the per-project config (or None if absent).
            Same slicing convention as raw_global.
        global_keys: Key path within the section dict passed as raw_global.
        project_keys: Key path within raw_project (or None if field doesn't exist at project scope).
        env_var_name: The env var name to check (or None if no env var for this field).

    Returns:
        ConfigSourceLabel
    """
    import os

    # Check 1: direct env var
    if env_var_name is not None:
        raw_env = os.environ.get(env_var_name, "").strip()
        if raw_env:
            return "env_var"

    # Check 2: env_var_interpolation in project doc
    if project_keys is not None and raw_project is not None:
        raw_proj_val = _get_nested(raw_project, *project_keys)
        if raw_proj_val is not None and raw_proj_val != "" and _has_var_interpolation(raw_proj_val):
            return "env_var_interpolation"

    # Check 3: env_var_interpolation in global doc
    if raw_global is not None:
        raw_global_val = _get_nested(raw_global, *global_keys)
        if (
            raw_global_val is not None
            and raw_global_val != ""
            and _has_var_interpolation(raw_global_val)
        ):
            return "env_var_interpolation"

    # Check 4: per_project_config (field is present and non-empty in project TOML)
    if project_keys is not None and raw_project is not None:
        raw_proj_val = _get_nested(raw_project, *project_keys)
        if raw_proj_val is not None and raw_proj_val != "":
            return "per_project_config"

    # Check 5: global_config (field is present and non-empty in global TOML)
    if raw_global is not None:
        raw_global_val = _get_nested(raw_global, *global_keys)
        if raw_global_val is not None and raw_global_val != "":
            return "global_config"

    return "builtin_default"


# ---------------------------------------------------------------------------
# Main source collection
# ---------------------------------------------------------------------------


def _collect_sourced_values(
    home: Path,
    project_id: str | None,
) -> dict[str, SourcedValue]:
    """Build a flat dict of all config keys mapped to SourcedValue.

    Calls _parse_toml_both() from loader.py to read each config file once,
    returning both the raw (un-interpolated) and interpolated dicts in a single
    pass. Raw docs are used for source attribution (${VAR} pattern detection);
    interpolated docs provide the effective resolved values.

    Keys returned (dot-separated section paths):
        retention.raw_traces_ttl_days
        retention.analysis_ttl_days
        retention.cleanup_after_analysis
        analysis.default_agent
        analysis.models.claude_code
        analysis.models.codex
        analysis.models.opencode
        analysis.models.fallback.fallback_models
        project_analysis.model  (only if project_id given)

    Raises:
        SecondSightConfigError: Any config file is malformed or ${VAR} is unresolvable.
    """
    global_path = home / "config.toml"
    project_path = (home / "projects" / project_id / "config.toml") if project_id else None

    # Load .env before reading TOML — same side effect as load_global_config() / load_project_config().
    # Without this, ${VAR} refs resolved only via ~/.secondsight/.env would raise
    # SecondSightConfigError here while the runtime loader would succeed.
    _load_dotenv_if_exists(home / ".env")

    # Read each config file once: _parse_toml_both() returns (raw, interpolated) in a
    # single pass. raw_* dicts keep ${VAR} patterns intact (for source attribution);
    # interp_* dicts have them expanded (for effective values).
    raw_global, interp_global_r = _parse_toml_both(global_path)
    interp_global = interp_global_r or {}

    if project_path:
        raw_project, interp_project_r = _parse_toml_both(project_path)
        interp_project = interp_project_r or {}
    else:
        raw_project, interp_project = None, {}

    result: dict[str, SourcedValue] = {}

    # --- retention.raw_traces_ttl_days ---
    global_ret = interp_global.get("retention") or {}
    project_ret = interp_project.get("retention") or {}

    # Fix: use `is None` instead of `or` to avoid treating 0 as falsy.
    # `project_ret.get("raw_traces_ttl_days") or ...` would silently ignore an explicit
    # project-level 0, falling through to the global/builtin value with the wrong label.
    _project_raw_ttl = project_ret.get("raw_traces_ttl_days")
    raw_ttl = (
        _project_raw_ttl
        if _project_raw_ttl is not None
        else global_ret.get("raw_traces_ttl_days", BUILTIN_DEFAULT_TTL_DAYS)
    )
    result["retention.raw_traces_ttl_days"] = SourcedValue(
        value=raw_ttl,
        source=_determine_source(
            raw_global=raw_global.get("retention") if raw_global else None,  # type: ignore[arg-type]
            raw_project=raw_project.get("retention") if raw_project else None,  # type: ignore[arg-type]
            global_keys=("raw_traces_ttl_days",),
            project_keys=("raw_traces_ttl_days",),
            env_var_name=None,
        ),
    )

    # --- retention.analysis_ttl_days ---
    # Same fix: use `is None` check to handle explicit 0 without treating it as falsy.
    _project_analysis_ttl = project_ret.get("analysis_ttl_days")
    analysis_ttl = (
        _project_analysis_ttl
        if _project_analysis_ttl is not None
        else global_ret.get("analysis_ttl_days", BUILTIN_DEFAULT_ANALYSIS_TTL_DAYS)
    )
    result["retention.analysis_ttl_days"] = SourcedValue(
        value=analysis_ttl,
        source=_determine_source(
            raw_global=raw_global.get("retention") if raw_global else None,  # type: ignore[arg-type]
            raw_project=raw_project.get("retention") if raw_project else None,  # type: ignore[arg-type]
            global_keys=("analysis_ttl_days",),
            project_keys=("analysis_ttl_days",),
            env_var_name=None,
        ),
    )

    # --- retention.cleanup_after_analysis ---
    # Boolean False is falsy but is a valid explicit setting, so we must use `is None`
    # to distinguish "not set" from "explicitly set to False".
    # After _parse_toml() interpolation, a field written as `cleanup_after_analysis = "${VAR}"`
    # will be a string ("true"/"false"), not a Python bool. Normalise to bool.
    # Variables named `_interp` to distinguish from the un-interpolated `raw_*` dicts.
    cleanup_project_interp = project_ret.get("cleanup_after_analysis")
    cleanup_global_interp = global_ret.get("cleanup_after_analysis")
    _cleanup_effective = (
        cleanup_project_interp
        if cleanup_project_interp is not None
        else (cleanup_global_interp if cleanup_global_interp is not None else False)
    )
    # Normalise string "true"/"false" (produced by ${VAR} interpolation of a boolean field)
    if isinstance(_cleanup_effective, str):
        cleanup_val: bool = _cleanup_effective.strip().lower() in ("1", "true", "yes")
    else:
        cleanup_val = bool(_cleanup_effective)

    raw_global_ret = raw_global.get("retention") if raw_global else None
    raw_project_ret = raw_project.get("retention") if raw_project else None
    cleanup_source: ConfigSourceLabel = _determine_source(
        raw_global=raw_global_ret,  # type: ignore[arg-type]
        raw_project=raw_project_ret,  # type: ignore[arg-type]
        global_keys=("cleanup_after_analysis",),
        project_keys=("cleanup_after_analysis",),
        env_var_name=None,
    )
    result["retention.cleanup_after_analysis"] = SourcedValue(
        value=cleanup_val, source=cleanup_source
    )

    # --- analysis.default_agent ---
    global_analysis = interp_global.get("analysis") or {}
    raw_global_analysis = (raw_global.get("analysis") if raw_global else None) or {}
    raw_project_analysis = (raw_project.get("analysis") if raw_project else None) or {}

    env_agent = get_env_default_agent()
    toml_agent = global_analysis.get("default_agent", "") or ""
    default_agent = env_agent or toml_agent or BUILTIN_DEFAULT_AGENT

    result["analysis.default_agent"] = SourcedValue(
        value=default_agent,
        source=_determine_source(
            raw_global=raw_global_analysis,
            raw_project=raw_project_analysis,
            global_keys=("default_agent",),
            project_keys=None,
            env_var_name=ENV_DEFAULT_AGENT,
        ),
    )

    # --- analysis.models.* ---
    global_models = global_analysis.get("models") or {}
    raw_global_models = (
        raw_global_analysis.get("models") or {} if isinstance(raw_global_analysis, dict) else {}
    )

    env_model = get_env_analysis_model()

    for field_name in ("claude_code", "codex", "opencode"):
        toml_model_val = global_models.get(field_name, "") or ""
        # env var overrides all model fields
        effective_model = env_model or toml_model_val

        result[f"analysis.models.{field_name}"] = SourcedValue(
            value=effective_model,
            source=_determine_source(
                raw_global=raw_global_models if isinstance(raw_global_models, dict) else {},
                raw_project=None,  # model fields not per-project in global [analysis.models]
                global_keys=(field_name,),
                project_keys=None,
                env_var_name=ENV_ANALYSIS_MODEL,
            ),
        )

    # --- analysis.models.fallback.fallback_models ---
    global_fallback = global_models.get("fallback") or {}
    raw_global_fallback = (
        raw_global_models.get("fallback") or {} if isinstance(raw_global_models, dict) else {}
    )

    fallback_models_raw = global_fallback.get("fallback_models")
    if isinstance(fallback_models_raw, list):
        fallback_val = fallback_models_raw
    else:
        fallback_val = list(BUILTIN_FALLBACK_MODELS)

    # Route source detection through _determine_source() — same as every other field —
    # so any future env var added for fallback_models will be attributed correctly.
    # Sanitize first: _determine_source() treats any non-None/non-empty value as
    # configured, but we need to honour the list-type contract (anything not a list
    # falls through to builtin_default). The sanitized raw_global below presents the
    # field as "absent" when it has the wrong type, so the source cascades cleanly.
    raw_global_for_fallback: dict[str, Any] | None = None
    if (
        isinstance(raw_global_models, dict)
        and isinstance(raw_global_fallback, dict)
        and isinstance(raw_global_fallback.get("fallback_models"), list)
    ):
        raw_global_for_fallback = {
            "fallback": {"fallback_models": raw_global_fallback.get("fallback_models")}
        }

    fallback_source = _determine_source(
        raw_global=raw_global_for_fallback,
        raw_project=None,  # no per-project layer for fallback_models
        global_keys=("fallback", "fallback_models"),
        project_keys=None,
        env_var_name=None,  # no env var override for fallback_models today
    )
    result["analysis.models.fallback.fallback_models"] = SourcedValue(
        value=fallback_val, source=fallback_source
    )

    # --- project_analysis.model (only if project_id given) ---
    if project_id:
        project_analysis = interp_project.get("analysis") or {}
        proj_model_toml = project_analysis.get("model", "") or ""
        effective_proj_model = env_model or proj_model_toml

        result["project_analysis.model"] = SourcedValue(
            value=effective_proj_model,
            source=_determine_source(
                raw_global=None,
                raw_project=raw_project_analysis if isinstance(raw_project_analysis, dict) else {},
                global_keys=("model",),  # not used (raw_global=None)
                project_keys=("model",),
                env_var_name=ENV_ANALYSIS_MODEL,
            ),
        )

    return result


# ---------------------------------------------------------------------------
# TTL field registry — shared between show() and validate()
# ---------------------------------------------------------------------------

# All config keys that must hold integer values (days). Both show() and validate()
# reference this constant so adding a new TTL field requires updating only one site.
_TTL_INT_KEYS: tuple[str, ...] = (
    "retention.raw_traces_ttl_days",
    "retention.analysis_ttl_days",
)


def _is_invalid_ttl(sv: SourcedValue) -> bool:
    """Return True if the SourcedValue holds a non-integer TTL.

    bool is a subclass of int in Python, so isinstance(True, int) is True.
    The bool pre-check is mandatory to reject TOML `= true` explicitly.
    """
    return isinstance(sv.value, bool) or not isinstance(sv.value, int)


# ---------------------------------------------------------------------------
# Model name format validation
# ---------------------------------------------------------------------------

# Pattern for valid claude model: claude-<name>-<YYYYMMDD>
# e.g. claude-haiku-4-5-20251001, claude-opus-4-7-20261231
_CLAUDE_WITH_DATE_RE = re.compile(r"^claude-.+-\d{8}$")
_CLAUDE_PREFIX_RE = re.compile(r"^claude-")
_GPT_PREFIX_RE = re.compile(r"^gpt-")
_GPT_O_PREFIX_RE = re.compile(r"^o[0-9]")
_GEMINI_PREFIX_RE = re.compile(r"^gemini-")


@dataclass
class ModelValidationResult:
    """Result of validating a single model name.

    Attributes:
        field: Config key (e.g. "analysis.models.claude_code")
        model: The model name that was checked.
        is_error: True if this is a hard error (exits 1).
        is_warning: True if this is a soft warning (exits 0).
        message: Human-readable description of the issue.
    """

    field: str
    model: str
    is_error: bool
    is_warning: bool
    message: str


def _validate_model_name(field: str, model: str) -> ModelValidationResult | None:
    """Validate the format of a model name string.

    Rules:
    - Empty string → no issue (means "not set").
    - claude-* without date suffix → error (malformed, DC-5).
    - claude-* with YYYYMMDD suffix → valid.
    - gpt-4* or gpt-o* or o[0-9]* → valid format (no date required).
    - gemini-* → valid format.
    - Unknown prefix → warn (not error).

    Returns:
        ModelValidationResult if there's an issue, None if clean.
    """
    if not model:
        return None  # empty = not set, valid

    if _CLAUDE_PREFIX_RE.match(model):
        if not _CLAUDE_WITH_DATE_RE.match(model):
            return ModelValidationResult(
                field=field,
                model=model,
                is_error=True,
                is_warning=False,
                message=(
                    f"claude model name must end in -YYYYMMDD date suffix "
                    f"(got {model!r}). "
                    f"Example: claude-haiku-4-5-20251001"
                ),
            )
        return None  # valid claude model

    if _GPT_PREFIX_RE.match(model) or _GPT_O_PREFIX_RE.match(model):
        return None  # valid gpt format

    if _GEMINI_PREFIX_RE.match(model):
        return None  # valid gemini format

    # Unknown prefix → warn
    return ModelValidationResult(
        field=field,
        model=model,
        is_error=False,
        is_warning=True,
        message=f"unrecognised model prefix in {field!r}: {model!r} — validate manually",
    )


def _collect_model_issues(
    sourced: dict[str, SourcedValue],
) -> list[ModelValidationResult]:
    """Run model format validation on all model fields in sourced config."""
    model_fields = [
        "analysis.models.claude_code",
        "analysis.models.codex",
        "analysis.models.opencode",
        "project_analysis.model",
    ]
    issues: list[ModelValidationResult] = []
    for field_key in model_fields:
        sv = sourced.get(field_key)
        if sv is None:
            continue
        model_val = sv.value
        if isinstance(model_val, str):
            result = _validate_model_name(field_key, model_val)
            if result is not None:
                issues.append(result)
    return issues


# ---------------------------------------------------------------------------
# `config show` subcommand
# ---------------------------------------------------------------------------


@app.command(name="show")
def show(
    project: str = typer.Option(
        "",
        "--project",
        help="Project ID to include per-project config layer.",
    ),
    secondsight_home_override: str = typer.Option(
        "",
        "--secondsight-home",
        help="Override the SecondSight home directory (~/.secondsight by default).",
    ),
) -> None:
    """Print effective config with source attribution per field.

    Sources shown: [env_var] | [env_var_interpolation] | [per_project_config] |
    [global_config] | [builtin_default]
    """
    home = resolve_secondsight_home(secondsight_home_override)
    project_id = project.strip() or None

    if project_id:
        from secondsight.api._id_safety import is_safe_id

        if not is_safe_id(project_id):
            raise typer.BadParameter(
                f"project {project_id!r} contains unsafe path characters.",
                param_hint="--project",
            )

    try:
        sourced = _collect_sourced_values(home, project_id)
    except SecondSightConfigError as exc:
        typer.echo(f"config error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    # --- Render ---
    _render_section(
        "[retention]",
        sourced,
        [
            "retention.raw_traces_ttl_days",
            "retention.analysis_ttl_days",
            "retention.cleanup_after_analysis",
        ],
    )

    _render_section(
        "[analysis]",
        sourced,
        [
            "analysis.default_agent",
        ],
    )

    _render_section(
        "[analysis.models]",
        sourced,
        [
            "analysis.models.claude_code",
            "analysis.models.codex",
            "analysis.models.opencode",
        ],
    )

    _render_section(
        "[analysis.models.fallback]",
        sourced,
        [
            "analysis.models.fallback.fallback_models",
        ],
    )

    if project_id:
        _render_section(
            f"[project:{project_id}]",
            sourced,
            [
                "project_analysis.model",
            ],
        )

    # Warn about TTL type issues. validate exits 1 for these; show warns only so the
    # operator knows to run validate — without this, show silently displays `True` with
    # no indication that the runtime loader would reject the same config.
    for ttl_key in _TTL_INT_KEYS:
        sv = sourced.get(ttl_key)
        if sv is not None and _is_invalid_ttl(sv):
            typer.echo(
                f"WARNING: {ttl_key}: expected integer (days), "
                f"got {type(sv.value).__name__!r} ({sv.value!r}). "
                f"Run 'secondsight config validate' for details."
            )

    now_iso = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")
    typer.echo(f"Config last loaded at: {now_iso}")


def _render_section(
    header: str,
    sourced: dict[str, SourcedValue],
    keys: list[str],
) -> None:
    """Print a section header followed by key = value  [source] lines."""
    typer.echo(header)
    for full_key in keys:
        sv = sourced.get(full_key)
        if sv is None:
            continue
        # Extract short name (after last dot)
        short_name = full_key.rsplit(".", 1)[-1]
        label = _SOURCE_DISPLAY.get(sv.source, f"[{sv.source}]")
        val_str = str(sv.value)
        typer.echo(f"  {short_name} = {val_str}  {label}")
    typer.echo("")


# ---------------------------------------------------------------------------
# `config validate` subcommand
# ---------------------------------------------------------------------------


@app.command(name="validate")
def validate(
    project: str = typer.Option(
        "",
        "--project",
        help="Project ID to also validate the per-project config layer.",
    ),
    secondsight_home_override: str = typer.Option(
        "",
        "--secondsight-home",
        help="Override the SecondSight home directory (~/.secondsight by default).",
    ),
) -> None:
    """Validate config file format and model name patterns.

    Exits 0 if all files are valid (warnings are non-fatal).
    Exits 1 if any error is found (malformed TOML, unresolvable ${VAR}, invalid model format).
    """
    home = resolve_secondsight_home(secondsight_home_override)
    project_id = project.strip() or None

    if project_id:
        from secondsight.api._id_safety import is_safe_id

        if not is_safe_id(project_id):
            raise typer.BadParameter(
                f"project {project_id!r} contains unsafe path characters.",
                param_hint="--project",
            )

    errors: list[str] = []
    warnings: list[str] = []
    files_checked = 0

    global_path = home / "config.toml"
    project_path = (home / "projects" / project_id / "config.toml") if project_id else None

    if global_path.is_file():
        files_checked += 1

    if project_path and project_path.is_file():
        files_checked += 1

    # --- Try loading full config (catches TOML malform + ${VAR} errors) ---
    try:
        sourced = _collect_sourced_values(home, project_id)
    except SecondSightConfigError as exc:
        err_msg = str(exc)
        errors.append(err_msg)
        # Print immediately so missing var name is visible
        typer.echo(f"error: {err_msg}", err=False)
        # Can't run model validation without sourced values
        _print_summary(files_checked, errors, warnings)
        raise typer.Exit(code=1)

    # --- Retention TTL type validation ---
    # _collect_sourced_values() uses simple dict.get() for retention, not _resolve_ttl_field().
    # We add a lightweight type check here to catch the most common misconfiguration
    # (string or boolean where int is expected) that would otherwise pass validate silently
    # but fail at runtime.
    for ttl_key in _TTL_INT_KEYS:
        sv = sourced.get(ttl_key)
        if sv is not None and _is_invalid_ttl(sv):
            err_msg = (
                f"{ttl_key}: expected integer (days), got {type(sv.value).__name__!r} "
                f"({sv.value!r})"
            )
            errors.append(err_msg)
            typer.echo(f"error: {err_msg}")

    # --- Model format validation ---
    model_issues = _collect_model_issues(sourced)
    for issue in model_issues:
        if issue.is_error:
            errors.append(issue.message)
            typer.echo(f"error: {issue.message}")
        elif issue.is_warning:
            warnings.append(issue.message)
            typer.echo(f"warning: {issue.message}")

    _print_summary(files_checked, errors, warnings)

    if errors:
        raise typer.Exit(code=1)


def _print_summary(files_checked: int, errors: list[str], warnings: list[str]) -> None:
    """Print the final summary line."""
    n_errors = len(errors)
    n_warnings = len(warnings)
    parts = [f"{files_checked} config file(s) validated", f"{n_errors} error(s)"]
    if n_warnings:
        parts.append(f"{n_warnings} warning(s)")
    typer.echo(", ".join(parts))


__all__ = ["app", "SourcedValue", "_TTL_INT_KEYS", "_is_invalid_ttl"]
