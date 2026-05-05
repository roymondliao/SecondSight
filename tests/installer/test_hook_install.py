"""Death + unit tests for HookInstaller (GUR-98 / P1-11).

Death tests:
  DT-1  Missing source bundle -> raises HookBundleNotFoundError, NOT a
        silent empty install. Without this, broken wheels would land
        zero hook scripts and Claude Code would silently never fire.
  DT-2  Idempotent: second install with identical content does NOT
        re-write files. Asserted by checking mtime equality.
  DT-3  Files land with mode 0o755 even when the source bundle's bytes
        were checked in with mode 0o644 (common in clean source trees).
  DT-4  Atomic copy: a simulated failure mid-copy leaves the destination
        either absent or at the OLD content; never half-written. We also
        assert no .tmp_*.sh leak.
  DT-5  bundled_hook_dir() finds the source-tree scripts/hooks/ in
        editable mode (proves the auto-discovery climb-up).
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from secondsight.installer.hook_install import (
    HOOK_FILES,
    HookBundleNotFoundError,
    HookInstaller,
    bundled_hook_dir,
)


def _make_bundle(tmp_path: Path) -> Path:
    src = tmp_path / "bundle"
    src.mkdir()
    for name in HOOK_FILES:
        (src / name).write_text(
            f"#!/usr/bin/env bash\n# {name}\nexit 0\n",
            encoding="utf-8",
        )
        # Source mode mimics a freshly-checked-out file.
        os.chmod(src / name, 0o644)
    return src


# ---------------------------------------------------------------------------
# DT-1: missing source bundle
# ---------------------------------------------------------------------------


def test_death_missing_bundle_raises(tmp_path: Path) -> None:
    nonexistent = tmp_path / "nope" / "hooks"
    installer = HookInstaller(source_dir=nonexistent)
    with pytest.raises(HookBundleNotFoundError):
        installer.install(target_dir=tmp_path / "claude" / "hooks")


# ---------------------------------------------------------------------------
# DT-2: install is idempotent (mtime preserved on second run)
# ---------------------------------------------------------------------------


def test_death_install_idempotent_skips_identical(tmp_path: Path) -> None:
    src = _make_bundle(tmp_path)
    target = tmp_path / "claude" / "hooks"

    installer = HookInstaller(source_dir=src)
    plan_first = installer.install(target)
    assert sorted(plan_first.copied) == sorted(HOOK_FILES)

    # Capture mtimes after first install.
    mtimes_after_first = {
        name: (target / name).stat().st_mtime_ns for name in HOOK_FILES
    }
    # Sleep a bit so a regression that re-wrote files would change mtime.
    time.sleep(0.05)

    plan_second = installer.install(target)
    assert plan_second.copied == [], (
        f"second install must skip everything, got copied={plan_second.copied!r}"
    )
    assert sorted(plan_second.skipped_identical) == sorted(HOOK_FILES)

    mtimes_after_second = {
        name: (target / name).stat().st_mtime_ns for name in HOOK_FILES
    }
    assert mtimes_after_first == mtimes_after_second, (
        "second install must not touch unchanged files (mtime should match)"
    )


# ---------------------------------------------------------------------------
# DT-3: install marks scripts executable
# ---------------------------------------------------------------------------


def test_death_install_marks_scripts_executable(tmp_path: Path) -> None:
    src = _make_bundle(tmp_path)
    target = tmp_path / "claude" / "hooks"
    HookInstaller(source_dir=src).install(target)
    for name in HOOK_FILES:
        mode = (target / name).stat().st_mode & 0o777
        assert mode == 0o755, (
            f"{name} must be installed with mode 0o755, got {oct(mode)}"
        )


# ---------------------------------------------------------------------------
# DT-4: atomic copy — failure leaves no half-written file
# ---------------------------------------------------------------------------


def test_death_atomic_copy_no_partial_on_failure(tmp_path: Path) -> None:
    src = _make_bundle(tmp_path)
    target = tmp_path / "claude" / "hooks"

    # Pre-create the destination dir with one OLD version of pre-tool-use.sh.
    target.mkdir(parents=True, exist_ok=True)
    (target / "pre-tool-use.sh").write_text("OLD\n", encoding="utf-8")
    os.chmod(target / "pre-tool-use.sh", 0o755)

    def replace_after_first(src_path: str, dst_path: str) -> None:
        # Fail on the first replace (which targets _lib.sh — first in HOOK_FILES).
        raise OSError("simulated atomic-rename failure")

    with patch(
        "secondsight.installer.hook_install.os.replace",
        side_effect=replace_after_first,
    ):
        with pytest.raises(OSError, match="simulated atomic-rename failure"):
            HookInstaller(source_dir=src).install(target)

    # OLD content remains; the destination was never half-written.
    assert (target / "pre-tool-use.sh").read_text(encoding="utf-8") == "OLD\n"
    # _lib.sh was never created (failure happened before replace).
    assert not (target / "_lib.sh").exists()
    # No tmp leak.
    assert list(target.glob(".tmp_*")) == [], "tmp files leaked"


# ---------------------------------------------------------------------------
# DT-5: bundled_hook_dir() locates the source-tree scripts/hooks/
# ---------------------------------------------------------------------------


def test_death_bundled_hook_dir_finds_source_tree() -> None:
    found = bundled_hook_dir()
    assert (found / "_lib.sh").is_file(), (
        f"bundled_hook_dir() must locate _lib.sh in dev / editable mode; "
        f"got {found}"
    )


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------


def test_dry_run_does_not_write(tmp_path: Path) -> None:
    src = _make_bundle(tmp_path)
    target = tmp_path / "claude" / "hooks"
    plan = HookInstaller(source_dir=src).install(target, dry_run=True)
    assert sorted(plan.copied) == sorted(HOOK_FILES)
    assert not target.exists(), "dry_run must not create the target dir"


def test_install_overwrites_when_content_differs(tmp_path: Path) -> None:
    src = _make_bundle(tmp_path)
    target = tmp_path / "claude" / "hooks"
    target.mkdir(parents=True)
    (target / "_lib.sh").write_text("STALE\n", encoding="utf-8")
    plan = HookInstaller(source_dir=src).install(target)
    assert "_lib.sh" in plan.copied
    assert (target / "_lib.sh").read_text(encoding="utf-8").startswith("#!/usr/bin/env bash")
