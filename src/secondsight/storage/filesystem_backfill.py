"""FilesystemBackfill — `secondsight sync` core (P1-13).

SD §3.9.2 splits backfill into two recovery paths:

    Path A (sync_log replay) — server was up, DB INSERT raised. The raw
        trace JSON is on disk; sync_log.jsonl records the failure with
        ``raw_trace_path``. We re-INSERT from the raw trace and then
        clear the processed sync_log entries.

    Path B (filesystem walk) — belt-and-braces: walk every
        ``sessions/*/events/*.json`` and INSERT any whose event_id is
        not yet in the DB. Catches edge cases where neither sync_log
        nor pipeline saw the failure (e.g. the filesystem was rsync'd
        in from another machine).

Path C — fallback_events.jsonl replay (server-down) is deferred to a
follow-up. The fallback envelope shape currently lacks event_id /
sequence_number, so replaying it would require reconstructing those
from the adapter pipeline. Phase 1 sync ARCHIVES the fallback file
(atomic move to a timestamped .bak) so operators can see it accumulated
work without us silently dropping data. See P1-13 scar carry-forward.

Idempotency contract (Path A + B):
    EventsRepository.insert uses INSERT … ON CONFLICT DO NOTHING on
    event.id, so re-running sync against the same filesystem is a no-op
    on the DB side. The sync_log replay step removes processed lines
    only after the corresponding insert returned without raising.

Silent failure surface this module closes:
    * sync_log entry references a missing/corrupt raw_trace file ->
      surfaces as RawTraceCorruptionError; the entry is left in the log
      and counted in `BackfillReport.failures` (NOT silently dropped).
    * Filesystem walk encounters a corrupt JSON file -> recorded in
      `failures`; iteration continues so one bad file does not block the
      rest of the session.
    * fallback_events.jsonl is archived ONLY if the pre-archive
      ``has_lines`` check found content; an empty file is left alone.
      The .bak path embeds the wall-clock timestamp so multiple sync runs
      do not stomp on each other.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from secondsight.api.registry import ProjectResources
from secondsight.storage.raw_trace_store import RawTraceCorruptionError


@dataclass(frozen=True)
class BackfillReport:
    """Summary of what one project's sync run did. Returned to the CLI."""

    project_id: str
    sync_log_replayed: int = 0
    """Rows successfully re-INSERTed from sync_log entries."""

    sync_log_remaining: int = 0
    """Rows still in sync_log after the run (all failed to replay)."""

    filesystem_inserted: int = 0
    """Rows inserted by the filesystem walk that were absent from the DB."""

    filesystem_already_present: int = 0
    """Rows the filesystem walk found that were already in the DB."""

    failures: list[str] = field(default_factory=list)
    """Human-readable failure messages. Empty list = clean run."""


@dataclass(frozen=True)
class FallbackArchiveReport:
    """Summary of the fallback_events.jsonl archive step."""

    archived: bool
    """True iff the file was moved aside this run."""

    archive_path: Path | None
    """Where the archive landed (None if nothing was archived)."""

    line_count: int
    """Number of lines that were in the file when we archived it."""


