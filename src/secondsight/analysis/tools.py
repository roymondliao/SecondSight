"""AnalysisTools — framework-agnostic tool layer for the analysis agent (GUR-103 task-1).

The four tool methods mirror SD §5.4 names:
  - read_traces(session_id)          → thin pass-through to EventsRepository
  - read_project_file(relative_path) → security-critical file access
  - query_structured_store(query)    → two approved shapes only
  - read_historical_flags(project_id, limit) → grouped BehaviorFlag history

Security contracts enforced by read_project_file:
  DC-1 (Symlink escape): Path.resolve(strict=True) + is_relative_to re-check.
      Symlinks that resolve outside the project root are blocked at WARN level.
      The project_root is resolved ONCE at construction time to eliminate the
      TOCTOU window where a symlink retarget of the root itself could shift the
      containment boundary between calls.
  DC-2 (Denylist case bypass): every path component (including ancestor
      directories) is matched case-insensitively against the denylist.
      The mechanism is component-level exact/glob matching: each component of
      the relative path (e.g. ".ssh", "id_rsa") is independently tested against
      every denylist pattern. So ".ssh" blocks ALL reads inside .ssh/ — the
      directory component matches the ".ssh" pattern, causing rejection before
      the filename is even checked.

query_structured_store v1 vocabulary:
    Exactly two approved `kind` values:
    - `behavior_flag_summary`: maps to BehaviorFlagsRepository.count_by_type
    - `directive_active`: maps to DirectivesRepository.get_active_conventions
    New shapes are intentional API expansion. Each new shape requires:
      1. A typed query dataclass.
      2. A repo method.
      3. A test in test_tools.py.
    No free-form kind expansion.

Async contract:
    read_project_file is async because it uses `asyncio.to_thread` for the
    blocking `Path.read_bytes()` call. Callers MUST `await` it. PydanticAI
    tool registration handles this transparently in task-4 (sdk/agent.py),
    but the contract is documented here for callers in other contexts.

This module is domain-agnostic: it contains NO PydanticAI-specific code.
The SDK layer (task-4) wraps these methods into PydanticAI tools.

Design assumption:
    project_root is an absolute path pointing to an existing directory.
    The constructor resolves it at construction time. If project_root=None,
    read_project_file raises ProjectFileToolError immediately — no guessing,
    no CWD fallback.

If this assumption stops holding (e.g., the registry provides a lazy
project_root), the first thing to rot is the construction-time resolve
raising on a directory that hasn't been created yet — construction would
fail before any tool is ever called.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
from pathlib import Path
from typing import Any

from secondsight.analysis.config import BUILTIN_SIZE_CAP_KB
from secondsight.analysis.schemas import BehaviorFlagType
from secondsight.event import Event
from secondsight.storage.behavior_flags_repository import BehaviorFlagsRepository
from secondsight.storage.directives_repository import DirectivesRepository
from secondsight.storage.events_repository import EventsRepository

_logger = logging.getLogger(__name__)

# ---- Size cap (in bytes, derived from config.py's BUILTIN_SIZE_CAP_KB) ----
# Single source of truth: BUILTIN_SIZE_CAP_KB in config.py.
# Computed here once at module load to avoid repeated multiplication.
_DEFAULT_SIZE_CAP_BYTES: int = BUILTIN_SIZE_CAP_KB * 1024  # 262144 bytes

# ---- Built-in denylist ----
# Any component (filename OR ancestor directory name) matching one of these patterns
# (case-insensitively) causes read_project_file to reject the request.
#
# Patterns follow fnmatch semantics (shell globbing, case-insensitive via .lower()).
# Adding a new pattern here requires:
#   1. A death test (DT-1.X) in test_tools.py.
#   2. A comment explaining WHY this pattern is in the built-in list.
_BUILTIN_DENYLIST: tuple[str, ...] = (
    ".env",  # dotenv files carry secrets
    ".env.*",  # .env.local, .env.production, etc.
    "*credentials*",  # any file with "credentials" in its name
    "*secret*",  # any file with "secret" in its name
    "*.pem",  # PEM-encoded private keys / certs
    "id_rsa*",  # SSH private keys (id_rsa, id_rsa.pub)
    ".aws",  # AWS credentials directory; any path component matching
    # ".aws" (case-insensitive) blocks the entire subtree
    # because the directory component is checked.
    ".ssh",  # SSH directory; same mechanism — ".ssh" component blocks
    # all reads inside .ssh/ without needing a "/*" suffix.
    # NOTE: ".aws/*" and ".ssh/*" patterns were removed (GUR-103 fix-loop).
    # Path.parts yields components without slashes (.ssh, id_rsa), so fnmatch
    # against ".aws/*" can never match — the "/*" suffix is dead. The ".aws"
    # and ".ssh" exact-name entries already block the entire directory tree
    # via component-level matching. No functional change; dead patterns removed.
)

# ---- Valid query_structured_store kinds ----
# Exhaustive set. See module docstring for expansion protocol.
_VALID_QUERY_KINDS: frozenset[str] = frozenset({"behavior_flag_summary", "directive_active"})


class ProjectFileToolError(Exception):
    """Raised when read_project_file cannot or should not read a file.

    Covers all file-access failure modes uniformly so callers don't need
    to handle FileNotFoundError, PermissionError, etc. separately.
    Callers catch ProjectFileToolError only; no other exception should
    escape from read_project_file.
    """


class AnalysisTools:
    """Framework-agnostic tool layer for the GUR-103 analysis agent.

    Each method corresponds to one tool available to the LLM.
    Methods are async-first (even read_traces, which does sync DB work
    in practice) to allow the SDK layer (task-4) to register them
    uniformly as PydanticAI async tools.

    Construction:
        tools = AnalysisTools(
            project_root=Path("/home/user/.secondsight/projects/proj-alpha"),
            events_repo=events_repo,
            flags_repo=flags_repo,
            directives_repo=directives_repo,
        )

    If project_root=None, read_project_file raises ProjectFileToolError
    immediately on any call.

    Security note: project_root is resolved at construction time (not on
    every call) to eliminate the TOCTOU window where a symlink retarget
    of the project root itself could shift the containment boundary
    between successive read_project_file calls.
    """

    def __init__(
        self,
        *,
        project_root: Path | None,
        events_repo: EventsRepository,
        flags_repo: BehaviorFlagsRepository,
        directives_repo: DirectivesRepository,
        extra_denylist: list[str] | None = None,
        size_cap_bytes: int = _DEFAULT_SIZE_CAP_BYTES,
        read_project_file_enabled: bool = True,
    ) -> None:
        """Initialise AnalysisTools.

        Args:
            project_root: Absolute path to the project's root directory.
                Pass None if no project is configured — read_project_file
                raises ProjectFileToolError on any call.
                The path is resolved (symlinks followed) at construction
                time to establish a stable containment boundary.
            events_repo: EventsRepository for read_traces.
            flags_repo: BehaviorFlagsRepository for read_historical_flags
                and query_structured_store(kind=behavior_flag_summary).
            directives_repo: DirectivesRepository for
                query_structured_store(kind=directive_active).
            extra_denylist: Additional denylist patterns (from project config).
                These are ADDITIVE; cannot remove built-in patterns.
            size_cap_bytes: Maximum file content to return in bytes.
                Default: 256 KiB (from BUILTIN_SIZE_CAP_KB in config.py).
                Must be a positive integer.
            read_project_file_enabled: Kill switch for the read_project_file
                tool (D8 escape valve). When False, every call to
                read_project_file raises ProjectFileToolError immediately —
                no FS access attempted. Default True so existing callers keep
                working without passing this argument.
                Set via AnalysisConfig.read_project_file_enabled from
                [analysis.read_project_file] enabled = false in project config.

        Raises:
            ValueError: If project_root is not None but not absolute.
        """
        if project_root is not None:
            project_root = Path(project_root)
            if not project_root.is_absolute():
                raise ValueError(
                    f"AnalysisTools: project_root must be an absolute path, got {project_root!r}"
                )
            # Resolve once at construction time (TOCTOU mitigation):
            # a symlink retarget of the project root itself after construction
            # cannot shift the containment boundary, because we snapshot the
            # resolved form here.
            self._project_root_resolved: Path | None = project_root.resolve()
        else:
            self._project_root_resolved = None

        self._events_repo = events_repo
        self._flags_repo = flags_repo
        self._directives_repo = directives_repo
        self._extra_denylist: tuple[str, ...] = tuple(extra_denylist or [])
        self._size_cap_bytes = size_cap_bytes
        self._read_project_file_enabled = read_project_file_enabled

    # ------------------------------------------------------------------
    # Tool: read_traces
    # ------------------------------------------------------------------

    async def read_traces(self, session_id: str) -> list[Event]:
        """Return all events for a session in sequence order.

        Thin pass-through to EventsRepository.get_session_events.

        Note: returns Event objects, not OpenTelemetry traces. The name
        "read_traces" reflects SD §5.4 vocabulary; data shape is
        `Event[EventType]` from EventsRepository.get_session_events.

        Args:
            session_id: The session identifier to fetch events for.

        Returns:
            List of Event objects, ordered by sequence_number ascending.
            Empty list if the session has no events.
        """
        return await asyncio.to_thread(self._events_repo.get_session_events, session_id)

    # ------------------------------------------------------------------
    # Tool: read_project_file
    # ------------------------------------------------------------------

    async def read_project_file(self, relative_path: str) -> str:
        """Read a project file safely.

        Security invariants enforced (see module docstring for full detail):
          1. project_root must be configured (not None).
          2. Resolved path must remain inside project_root (DC-1 symlink guard).
             project_root was resolved at construction; this path is resolved
             at call time with strict=True.
          3. Every path component is matched case-insensitively against the
             denylist (DC-2 case bypass + ancestor directory guard).
          4. Files larger than size_cap_bytes are truncated with a marker.
          5. Binary files (non-UTF-8 or containing null bytes) return a
             placeholder, never raw bytes.

        Args:
            relative_path: Path relative to the project root.
                Must not start with '/'. Traversal via '..' is blocked
                by the is_relative_to check after resolution.

        Returns:
            UTF-8 decoded file content (possibly truncated).
            `<binary file: N bytes>` if the file contains null bytes or
            is not valid UTF-8.
            `<truncated: original size N bytes>` appended if truncated.

        Raises:
            ProjectFileToolError: For ALL file access failures — missing
                project_root, path escape, denylist hit, file not found,
                permission denied, etc. FileNotFoundError never escapes.
        """
        # Step 1a: D8 kill switch — per-project escape valve.
        if not self._read_project_file_enabled:
            raise ProjectFileToolError(
                "read_project_file is disabled by project config "
                "(read_project_file_enabled=False). "
                "Set [analysis.read_project_file] enabled = true to re-enable."
            )

        # Step 1b: project_root must be configured.
        if self._project_root_resolved is None:
            raise ProjectFileToolError(
                "read_project_file: no project root configured. "
                "AnalysisTools was constructed with project_root=None."
            )

        project_root_resolved = self._project_root_resolved

        # Step 2: Resolve the path (strict=True raises FileNotFoundError on
        # non-existent paths; we wrap it below).
        try:
            resolved = await asyncio.to_thread(
                _resolve_strict, project_root_resolved, relative_path
            )
        except FileNotFoundError:
            # Uniform error: callers catch ProjectFileToolError only.
            # FileNotFoundError must never escape (scar report item).
            raise ProjectFileToolError(f"read_project_file: file not found: {relative_path!r}")
        except OSError as exc:
            raise ProjectFileToolError(
                f"read_project_file: OS error accessing {relative_path!r}: {exc}"
            ) from exc

        # Step 3: Post-resolve sandbox check — the resolved path must still
        # be inside project_root_resolved. Catches DC-1 symlink escape.
        # Note: we use the construction-time resolved root (not re-resolving
        # here) to maintain a stable containment boundary.
        if not resolved.is_relative_to(project_root_resolved):
            _logger.warning(
                "read_project_file: sandbox violation — resolved path %r is outside "
                "project root %r (requested: %r). Blocking.",
                str(resolved),
                str(project_root_resolved),
                relative_path,
            )
            raise ProjectFileToolError(
                f"read_project_file: path {relative_path!r} resolves outside project root "
                f"(resolved: {resolved!r})"
            )

        # Step 4: Denylist check — every component of the relative path.
        # DC-2: case-insensitive, checks ancestor directories too.
        try:
            rel = resolved.relative_to(project_root_resolved)
        except ValueError:
            # Should never reach here given the is_relative_to check above,
            # but guard defensively.
            raise ProjectFileToolError(
                f"read_project_file: could not compute relative path for {resolved!r}"
            )

        self._check_denylist(rel, original_path=relative_path)

        # Step 5: Read file content asynchronously (DC-5: blocking IO in thread).
        raw_bytes = await asyncio.to_thread(resolved.read_bytes)

        # Step 6: Binary check on the FULL file BEFORE truncation.
        # Null bytes are the primary indicator of binary content even though
        # b"\x00".decode("utf-8") succeeds (null is a valid UTF-8 codepoint).
        # Returning null bytes in a JSON-encoded tool output would silently
        # corrupt the LLM's JSON decoding or inject control characters.
        #
        # IMPORTANT: This check must happen before the size cap slice below.
        # A file with text in the first 256 KiB and null bytes after would
        # otherwise pass the binary guard and return truncated text — violating
        # the contract "binary files return a placeholder, never raw bytes".
        original_size = len(raw_bytes)
        if b"\x00" in raw_bytes:
            return f"<binary file: {original_size} bytes>"
        try:
            # Also validate UTF-8 decodability on the full bytes before truncation.
            # A file that is valid UTF-8 in the first 256 KiB but not globally
            # should still return binary placeholder (not truncated-but-decoded text).
            raw_bytes.decode("utf-8")
        except UnicodeDecodeError:
            return f"<binary file: {original_size} bytes>"

        # Step 7: Size cap (only reached for confirmed text files).
        truncated = False
        if original_size > self._size_cap_bytes:
            raw_bytes = raw_bytes[: self._size_cap_bytes]
            truncated = True

        # Step 8: Decode the (possibly truncated) text bytes — safe, full file was UTF-8.
        content = raw_bytes.decode("utf-8")

        if truncated:
            content += f"<truncated: original size {original_size} bytes>"

        return content

    def _check_denylist(self, rel: Path, *, original_path: str) -> None:
        """Check every component of rel against the combined denylist.

        Args:
            rel: Relative path from project root (already resolved).
            original_path: The original user-supplied path string
                (preserved in the warning log — DC-2 requirement).

        Raises:
            ProjectFileToolError: if any component matches any denylist pattern.
        """
        combined_denylist = _BUILTIN_DENYLIST + self._extra_denylist
        # Iterate over all components: parts includes every directory and the
        # final filename. E.g., ".ssh/id_rsa" → (".ssh", "id_rsa").
        for component in rel.parts:
            for pattern in combined_denylist:
                if fnmatch.fnmatchcase(component.lower(), pattern.lower()):
                    _logger.warning(
                        "read_project_file: denylist hit — component %r (from path %r) "
                        "matched pattern %r. Blocking.",
                        component,  # original case preserved
                        original_path,
                        pattern,
                    )
                    raise ProjectFileToolError(
                        f"read_project_file: path {original_path!r} contains component "
                        f"{component!r} which matches denylist pattern {pattern!r}"
                    )

    # ------------------------------------------------------------------
    # Tool: query_structured_store
    # ------------------------------------------------------------------

    async def query_structured_store(self, query: dict[str, Any]) -> Any:
        """Query the structured data store with an approved query shape.

        v1 supports exactly two `kind` values:
          - `behavior_flag_summary`: counts of flags per BehaviorFlagType
              for a project. Requires `project_id` in query.
          - `directive_active`: list of active conventions for a project.
              Requires `project_id` in query.

        Raises:
            ValueError: If `kind` is missing, empty, or not in the approved
                vocabulary. Also raised if `project_id` is missing or empty.

        Expansion protocol: each new `kind` requires:
          1. A typed query dataclass.
          2. A repo method (or existing repo method confirmed sufficient).
          3. A death test + a happy-path test in test_tools.py.
          No free-form expansion — kind must be an explicitly approved string.
        """
        kind = query.get("kind", "")
        if not kind:
            raise ValueError(
                "query_structured_store: query must contain a non-empty 'kind' key. "
                f"Got query={query!r}"
            )
        if kind not in _VALID_QUERY_KINDS:
            raise ValueError(
                f"query_structured_store: unknown kind={kind!r}. "
                f"Approved kinds: {sorted(_VALID_QUERY_KINDS)}. "
                "To add a new kind, follow the expansion protocol in tools.py."
            )

        project_id: str = query.get("project_id", "")
        if not project_id:
            raise ValueError(
                f"query_structured_store: kind={kind!r} requires a non-empty "
                f"'project_id' in the query. Got query={query!r}"
            )

        if kind == "behavior_flag_summary":
            return await asyncio.to_thread(self._flags_repo.count_by_type, project_id)

        if kind == "directive_active":
            return await asyncio.to_thread(self._directives_repo.get_active_conventions, project_id)

        # This line is unreachable given the vocabulary check above,
        # but explicit exhaustion prevents silent no-op if the set drifts.
        raise ValueError(  # pragma: no cover
            f"query_structured_store: kind={kind!r} passed vocabulary check but "
            "has no handler — this is a programming error."
        )

    # ------------------------------------------------------------------
    # Tool: read_historical_flags
    # ------------------------------------------------------------------

    async def read_historical_flags(
        self, project_id: str, limit: int = 200
    ) -> dict[BehaviorFlagType, list[Any]]:
        """Return historical behavior flags for a project, grouped by flag_type.

        Args:
            project_id: The project identifier.
            limit: Maximum number of flags to return PER FLAG TYPE.
                Default: 200. Applied per-type, not globally.
                NOTE: The underlying repo fetches ALL flags then truncates
                in Python. For projects with many flags, this is bounded by
                BehaviorFlagType enum size (currently 6 types × N flags).
                A SQL-level LIMIT is the v2 optimization; accepted for v1.

        Returns:
            Dict mapping BehaviorFlagType → list of BehaviorFlag objects.
            Flag types with zero flags are NOT included in the dict.
        """
        result: dict[BehaviorFlagType, list[Any]] = {}
        for flag_type in BehaviorFlagType:
            flags = await asyncio.to_thread(
                self._flags_repo.get_project_flags_by_type,
                project_id,
                flag_type,
            )
            if flags:
                result[flag_type] = flags[:limit]
        return result


# ------------------------------------------------------------------
# Private helpers (pure functions, no AnalysisTools state)
# ------------------------------------------------------------------


def _resolve_strict(project_root: Path, relative_path: str) -> Path:
    """Resolve project_root / relative_path with strict=True.

    strict=True raises FileNotFoundError for non-existent paths.
    This runs in a thread because it performs FS I/O (stat calls).

    This is extracted as a free function so it can be called via
    asyncio.to_thread without capturing self, keeping the thread
    boundary explicit.
    """
    return Path(project_root / relative_path).resolve(strict=True)


__all__ = [
    "AnalysisTools",
    "ProjectFileToolError",
]
