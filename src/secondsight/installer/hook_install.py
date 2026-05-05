"""HookInstaller — copy bundled hook scripts to a target directory.

Source-of-truth for the hook scripts is ``scripts/hooks/`` at the repo root
(SD §3.9.2 — shipped in P1-8). This module locates that bundle at runtime
and copies it to ``~/.claude/hooks/``.

Discovery order (first hit wins):
    1. ``<package_root>/_resources/hooks/`` — present when the wheel was
       built with the ``[tool.hatch.build.targets.wheel.force-include]``
       directive that bundles ``scripts/hooks/`` into the package.
    2. ``<repo_root>/scripts/hooks/`` — present when running from a source
       checkout (editable install / pytest). We climb up from
       ``Path(secondsight.__file__)`` until we find ``pyproject.toml``.

Copy semantics:
    * Files are copied with mode 0o755 so Claude Code can exec them. We do
      NOT preserve source mode bytewise (a file checked in with 0o644 must
      still be executable after install).
    * Existing files at the destination are overwritten only if their bytes
      differ. Same-byte writes are skipped so install is idempotent at the
      filesystem timestamp level — important for diff-based observers.
    * We never delete files in the destination. The caller's cleanup
      responsibility ends at this directory boundary.

Silent failure surface this module closes:
    * Missing source bundle (broken install) -> raises ``HookBundleNotFoundError``
      so the CLI can print a useful error rather than silently producing an
      empty install.
    * Destination exists with a directory of wrong type (e.g. user has a
      symlink to a parent dir loop) -> raises OSError; we do not try to
      "fix" the destination filesystem.
    * Partial copy: each file is written via tmpfile + os.replace so a
      crashed install never leaves a half-written hook script that Claude
      Code might exec.
"""

from __future__ import annotations

import filecmp
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import secondsight as _ss_pkg  # for __file__-based discovery

# Canonical filenames in the bundle. Any file present in the bundle but NOT
# in this list is silently ignored by the installer — we do not want a stray
# helper file (e.g. a future `_lib_v2.sh`) to land in the user's hook dir
# unannounced. _lib.sh is included because every per-event script sources it.
HOOK_FILES: tuple[str, ...] = (
    "_lib.sh",
    "pre-tool-use.sh",
    "post-tool-use.sh",
    "user-prompt.sh",
    "session-start.sh",
    "session-end.sh",
)

_EXEC_MODE = 0o755


class HookBundleNotFoundError(FileNotFoundError):
    """The hook-script bundle could not be located on disk.

    This means either the wheel was built without the force-include directive
    OR the source checkout no longer has scripts/hooks/. Either way the
    installer cannot proceed; we surface a typed error so the CLI command
    can pretty-print a recovery hint.
    """


@dataclass(frozen=True)
class HookInstallPlan:
    """The result of an install pass.

    `copied`: filenames that were written (new or content-changed).
    `skipped_identical`: filenames already at destination with matching bytes.
    `target_dir`: where the files were placed.
    `source_dir`: where they came from (useful in test/dev for debugging).
    """

    target_dir: Path
    source_dir: Path
    copied: list[str] = field(default_factory=list)
    skipped_identical: list[str] = field(default_factory=list)


class HookInstaller:
    """Copy SecondSight hook scripts into a target directory."""

    def __init__(
        self,
        *,
        source_dir: Path | None = None,
    ) -> None:
        """Construct an installer.

        Args:
            source_dir: Override the auto-discovered bundle location. Used
                by tests to point at a fixture directory; in production
                this is None and we use ``bundled_hook_dir()``.
        """
        self._source_dir = source_dir

    def install(
        self,
        target_dir: Path,
        *,
        dry_run: bool = False,
    ) -> HookInstallPlan:
        """Copy every entry of HOOK_FILES into ``target_dir``.

        Raises:
            HookBundleNotFoundError: source bundle missing.
            FileNotFoundError: a file listed in HOOK_FILES is absent from
                the bundle (broken release; not user-recoverable).
            OSError: filesystem error at destination.

        Returns:
            HookInstallPlan describing what was (or would be) written.
        """
        source = self._source_dir or bundled_hook_dir()
        if not source.is_dir():
            raise HookBundleNotFoundError(
                f"Hook bundle not found at {source}. "
                f"Either the SecondSight install is broken (wheel missing "
                f"force-included scripts/hooks/) or the source checkout has "
                f"no scripts/hooks/ directory."
            )

        copied: list[str] = []
        skipped: list[str] = []

        if not dry_run:
            target_dir.mkdir(parents=True, exist_ok=True)

        for name in HOOK_FILES:
            src_path = source / name
            if not src_path.is_file():
                raise FileNotFoundError(
                    f"Required hook bundle file missing: {src_path}. "
                    f"This is a packaging bug, not a user error."
                )
            dst_path = target_dir / name

            # Skip-if-identical: cheap byte compare. shallow=False so we read
            # contents (file size alone is not enough — _lib.sh edits often
            # keep size constant).
            if dst_path.is_file() and filecmp.cmp(src_path, dst_path, shallow=False):
                skipped.append(name)
                continue

            if dry_run:
                copied.append(name)
                continue

            self._atomic_copy(src_path, dst_path)
            copied.append(name)

        return HookInstallPlan(
            target_dir=target_dir,
            source_dir=source,
            copied=copied,
            skipped_identical=skipped,
        )

    @staticmethod
    def _atomic_copy(src: Path, dst: Path) -> None:
        """Copy `src` to `dst` atomically, marking the result executable."""
        # Tmp file in the SAME directory so os.replace is single-fs.
        tmp_fd, tmp_path = tempfile.mkstemp(
            prefix=f".tmp_{dst.name}_",
            dir=str(dst.parent),
        )
        os.close(tmp_fd)
        needs_cleanup = True
        try:
            shutil.copyfile(src, tmp_path)
            os.chmod(tmp_path, _EXEC_MODE)
            os.replace(tmp_path, dst)
            needs_cleanup = False
        finally:
            if needs_cleanup and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


def bundled_hook_dir() -> Path:
    """Return the path to the bundled hook-script directory.

    Discovery order documented at module level. We do NOT raise here when
    nothing is found — the caller (HookInstaller.install) does, with a more
    actionable message. This function is best-effort; treat the returned
    path as a candidate that may not exist.
    """
    pkg_root = Path(_ss_pkg.__file__).resolve().parent
    packaged = pkg_root / "_resources" / "hooks"
    if (packaged / "_lib.sh").is_file():
        return packaged

    # Climb up from the package dir until we find pyproject.toml — that
    # marks the repo root in editable / source-checkout mode.
    cur = pkg_root
    for _ in range(6):  # pragma: no branch — bounded climb
        cur = cur.parent
        if (cur / "pyproject.toml").is_file():
            candidate = cur / "scripts" / "hooks"
            if candidate.is_dir():
                return candidate
            break

    # Final fallback: return the packaged path even though we know it does
    # not exist, so the caller can include a real path in its error message.
    return packaged


__all__ = [
    "HOOK_FILES",
    "HookBundleNotFoundError",
    "HookInstallPlan",
    "HookInstaller",
    "bundled_hook_dir",
]
