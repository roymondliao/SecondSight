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
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import sqlalchemy as sa

from secondsight.storage.events_table import events

if TYPE_CHECKING:
    from secondsight.storage.events_repository import EventsRepository

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


@dataclass(frozen=True)
class ExpiredSession:
    """One session whose retention has expired and is eligible for cleanup.

    ``last_event_at`` is preserved on the result so the cleanup log
    line can attribute *why* the session was selected (D4: the cleanup
    audit trail). Without it, an operator looking at "session reaped"
    has no way to verify the boundary was applied correctly.
    """

    session_id: str
    last_event_at: datetime


def enumerate_expired_sessions(
    repo: EventsRepository,
    *,
    raw_traces_ttl_days: int,
    now: datetime,
) -> list[ExpiredSession]:
    """Return sessions whose most-recent event is at or before the TTL
    cutoff (``now - raw_traces_ttl_days``).

    The boundary is ``last_event_at``, NOT ``created_at`` (decision D1
    in 2-plan.md): a session that was *first* observed 100d ago but had
    its most-recent event 5 minutes ago is still observably alive and
    must not be reaped (DC-2).

    Inclusive boundary: a session whose last event is *exactly* at
    ``now - ttl_days`` IS expired. Strict inequality would let
    sessions linger one tick past their advertised TTL.

    Args:
        repo: EventsRepository for the project being scanned.
        raw_traces_ttl_days: Resolved TTL in days.
        now: Wall-clock reference for cutoff computation. Passed in so
            tests are deterministic (DC-1, DC-2 use fixed timestamps).

    Returns:
        List of :class:`ExpiredSession` ordered by session_id ascending
        for stable cleanup logs and reproducible ``--dry-run`` output.

    Raises:
        Nothing for the empty case (DC-1). Underlying SQLAlchemy errors
        propagate; this function does not catch them.
    """
    cutoff = now - timedelta(days=raw_traces_ttl_days)
    stmt = (
        sa.select(
            events.c.session_id,
            sa.func.max(events.c.timestamp).label("last_event_at"),
        )
        .group_by(events.c.session_id)
        .having(sa.func.max(events.c.timestamp) <= cutoff)
        .order_by(events.c.session_id.asc())
    )
    with repo._db.engine.connect() as conn:  # noqa: SLF001 — see note below
        rows = conn.execute(stmt).all()
    return [ExpiredSession(session_id=r.session_id, last_event_at=r.last_event_at) for r in rows]


# Note on the `repo._db` access above: the retention module sits next
# to EventsRepository in the storage layer and intentionally co-owns
# the events-table schema (events_table.events is imported at the top
# of this module). Adding a public `engine` property to the repo would
# leak SQLAlchemy lower-level types to callers who don't need them.
# We could alternatively add `get_expired_session_ids(cutoff)` to the
# repo, but that mixes retention policy into a generic repo API.
# Co-owning the storage internals here is the lesser of two evils.


__all__ = [
    "BUILTIN_DEFAULT_TTL_DAYS",
    "ConfigSource",
    "ExpiredSession",
    "RetentionConfig",
    "RetentionConfigError",
    "enumerate_expired_sessions",
]
