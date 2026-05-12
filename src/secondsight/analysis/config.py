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
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

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
                or contains values of the wrong type or invalid range.
        """
        config_path = Path(config_path)
        if not config_path.is_file():
            return cls()

        try:
            with config_path.open("rb") as fh:
                doc = tomllib.load(fh)
        except tomllib.TOMLDecodeError as exc:
            raise AnalysisConfigError(
                f"malformed TOML in analysis config ({config_path}): {exc}"
            ) from exc

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
# GUR-103 task-3 additions: model-selection config schema
# ---------------------------------------------------------------------------
# These dataclasses mirror the TOML section structure from 2-plan.md §6:
#
#   [analysis]
#   default_agent = "claude_code"
#
#   [analysis.models.fallback]
#   fallback_models = ["gpt-4o-mini", "gemini-2.0-flash"]
#
#   [analysis.models]
#   claude_code = ""      # empty = use adapter default (SD §5.7.1)
#   codex = ""            # empty = raises ModelSelectionError
#   opencode = ""         # empty = raises ModelSelectionError
#
# These are ADDITIVE to the existing AnalysisConfig (read_project_file).
# Callers using only read_project_file do not need to instantiate these.
#
# NOTE: These config classes define the schema but do NOT load from TOML
# directly — loading is deferred to a future GlobalConfig loader that merges
# all sections. For now, callers construct these dataclasses from parsed TOML
# or from test fixtures.
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
    """

    claude_code: str = ""
    codex: str = ""
    opencode: str = ""
    fallback: FallbackModelsConfig = field(default_factory=FallbackModelsConfig)


@dataclass(frozen=True)
class GlobalAnalysisConfig:
    """Config for the [analysis] section in global config.toml.

    Combines model-selection keys (D7, D11) with the read_project_file
    settings already in AnalysisConfig.

    Attributes:
        default_agent: Which agent type to use by default. "auto" is opt-in (D7).
            Default: "claude_code".
        model: Empty for GlobalAnalysisConfig (model override lives at project scope).
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
    """

    model: str = ""
