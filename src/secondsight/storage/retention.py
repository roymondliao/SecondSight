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

import shutil
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import sqlalchemy as sa
from loguru import logger

from secondsight.storage.events_table import events

if TYPE_CHECKING:
    from secondsight.storage.events_repository import EventsRepository
    from secondsight.storage.raw_trace_store import RawTraceStore

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


# ---------------------------------------------------------------------------
# RawTracesPurger — task-A4 (DC-5)
# ---------------------------------------------------------------------------

PurgeStage = Literal["filesystem", "database"]


@dataclass(frozen=True)
class PurgeFailure:
    """One session that failed to purge cleanly.

    ``stage`` distinguishes a pre-FS failure (DB row still intact, next
    reap will retry) from a post-FS failure (FS already gone, DB row
    still present — the explicit FS/DB drift D3 acknowledges).
    """

    session_id: str
    stage: PurgeStage
    error: str


@dataclass(frozen=True)
class PurgeResult:
    """Outcome of one ``RawTracesPurger.purge()`` invocation.

    Order of ``purged_session_ids`` matches input order (sessions that
    failed are NOT in this list — they appear in ``failures`` instead).
    """

    purged_session_ids: tuple[str, ...]
    failures: tuple[PurgeFailure, ...]

    @property
    def had_failures(self) -> bool:
        return bool(self.failures)


def _delete_fs_session(store: RawTraceStore, session_id: str) -> bool:
    """Remove ``sessions/{session_id}/`` from disk. Returns True if a
    directory was removed, False if it was already absent.

    Absent directory is the idempotent path: an operator may have
    already cleaned it manually. The DB cleanup must still proceed so
    the events row is also reaped.
    """
    session_dir = store.project_root / "sessions" / session_id
    if not session_dir.exists():
        return False
    shutil.rmtree(session_dir)
    return True


def _delete_db_events_for_session(repo: EventsRepository, session_id: str) -> int:
    """``DELETE FROM events WHERE session_id = ?``. Returns rowcount.

    Per-session rather than batched IN(...) so a single corrupt session
    cannot pull the whole batch down — the DC-5 contract says partial
    failure is recoverable and other sessions should still be reaped.
    """
    stmt = sa.delete(events).where(events.c.session_id == session_id)
    with repo._db.engine.begin() as conn:  # noqa: SLF001 — see retention module note
        return int(conn.execute(stmt).rowcount or 0)


class RawTracesPurger:
    """Destructive side of retention. FS first, DB second (D3).

    The two operations are NOT a single atomic transaction (a sqlite
    transaction cannot encompass an ``rmtree``). The purger explicitly
    chooses FS-first so a partial failure leaves a recoverable state on
    the DB side: if FS removal blew up, the DB row is still there and
    the next reap will re-attempt. The opposite order would leave the
    DB row deleted and the FS files orphaned forever — invisible
    to the enumerator on the next run.

    The flip side (D3 acknowledgement): if FS succeeds and DB then
    fails, the FS files ARE gone and we cannot put them back. We log a
    structured ERROR and keep going (DC-5). The CLI layer (task-A6)
    surfaces this as a non-zero exit code.
    """

    def __init__(
        self,
        *,
        repo: EventsRepository,
        raw_trace_store: RawTraceStore,
    ) -> None:
        self._repo = repo
        self._store = raw_trace_store

    def purge(self, expired: Sequence[ExpiredSession]) -> PurgeResult:
        purged: list[str] = []
        failures: list[PurgeFailure] = []

        for session in expired:
            sid = session.session_id

            # Stage 1: filesystem.
            try:
                _delete_fs_session(self._store, sid)
            except Exception as exc:  # noqa: BLE001 — boundary owns the catch
                logger.error(
                    "raw_traces purge: FS removal failed for session_id={sid}: {exc_type}: {exc}",
                    sid=sid,
                    exc_type=type(exc).__name__,
                    exc=exc,
                )
                failures.append(
                    PurgeFailure(
                        session_id=sid,
                        stage="filesystem",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                # FS-first contract: do NOT touch DB if FS failed. Next
                # reap will re-enumerate and retry both sides.
                continue

            # Stage 2: database. FS files are now gone for `sid`.
            try:
                _delete_db_events_for_session(self._repo, sid)
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "raw_traces purge: DB delete failed for session_id={sid} "
                    "AFTER filesystem removal — DB/FS drift; manual reconcile "
                    "required: {exc_type}: {exc}",
                    sid=sid,
                    exc_type=type(exc).__name__,
                    exc=exc,
                )
                failures.append(
                    PurgeFailure(
                        session_id=sid,
                        stage="database",
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue

            purged.append(sid)

        return PurgeResult(
            purged_session_ids=tuple(purged),
            failures=tuple(failures),
        )


__all__ = [
    "BUILTIN_DEFAULT_TTL_DAYS",
    "ConfigSource",
    "ExpiredSession",
    "PurgeFailure",
    "PurgeResult",
    "PurgeStage",
    "RawTracesPurger",
    "RetentionConfig",
    "RetentionConfigError",
    "enumerate_expired_sessions",
]
