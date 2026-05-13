"""AnalysisConfig — TOML-backed configuration for the analysis layer (GUR-103 task-1).

Modeled on `storage/retention_config.py` (GUR-147 pattern).

Reads from a single config.toml file path (the per-project config).
The relevant section is `[analysis.read_project_file]`.

Precedence:
    Config file present → values are read and validated.
    Config file absent  → all built-in defaults (never raise on missing file).
    Config file present but malformed → raise AnalysisConfigError.

DC-C1: Malformed TOML raises AnalysisConfigError.
DC-C2: Wrong-type values raise AnalysisConfigError.
DC-C3: Non-positive size_cap_kb raises AnalysisConfigError.

Design assumption:
    AnalysisConfig.load() is called at AnalysisTools construction time, not
    at every tool invocation, so the config is read once per tools instance.
    If config changes on disk during a process lifetime, the process must
    be restarted to pick up the change.

If this assumption stops holding (hot-reload scenario), the first thing
to rot is config changes taking effect mid-session without restart.

config-unification note (task-1):
    The model-selection config classes (ModelsConfig, GlobalAnalysisConfig,
    ProjectAnalysisConfig, FallbackModelsConfig) are now defined in
    secondsight.config.schema and re-exported here for backward compatibility.
    AnalysisConfig and AnalysisConfigError remain defined in this module
    because AnalysisConfig.load() is a TOML-reading classmethod that belongs
    in the analysis layer, not in the schema-only config package.
    Task-2 will integrate load() into the unified loader.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# Re-export model-selection schema classes from config/schema.py.
# These are now defined there as the single source of truth.
# _verify_adapter_registry_consistency() (sdk/model_selection.py) imports
# ModelsConfig from this module — the re-export ensures it gets the same
# class object as config/schema.py, preventing isinstance() split.
from secondsight.config.schema import (
    BUILTIN_DEFAULT_AGENT,
    BUILTIN_FALLBACK_MODELS,
    FallbackModelsConfig,
    GlobalAnalysisConfig,
    ModelsConfig,
    ProjectAnalysisConfig,
)

# ---- Built-in defaults ----

BUILTIN_SIZE_CAP_KB: int = 256
BUILTIN_READ_PROJECT_FILE_ENABLED: bool = True


class AnalysisConfigError(Exception):
    """Raised when a config file is present but unreadable or has invalid values.
    NOT raised when the file is absent — absent → built-in defaults.
    """


@dataclass(frozen=True)
class AnalysisConfig:
    """Resolved analysis configuration.

    Attributes:
        read_project_file_enabled: Whether the read_project_file tool is enabled.
            Default: True. Set to False via [analysis.read_project_file] enabled = false.
        size_cap_kb: Maximum file read size in KiB.
            Default: 256 (256 KiB). Files larger than this are truncated with a marker.
        extra_denylist: Additional denylist patterns from project config.
            These are ADDITIVE on top of the built-in denylist.
            Default: [] (empty — no additions).

    Note: Task-2 will integrate this TOML loader into the unified
    load_project_config() function in secondsight.config.loader.
    """

    read_project_file_enabled: bool = BUILTIN_READ_PROJECT_FILE_ENABLED
    size_cap_kb: int = BUILTIN_SIZE_CAP_KB
    extra_denylist: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, *, config_path: Path) -> "AnalysisConfig":
        """Load AnalysisConfig from a TOML file.

        Args:
            config_path: Path to the TOML config file.
                If absent, returns built-in defaults silently.

        Returns:
            AnalysisConfig with resolved values.

        Raises:
            AnalysisConfigError: File IS present but cannot be parsed,
                or contains values of the wrong type or invalid range,
                or a ``${VAR}`` reference in the file is unresolvable.

        Note:
            ``${VAR}`` interpolation is applied to all string leaves before
            type checks. Without this, ``[analysis.read_project_file].denylist``
            entries containing ``${HOME}`` would silently pass through as
            literal strings — diverging from the rest of the unified config
            which interpolates uniformly. Routes through the unified loader's
            ``_parse_toml()`` to keep one single parse-and-interpolate path.
        """
        # Defer import to break a potential circular dependency:
        # loader.py imports from analysis/config.py at module level for re-exports,
        # so importing _parse_toml at module level here would create a cycle.
        from secondsight.config.loader import _parse_toml
        from secondsight.config.schema import SecondSightConfigError

        config_path = Path(config_path)
        if not config_path.is_file():
            return cls()

        try:
            doc = _parse_toml(config_path)
        except SecondSightConfigError as exc:
            # Translate the unified loader's error into AnalysisConfig's contract.
            # The contract (AnalysisConfigError on any parse failure) is preserved;
            # only the underlying mechanism changed. Callers that already catch
            # AnalysisConfigError continue to work without modification.
            raise AnalysisConfigError(str(exc)) from exc

        if doc is None:
            return cls()

        section = _get_read_project_file_section(doc)
        if section is None:
            return cls()

        enabled = _read_bool(section, "enabled", config_path)
        size_cap_kb = _read_positive_int(section, "size_cap_kb", config_path)
        extra_denylist = _read_string_list(section, "denylist", config_path)

        return cls(
            read_project_file_enabled=enabled,
            size_cap_kb=size_cap_kb,
            extra_denylist=extra_denylist,
        )


def _get_read_project_file_section(doc: dict) -> dict | None:
    """Extract [analysis.read_project_file] section or None if absent."""
    analysis = doc.get("analysis")
    if not isinstance(analysis, dict):
        return None
    rpf = analysis.get("read_project_file")
    if not isinstance(rpf, dict):
        return None
    return rpf


def _read_bool(section: dict, key: str, config_path: Path) -> bool:
    """Read a boolean value from a TOML section; return default if key absent."""
    if key not in section:
        return BUILTIN_READ_PROJECT_FILE_ENABLED
    value = section[key]
    if not isinstance(value, bool):
        raise AnalysisConfigError(
            f"[analysis.read_project_file].{key} in {config_path} must be a boolean, "
            f"got {type(value).__name__}: {value!r}"
        )
    return value


def _read_positive_int(section: dict, key: str, config_path: Path) -> int:
    """Read a positive int from a TOML section; return default if key absent.

    Booleans are rejected even though they are technically int in Python.
    """
    if key not in section:
        return BUILTIN_SIZE_CAP_KB
    value = section[key]
    # Reject booleans (technically int in Python, but operator typo if used here)
    if isinstance(value, bool) or not isinstance(value, int):
        raise AnalysisConfigError(
            f"[analysis.read_project_file].{key} in {config_path} must be a positive integer, "
            f"got {type(value).__name__}: {value!r}"
        )
    if value <= 0:
        raise AnalysisConfigError(
            f"[analysis.read_project_file].{key} in {config_path} must be a positive integer, "
            f"got {value}"
        )
    return value


def _read_string_list(section: dict, key: str, config_path: Path) -> list[str]:
    """Read a list of strings from a TOML section; return empty list if absent."""
    if key not in section:
        return []
    value = section[key]
    if not isinstance(value, list):
        raise AnalysisConfigError(
            f"[analysis.read_project_file].{key} in {config_path} must be a list of strings, "
            f"got {type(value).__name__}: {value!r}"
        )
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise AnalysisConfigError(
                f"[analysis.read_project_file].{key}[{i}] in {config_path} must be a string, "
                f"got {type(item).__name__}: {item!r}"
            )
    return list(value)


__all__ = [
    "AnalysisConfig",
    "AnalysisConfigError",
    "BUILTIN_READ_PROJECT_FILE_ENABLED",
    "BUILTIN_SIZE_CAP_KB",
    # Model-selection config (GUR-103 task-3 additions)
    "FallbackModelsConfig",
    "ModelsConfig",
    "GlobalAnalysisConfig",
    "ProjectAnalysisConfig",
    "BUILTIN_DEFAULT_AGENT",
    "BUILTIN_FALLBACK_MODELS",
]


# ---------------------------------------------------------------------------
# GUR-103 task-3 model-selection config schema — re-exported from config/schema.py
# ---------------------------------------------------------------------------
# These dataclasses are now defined in secondsight.config.schema (config-unification
# task-1) and re-exported here for backward compatibility. External callers that
# import from secondsight.analysis.config continue to work without changes.
#
# Do NOT redefine FallbackModelsConfig, ModelsConfig, GlobalAnalysisConfig, or
# ProjectAnalysisConfig in this file — the imports at the top of this module
# bring them in from config/schema.py. Redefining them here would shadow those
# imports and create a class identity split (two different ModelsConfig objects),
# which breaks _verify_adapter_registry_consistency() in sdk/model_selection.py.
# ---------------------------------------------------------------------------
