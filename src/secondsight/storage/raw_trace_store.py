"""RawTraceStore — filesystem-first durability layer (P1-1).

Filesystem is the source of truth (SD §3.1). This module is the ONLY
intentional writer of event JSON files in `sessions/{session_id}/events/`.

Durability contract:
- write(event): raises on any failure. The destination path either holds
  a fully-readable JSON or it does not exist. No partial files survive.
- read(path): raises a typed error on any corruption. Never returns a
  partial event silently.

Async strategy: stdlib sync I/O wrapped in `asyncio.to_thread`. Rationale:
the SQLAlchemy DB writes are sync anyway and use the same wrapper, keeping
the I/O model uniform. At the documented event rate (0.5–2 events/sec,
SD §3.2) the overhead of thread-pool dispatch is negligible.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path

from secondsight.event import Event

_SAFE_SESSION_ID = re.compile(r"^[A-Za-z0-9_\-:.]+$")


def is_safe_session_id(value: str) -> bool:
    """Return True if ``value`` matches the strict session-id regex.

    Public defense-in-depth helper for callers (e.g. ``RawTracesPurger``)
    that consume session_ids from storage and use them as path components
    in destructive operations. The regex is the same one enforced at write
    time by :meth:`RawTraceStore.event_path`.
    """
    return _SAFE_SESSION_ID.fullmatch(value) is not None


class RawTraceStoreError(Exception):
    """Base class for raw-trace-store errors."""


class UnsafePathError(RawTraceStoreError):
    """A computed path would escape the project root or contain unsafe chars."""


class RawTraceCorruptionError(RawTraceStoreError):
    """A read operation found a file that is missing, truncated, or invalid."""


class RawTraceStore:
    """Filesystem-first event store. One JSON file per event."""

    def __init__(self, project_root: Path) -> None:
        self._project_root = Path(project_root).resolve()

    @property
    def project_root(self) -> Path:
        return self._project_root

    def event_path(self, event: Event) -> Path:
        """Compute the on-disk path for an event. Pure, deterministic.

        Raises UnsafePathError if the event's session_id would escape the
        project root or contains unsafe characters. event_type is an Enum
        so it is constrained by the type system.
        """
        if not _SAFE_SESSION_ID.fullmatch(event.session_id):
            raise UnsafePathError(f"session_id contains unsafe characters: {event.session_id!r}")

        events_dir = self._project_root / "sessions" / event.session_id / "events"
        # Re-resolve and verify the result is still under project_root —
        # belt-and-braces against any creative input that slips past the regex.
        try:
            resolved_parent = events_dir.resolve().parent.parent.parent
        except (OSError, RuntimeError) as exc:  # pragma: no cover — defensive
            raise UnsafePathError(str(exc)) from exc

        if resolved_parent != self._project_root:
            raise UnsafePathError(f"session_id escapes project root: {event.session_id!r}")

        ts_part = (
            event.timestamp.strftime("%Y%m%dT%H%M%S")
            + f"{event.timestamp.microsecond // 1000:03d}Z"
        )
        filename = f"{ts_part}_{event.event_type.value}_seq{event.sequence_number:06d}.json"
        return events_dir / filename

    async def write(self, event: Event) -> Path:
        """Atomically write an event JSON. Returns the destination path.

        Raises:
            UnsafePathError: session_id is unsafe.
            OSError: filesystem unavailable / permission denied / disk full.
            BaseException: re-raised after cleanup of any tmp file.
        """
        target = self.event_path(event)
        return await asyncio.to_thread(self._write_sync, event, target)

    @staticmethod
    def _write_sync(event: Event, target: Path) -> Path:
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = event.model_dump_json().encode("utf-8")

        # Atomic write: tmp file in the SAME directory as target so
        # `os.replace` is a single-filesystem rename (POSIX atomic).
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=".tmp_",
            suffix=".json",
            dir=str(target.parent),
        )
        _needs_cleanup = True
        try:
            with os.fdopen(tmp_fd, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, target)
            _needs_cleanup = False  # ownership transferred
        finally:
            if _needs_cleanup and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:  # pragma: no cover — best-effort cleanup
                    pass
        return target

    async def read(self, path: Path) -> Event:
        """Read an event back from disk.

        Raises:
            RawTraceCorruptionError: file missing, truncated, or non-JSON.
        """
        return await asyncio.to_thread(self._read_sync, path)

    @staticmethod
    def _read_sync(path: Path) -> Event:
        try:
            raw = path.read_bytes()
        except FileNotFoundError as exc:
            raise RawTraceCorruptionError(f"missing file: {path}") from exc
        except OSError as exc:
            raise RawTraceCorruptionError(f"cannot read {path}: {exc}") from exc
        if not raw:
            raise RawTraceCorruptionError(f"empty file: {path}")
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RawTraceCorruptionError(f"invalid JSON in {path}: {exc}") from exc
        try:
            return Event.model_validate(obj)
        except Exception as exc:
            raise RawTraceCorruptionError(f"event validation failed for {path}: {exc}") from exc

    async def iter_session(self, session_id: str) -> AsyncIterator[Path]:
        """Yield every event path for a session in lexicographic order."""
        if not _SAFE_SESSION_ID.fullmatch(session_id):
            raise UnsafePathError(f"session_id contains unsafe characters: {session_id!r}")
        events_dir = self._project_root / "sessions" / session_id / "events"
        if not events_dir.exists():
            return
        # sort lexicographically — filename embeds ISO timestamp + seq
        for child in sorted(events_dir.iterdir()):
            if child.is_file() and child.suffix == ".json":
                yield child
