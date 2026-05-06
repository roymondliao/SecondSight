"""RetentionConfig — TOML-backed retention policy resolution (task-A1, GUR-147).

This module is the FIRST config consumer in the codebase
(verification finding C1 in
``changes/2026-05-06_gur-107_phase3a-retention-observation-api/plan-verification.md``).
It defines the file format, not just consumes it.

Precedence (D4 in 2-plan.md):
    1. per-project: ``{home}/projects/{project_id}/config.toml`` ``[retention]``
    2. global: ``{home}/config.toml`` ``[retention]``
    3. built-in default: 90 days for ``raw_traces_ttl_days``

Each resolved TTL carries a ``source`` attribution
(``per_project_config`` / ``global_config`` / ``builtin_default``) so
cleanup runs can log which file the TTL came from. Without that
attribution, an operator has no way to verify their override took
effect — the silent-failure case from kickoff §3.

This module only resolves raw_traces_ttl_days for GUR-147 scope.
analysis_ttl_days defers to GUR-107b (blocked on Phase 2 / GUR-100).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

BUILTIN_DEFAULT_TTL_DAYS = 90

ConfigSource = Literal["per_project_config", "global_config", "builtin_default"]


class RetentionConfigError(Exception):
    """Raised when a config file is present but unreadable or has an
    invalid value. NOT raised when files are absent — that path uses
    the built-in default (DC-6b).
    """


@dataclass(frozen=True)
class RetentionConfig:
    """Resolved retention policy for ONE project.

    ``raw_traces_ttl_days`` is the effective TTL in days.
    ``source`` records which config layer supplied it.

    Use :meth:`load` to resolve from disk; the constructor is exposed
    for tests that want to fabricate a config without touching the
    filesystem.
    """

    raw_traces_ttl_days: int
    source: ConfigSource

    @classmethod
    def load(cls, *, home: Path, project_id: str) -> RetentionConfig:
        """Resolve retention policy for ``project_id`` under ``home``.

        Args:
            home: The SecondSight home directory (e.g. ``~/.secondsight``).
                Need not exist; missing → built-in default.
            project_id: Project identifier; the per-project config is
                read from ``{home}/projects/{project_id}/config.toml``.

        Returns:
            A frozen :class:`RetentionConfig` with the resolved value
            and its source attribution.

        Raises:
            RetentionConfigError: A config file IS present but cannot
                be parsed, or contains a value of the wrong type or a
                non-positive integer (DC-6).
        """
        home = Path(home)
        global_path = home / "config.toml"
        project_path = home / "projects" / project_id / "config.toml"

        per_project = _read_retention_section(project_path, label=project_id)
        if per_project is not None and "raw_traces_ttl_days" in per_project:
            value = _validate_ttl(
                per_project["raw_traces_ttl_days"],
                source_label=str(project_path),
            )
            return cls(
                raw_traces_ttl_days=value,
                source="per_project_config",
            )

        global_section = _read_retention_section(global_path, label="<global>")
        if global_section is not None and "raw_traces_ttl_days" in global_section:
            value = _validate_ttl(
                global_section["raw_traces_ttl_days"],
                source_label=str(global_path),
            )
            return cls(
                raw_traces_ttl_days=value,
                source="global_config",
            )

        return cls(
            raw_traces_ttl_days=BUILTIN_DEFAULT_TTL_DAYS,
            source="builtin_default",
        )


def _read_retention_section(path: Path, *, label: str) -> dict | None:
    """Read ``[retention]`` from ``path``. Return ``None`` if the file
    or section is absent. Raise on parse errors.

    Absent file is the fresh-install path (DC-6b) — never raise.
    Parse error is operator typo (DC-6) — surface loudly with
    ``label`` so they can locate the offending file.
    """
    if not path.is_file():
        return None
    try:
        with path.open("rb") as fh:
            doc = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise RetentionConfigError(
            f"malformed TOML in retention config for {label} ({path}): {exc}"
        ) from exc
    section = doc.get("retention")
    if not isinstance(section, dict):
        return None
    return section


def _validate_ttl(value: object, *, source_label: str) -> int:
    """Coerce a TOML-decoded value to a positive int TTL, or raise.

    A boolean is technically an int in Python but is rejected here:
    ``raw_traces_ttl_days = true`` is a typo, not "1 day".
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise RetentionConfigError(
            f"raw_traces_ttl_days in {source_label} must be a positive "
            f"integer, got {type(value).__name__}: {value!r}"
        )
    if value <= 0:
        raise RetentionConfigError(
            f"raw_traces_ttl_days in {source_label} must be a positive integer, got {value}"
        )
    return value


__all__ = [
    "BUILTIN_DEFAULT_TTL_DAYS",
    "ConfigSource",
    "RetentionConfig",
    "RetentionConfigError",
]