class FilesystemBackfill:
    """Orchestrates Path A and Path B for one project."""

    def __init__(self, resources: ProjectResources) -> None:
        self._resources = resources
        # Eagerly ensure the DB schema exists. ProjectRegistry already calls
        # create_schema during materialisation, but a `secondsight sync` that
        # spins up a fresh ProjectResources directly (bypassing registry)
        # would otherwise see an empty DB and silently skip every INSERT.
        self._resources.events_repository.create_schema()

    def run(self) -> BackfillReport:
        """Execute Path A then Path B. Returns a structured report."""
        replayed, remaining, replay_failures = self._replay_sync_log()
        inserted, already, walk_failures = self._walk_filesystem()
        return BackfillReport(
            project_id=self._resources.project_id,
            sync_log_replayed=replayed,
            sync_log_remaining=remaining,
            filesystem_inserted=inserted,
            filesystem_already_present=already,
            failures=replay_failures + walk_failures,
        )

    # ------------------------------------------------------------------
    # Path A — sync_log replay
    # ------------------------------------------------------------------

    def _replay_sync_log(self) -> tuple[int, int, list[str]]:
        sync_log = self._resources.sync_log
        repo = self._resources.events_repository
        store = self._resources.raw_trace_store

        # Load all pending entries up-front, then rewrite the log with only
        # the ones we couldn't process. This is simpler and safer than
        # mutating in-place: any crash before the rewrite leaves the original
        # log intact, so the next sync run sees the same entries.
        pending = list(sync_log.iter_pending())
        if not pending:
            return 0, 0, []

        replayed = 0
        leftover: list[dict[str, object]] = []
        failures: list[str] = []

        for entry in pending:
            raw_path = Path(entry.raw_trace_path)
            try:
                event = store._read_sync(raw_path)  # noqa: SLF001 — read path
            except RawTraceCorruptionError as exc:
                # Keep the entry; surface to operator. We do NOT drop it
                # from the log because that would erase the failure record.
                failures.append(
                    f"sync_log: cannot replay {entry.event_id}: {exc}"
                )
                leftover.append(self._entry_to_jsonl_obj(entry))
                continue
            except OSError as exc:
                failures.append(
                    f"sync_log: I/O error replaying {entry.event_id}: {exc}"
                )
                leftover.append(self._entry_to_jsonl_obj(entry))
                continue

            try:
                repo.insert(event)
            except Exception as exc:  # noqa: BLE001 — surface to operator
                failures.append(
                    f"sync_log: DB insert failed for {entry.event_id}: "
                    f"{type(exc).__name__}: {exc}"
                )
                leftover.append(self._entry_to_jsonl_obj(entry))
                continue

            replayed += 1

        self._rewrite_sync_log(sync_log.path, leftover)
        return replayed, len(leftover), failures

    @staticmethod
    def _entry_to_jsonl_obj(entry: object) -> dict[str, object]:
        """Mirror SyncLog's own line shape for round-trip safety."""
        # `entry` is a SyncLogEntry dataclass — duck-type via attrs so we
        # don't import the dataclass into this module's API surface.
        return {
            "event_id": getattr(entry, "event_id"),
            "raw_trace_path": getattr(entry, "raw_trace_path"),
            "error_class": getattr(entry, "error_class"),
            "error_message": getattr(entry, "error_message"),
            "timestamp": getattr(entry, "timestamp"),
        }

    @staticmethod
    def _rewrite_sync_log(path: Path, lines: list[dict[str, object]]) -> None:
        """Atomic rewrite. Empty list deletes the file."""
        if not lines:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return
        body = "".join(
            json.dumps(obj, ensure_ascii=False) + "\n" for obj in lines
        )
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(body, encoding="utf-8")
        os.replace(tmp, path)

    # ------------------------------------------------------------------
    # Path B — filesystem walk
    # ------------------------------------------------------------------

    def _walk_filesystem(self) -> tuple[int, int, list[str]]:
        repo = self._resources.events_repository
        store = self._resources.raw_trace_store
        sessions_root = store.project_root / "sessions"
        if not sessions_root.is_dir():
            return 0, 0, []

        inserted = 0
        already = 0
        failures: list[str] = []

        for session_dir in sorted(sessions_root.iterdir()):
            if not session_dir.is_dir():
                continue
            events_dir = session_dir / "events"
            if not events_dir.is_dir():
                continue
            for path in sorted(events_dir.iterdir()):
                if path.suffix != ".json" or not path.is_file():
                    continue
                try:
                    event = store._read_sync(path)  # noqa: SLF001
                except RawTraceCorruptionError as exc:
                    failures.append(f"filesystem: corrupt {path}: {exc}")
                    continue
                except OSError as exc:
                    failures.append(f"filesystem: I/O error {path}: {exc}")
                    continue

                if repo.exists(event.id):
                    already += 1
                    continue
                try:
                    repo.insert(event)
                except Exception as exc:  # noqa: BLE001
                    failures.append(
                        f"filesystem: insert failed {event.id}: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    continue
                inserted += 1

        return inserted, already, failures


def archive_fallback_events(fallback_path: Path) -> FallbackArchiveReport:
    """Move ``fallback_events.jsonl`` aside if it has content.

    Phase 1 does not (yet) replay fallback events through adapters — see the
    module docstring's Path C note. This helper makes the file's accumulated
    work *visible* by rotating it to ``fallback_events.jsonl.<ts>.bak`` so:

        * a future Phase 2 replayer can pick it up from the .bak path;
        * the live file is reset to empty so no double-replay can occur;
        * operators never silently lose pending work between phases.

    Empty/missing files are left alone (no-op, archived=False).
    """
    if not fallback_path.is_file():
        return FallbackArchiveReport(
            archived=False, archive_path=None, line_count=0
        )

    # Count lines defensively. We tolerate trailing partial lines (matches
    # SyncLog.iter_pending policy: a process killed mid-write will leave at
    # most one truncated line, which we count once).
    try:
        line_count = sum(1 for _ in fallback_path.open("r", encoding="utf-8"))
    except OSError:
        line_count = 0
    if line_count == 0:
        return FallbackArchiveReport(
            archived=False, archive_path=None, line_count=0
        )

    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    archive_path = fallback_path.with_name(f"{fallback_path.name}.{ts}.bak")
    # Defensive: if a file with the same .bak name somehow exists (two syncs
    # in the same wall-clock second), append a counter rather than overwrite.
    counter = 0
    candidate = archive_path
    while candidate.exists():
        counter += 1
        candidate = fallback_path.with_name(
            f"{fallback_path.name}.{ts}-{counter}.bak"
        )
    archive_path = candidate

    os.replace(fallback_path, archive_path)
    return FallbackArchiveReport(
        archived=True, archive_path=archive_path, line_count=line_count
    )


__all__ = [
    "BackfillReport",
    "FallbackArchiveReport",
    "FilesystemBackfill",
    "archive_fallback_events",
]
