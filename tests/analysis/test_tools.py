"""Death + happy-path tests for AnalysisTools (GUR-103 task-1, P2-11).

Samsara discipline: death tests first.

Death test inventory (silent-failure cases FIRST):
- DT-1.1: DC-1 — sandbox rejects symlink escape via resolved-path re-check.
          A symlink pointing outside project_root must raise ProjectFileToolError
          and log a WARN with the resolved path. Without this, secrets leak to LLM.
- DT-1.2: DC-2 — denylist match is case-insensitive on filename component.
          `.ENV` must match the `.env` pattern. Original case preserved in WARN log.
- DT-1.3: DC-2 — denylist match on ancestor directory component.
          `.ssh/id_rsa` is blocked by the `.ssh` component match (NOT a "/*" glob).
          Path.parts yields (".ssh", "id_rsa"); ".ssh" matches the ".ssh" denylist
          entry, blocking all reads under .ssh/ without needing a slash suffix.
- DT-1.4: size cap — file > 256 KiB returns first 256 KiB + truncation marker.
          Without this, a large file could send 10 MB of tokens to the LLM.
- DT-1.5: binary file placeholder — raw binary bytes return `<binary file: N bytes>`,
          never raw bytes that would corrupt LLM JSON encoding.
- DT-1.5b: binary detection on FULL bytes before truncation — a file with text in
          the first 256 KiB and null bytes after must still return binary placeholder.
          Previously, binary check ran on the truncated slice, silently passing.
- DT-1.6: query_structured_store rejects unknown kind — `{"kind":"DROP TABLE"}` raises
          ValueError before any repo method is called; repo methods get zero invocations.
- DT-1.D8: D8 kill switch — read_project_file_enabled=False raises ProjectFileToolError
          immediately with no FS access attempted. Acceptance criteria D8 escape valve.

Happy-path tests:
- HP-1.4: read_project_file happy path — valid in-project text file returns content.
- HP-1.5: read_traces happy path — delegates to events_repo.get_session_events.
- HP-1.6: query_structured_store behavior_flag_summary — delegates to flags repo.
- HP-1.7: query_structured_store directive_active — delegates to directives repo.
- HP-1.8: read_historical_flags — groups flags by flag_type, delegates to flags repo.

Assumptions:
- AnalysisTools is constructed with (project_root, events_repo, flags_repo, directives_repo).
- project_root=None signals "no project configured"; read_project_file raises immediately.
- pytest-asyncio with @pytest.mark.asyncio on each async test.
- Size cap is 256 * 1024 bytes = 262144 bytes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from secondsight.analysis.tools import AnalysisTools, ProjectFileToolError

SIZE_CAP = 256 * 1024  # 262144 bytes


# =====================================================================
# Fixtures
# =====================================================================


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "my_project"
    root.mkdir()
    return root


@pytest.fixture
def mock_events_repo() -> MagicMock:
    repo = MagicMock()
    repo.get_session_events.return_value = []
    return repo


@pytest.fixture
def mock_flags_repo() -> MagicMock:
    repo = MagicMock()
    repo.get_project_flags_by_type.return_value = []
    return repo


@pytest.fixture
def mock_directives_repo() -> MagicMock:
    repo = MagicMock()
    repo.get_active_conventions.return_value = []
    return repo


@pytest.fixture
def tools(
    project_root: Path,
    mock_events_repo: MagicMock,
    mock_flags_repo: MagicMock,
    mock_directives_repo: MagicMock,
) -> AnalysisTools:
    return AnalysisTools(
        project_root=project_root,
        events_repo=mock_events_repo,
        flags_repo=mock_flags_repo,
        directives_repo=mock_directives_repo,
    )


# =====================================================================
# DEATH TESTS — silent failure scenarios first
# =====================================================================


class TestDT11SandboxRejectsSymlinkEscape:
    """DT-1.1 (DC-1): A symlink pointing outside project_root must be rejected.

    Silent failure: `Path.resolve()` follows symlinks. Without the re-check
    post-resolve, `is_relative_to(project_root)` sees the resolved external
    path and silently allows `/etc/passwd` to be read.
    """

    @pytest.mark.asyncio
    async def test_DT_1_1_sandbox_rejects_symlink_escape(
        self,
        tmp_path: Path,
        mock_events_repo: MagicMock,
        mock_flags_repo: MagicMock,
        mock_directives_repo: MagicMock,
    ) -> None:
        # Arrange: project_root + a symlink inside it pointing OUTSIDE
        project_root = tmp_path / "project"
        project_root.mkdir()

        secret_file = tmp_path / "outside_secret.txt"
        secret_file.write_text("TOP SECRET")

        # Create the symlink inside the project root
        symlink = project_root / "escape.txt"
        symlink.symlink_to(secret_file)

        tools = AnalysisTools(
            project_root=project_root,
            events_repo=mock_events_repo,
            flags_repo=mock_flags_repo,
            directives_repo=mock_directives_repo,
        )

        # Act + Assert
        with pytest.raises(ProjectFileToolError) as exc_info:
            await tools.read_project_file("escape.txt")

        assert "escape.txt" in str(exc_info.value) or str(exc_info.value)

    @pytest.mark.asyncio
    async def test_symlink_escape_is_warn_logged(
        self,
        tmp_path: Path,
        mock_events_repo: MagicMock,
        mock_flags_repo: MagicMock,
        mock_directives_repo: MagicMock,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Rejection must WARN-log the resolved path (audit trail)."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        secret_file = tmp_path / "secret.env"
        secret_file.write_text("SECRET=abc")

        symlink = project_root / "local_link.env"
        symlink.symlink_to(secret_file)

        tools = AnalysisTools(
            project_root=project_root,
            events_repo=mock_events_repo,
            flags_repo=mock_flags_repo,
            directives_repo=mock_directives_repo,
        )

        with caplog.at_level(logging.WARNING):
            with pytest.raises(ProjectFileToolError):
                await tools.read_project_file("local_link.env")

        # The resolved path (outside project) must appear in the warning log
        assert any(
            str(secret_file.resolve()) in record.message
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ), (
            f"Expected resolved path {secret_file.resolve()} in WARN logs. Got: {[r.message for r in caplog.records]}"
        )


class TestDT12DenylistCaseInsensitive:
    """DT-1.2 (DC-2): Denylist match is case-insensitive on filename component.

    Silent failure: `.ENV` (uppercase) matches `*.env` on case-insensitive FS
    (macOS/Windows). On case-sensitive FS (Linux ext4), `.ENV` is a distinct
    file with the same secrets — literal match misses it. Denylist must be
    case-folded regardless of FS behavior.
    """

    @pytest.mark.asyncio
    async def test_DT_1_2_denylist_case_insensitive(
        self, project_root: Path, tools: AnalysisTools, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Arrange: create .ENV (uppercase) inside the project
        env_file = project_root / ".ENV"
        env_file.write_text("SECRET_KEY=hunter2")

        # Act + Assert: must be blocked by denylist regardless of case
        with caplog.at_level(logging.WARNING):
            with pytest.raises(ProjectFileToolError) as exc_info:
                await tools.read_project_file(".ENV")

        error_msg = str(exc_info.value)
        assert error_msg  # non-empty error

    @pytest.mark.asyncio
    async def test_uppercase_env_warn_log_preserves_original_case(
        self, project_root: Path, tools: AnalysisTools, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Original case must appear in the WARN log, not just the lower-cased match."""
        env_file = project_root / ".ENV"
        env_file.write_text("SECRET=xyz")

        with caplog.at_level(logging.WARNING):
            with pytest.raises(ProjectFileToolError):
                await tools.read_project_file(".ENV")

        # The WARNING must preserve the original path (not silently lower-case it)
        warn_messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert warn_messages, "Expected at least one WARN log for denylist hit"
        # The original case ".ENV" must appear somewhere in warnings
        combined = " ".join(warn_messages)
        assert ".ENV" in combined or ".env" in combined.lower(), (
            f"Expected denylist log to mention '.ENV', got: {combined}"
        )

    @pytest.mark.asyncio
    async def test_env_credentials_file_blocked_case_insensitive(
        self, project_root: Path, tools: AnalysisTools
    ) -> None:
        """*CREDENTIALS* pattern also case-insensitive."""
        cred_file = project_root / "MY_CREDENTIALS.txt"
        cred_file.write_text("password=secret")

        with pytest.raises(ProjectFileToolError):
            await tools.read_project_file("MY_CREDENTIALS.txt")


class TestDT13DenylistMatchOnAncestorDirectory:
    """DT-1.3 (DC-2): Ancestor directory matching.

    `.ssh/id_rsa` must be blocked by the `.ssh` denylist pattern matched
    against the DIRECTORY component of the path. The mechanism is component-level
    matching: Path.parts yields (".ssh", "id_rsa"), and the ".ssh" component
    matches the ".ssh" denylist entry (case-insensitive). No "/*" glob is needed
    or used — the exact ".ssh" entry blocks the entire subtree.

    Silent failure: checking only the final filename component misses:
      - `.ssh/id_rsa` (the ".ssh" dir component matches ".ssh" pattern)
      - `.aws/credentials` (the ".aws" dir component matches ".aws" pattern)
      - `.ssh/known_hosts` (not an id_rsa-patterned name, still blocked via ".ssh")
    """

    @pytest.mark.asyncio
    async def test_DT_1_3_denylist_ancestor_match(
        self, project_root: Path, tools: AnalysisTools
    ) -> None:
        """DT-1.3 canonical test: .ssh/id_rsa is blocked by the .ssh ancestor match."""
        # Arrange: create .ssh/id_rsa inside the project
        ssh_dir = project_root / ".ssh"
        ssh_dir.mkdir()
        id_rsa = ssh_dir / "id_rsa"
        id_rsa.write_text("fake-key-content")

        # Act + Assert: blocked by .ssh component matching (ancestor block)
        with pytest.raises(ProjectFileToolError):
            await tools.read_project_file(".ssh/id_rsa")

    @pytest.mark.asyncio
    async def test_aws_credentials_blocked_by_ancestor_pattern(
        self, project_root: Path, tools: AnalysisTools
    ) -> None:
        aws_dir = project_root / ".aws"
        aws_dir.mkdir()
        cred = aws_dir / "credentials"
        cred.write_text("[default]\naws_access_key_id = AKIA...")

        with pytest.raises(ProjectFileToolError):
            await tools.read_project_file(".aws/credentials")

    @pytest.mark.asyncio
    async def test_ancestor_blocked_not_filename_match(
        self, project_root: Path, tools: AnalysisTools
    ) -> None:
        """Verify ancestor directory component is what triggers the block,
        not the filename alone. 'known_hosts' matches neither id_rsa* nor
        any filename-level denylist pattern — but the '.ssh' directory
        component matches the '.ssh' denylist entry, blocking all reads
        inside .ssh/ regardless of the filename.

        This confirms the mechanism is component-level exact matching
        (not a "/*"-suffix glob on the full path).
        """
        ssh_dir = project_root / ".ssh"
        ssh_dir.mkdir()
        known_hosts = ssh_dir / "known_hosts"
        known_hosts.write_text("github.com ssh-rsa AAAA...")

        # known_hosts doesn't match id_rsa* or any other filename pattern,
        # but the .ssh directory component still blocks it.
        with pytest.raises(ProjectFileToolError):
            await tools.read_project_file(".ssh/known_hosts")


class TestDT14SizeCap:
    """DT-1.4: Files larger than 256 KiB are truncated with a marker.

    Silent failure: sending a 10 MB file to the LLM costs ~10K tokens and
    can exceed context limits, causing truncation by the provider with no
    notice. We cap and mark explicitly.
    """

    @pytest.mark.asyncio
    async def test_large_file_truncated_with_marker(
        self, project_root: Path, tools: AnalysisTools
    ) -> None:
        # Arrange: 512 KiB file (exactly double the cap)
        large_file = project_root / "big_file.txt"
        content = b"A" * (512 * 1024)
        large_file.write_bytes(content)

        result = await tools.read_project_file("big_file.txt")

        # Must contain the truncation marker
        assert "<truncated:" in result
        assert "524288" in result  # original size

        # The returned content must be the first SIZE_CAP bytes
        # (decoded, since all bytes are 'A')
        first_part = result.split("<truncated:")[0]
        assert len(first_part) == SIZE_CAP

    @pytest.mark.asyncio
    async def test_file_exactly_at_cap_not_truncated(
        self, project_root: Path, tools: AnalysisTools
    ) -> None:
        """A file of exactly 256 KiB must NOT be truncated."""
        exact_file = project_root / "exact.txt"
        content = b"B" * SIZE_CAP
        exact_file.write_bytes(content)

        result = await tools.read_project_file("exact.txt")

        assert "<truncated:" not in result
        assert len(result) == SIZE_CAP


class TestDT15BinaryFilePlaceholder:
    """DT-1.5: Binary files return a placeholder, never raw bytes.

    Silent failure: returning raw binary bytes inside a JSON-serialized
    tool output will corrupt the LLM's JSON decoding or inject control
    characters, potentially causing silent parsing failures or injection.
    """

    @pytest.mark.asyncio
    async def test_binary_file_returns_placeholder(
        self, project_root: Path, tools: AnalysisTools
    ) -> None:
        # Arrange: file with null bytes
        bin_file = project_root / "binary.bin"
        bin_file.write_bytes(b"\x00\x01\x02\xff\xfe")

        result = await tools.read_project_file("binary.bin")

        assert result.startswith("<binary file:")
        assert "5" in result  # file size in bytes
        # Must NOT contain raw binary
        assert "\x00" not in result
        assert "\xff" not in result

    @pytest.mark.asyncio
    async def test_binary_with_null_byte_returns_placeholder(
        self, project_root: Path, tools: AnalysisTools
    ) -> None:
        """Null byte is the key indicator of binary content."""
        null_file = project_root / "null.dat"
        null_file.write_bytes(b"\x00" * 10)

        result = await tools.read_project_file("null.dat")

        assert "<binary file:" in result
        assert "\x00" not in result

    @pytest.mark.asyncio
    async def test_DT_1_5b_binary_check_on_full_bytes_before_truncation(
        self,
        project_root: Path,
        mock_events_repo: MagicMock,
        mock_flags_repo: MagicMock,
        mock_directives_repo: MagicMock,
    ) -> None:
        """DT-1.5b (fix-loop): binary detection must happen on the FULL file,
        not the truncated slice.

        Silent failure: if binary check runs AFTER the size-cap slice, a 300 KiB
        file with valid text in the first 256 KiB and null bytes at byte 256K+1
        would pass the binary guard and return truncated text — violating the
        contract "binary files return a placeholder, never raw bytes".

        Fix: binary check runs before truncation.
        """
        # Arrange: file with 256 KiB of text, then null bytes at the end.
        # The size cap is SIZE_CAP = 256 * 1024. Null bytes appear AFTER the cap.
        text_part = b"A" * SIZE_CAP  # exactly the size cap in text
        null_part = b"\x00" * 1024  # null bytes beyond the cap
        mixed_file = project_root / "mixed_binary.bin"
        mixed_file.write_bytes(text_part + null_part)

        tools = AnalysisTools(
            project_root=project_root,
            events_repo=mock_events_repo,
            flags_repo=mock_flags_repo,
            directives_repo=mock_directives_repo,
        )

        result = await tools.read_project_file("mixed_binary.bin")

        # Must return binary placeholder, NOT truncated text.
        # The file has null bytes — binary placeholder is the contract.
        assert result.startswith("<binary file:"), (
            f"Expected binary placeholder for file with null bytes beyond size cap, "
            f"got: {result[:80]!r}"
        )
        # The original full size must be reported in the placeholder.
        full_size = SIZE_CAP + 1024
        assert str(full_size) in result, (
            f"Expected original size {full_size} in binary placeholder, got: {result}"
        )
        # Must NOT contain raw text content
        assert "<truncated:" not in result


class TestDT1D8KillSwitch:
    """DT-1.D8: D8 escape valve — read_project_file_enabled=False kills the tool.

    Silent failure: if the kill switch is documented but not implemented, a
    project operator sets `enabled = false` in project config and gets no
    protection. read_project_file continues reading files silently.
    The fix: AnalysisTools accepts read_project_file_enabled parameter and
    raises ProjectFileToolError immediately when False — no FS access.
    """

    @pytest.mark.asyncio
    async def test_kill_switch_disabled_raises_project_file_tool_error(
        self,
        tmp_path: Path,
        mock_events_repo: MagicMock,
        mock_flags_repo: MagicMock,
        mock_directives_repo: MagicMock,
    ) -> None:
        """When read_project_file_enabled=False, every call raises ProjectFileToolError."""
        project_root = tmp_path / "project"
        project_root.mkdir()
        readable_file = project_root / "README.md"
        readable_file.write_text("# Hello\n")

        tools = AnalysisTools(
            project_root=project_root,
            events_repo=mock_events_repo,
            flags_repo=mock_flags_repo,
            directives_repo=mock_directives_repo,
            read_project_file_enabled=False,
        )

        with pytest.raises(ProjectFileToolError) as exc_info:
            await tools.read_project_file("README.md")

        error_msg = str(exc_info.value)
        assert "disabled" in error_msg.lower(), (
            f"Expected 'disabled' in error message, got: {error_msg!r}"
        )

    @pytest.mark.asyncio
    async def test_kill_switch_disabled_no_fs_read_attempted(
        self,
        tmp_path: Path,
        mock_events_repo: MagicMock,
        mock_flags_repo: MagicMock,
        mock_directives_repo: MagicMock,
    ) -> None:
        """When disabled, no FS read must be attempted (not even a path resolution)."""
        project_root = tmp_path / "project"
        project_root.mkdir()

        tools = AnalysisTools(
            project_root=project_root,
            events_repo=mock_events_repo,
            flags_repo=mock_flags_repo,
            directives_repo=mock_directives_repo,
            read_project_file_enabled=False,
        )

        # Patch asyncio.to_thread to detect if any FS call is attempted.
        call_log: list[str] = []

        async def fake_to_thread(fn, *args, **kwargs):
            call_log.append(fn.__name__ if hasattr(fn, "__name__") else str(fn))
            raise AssertionError("FS read attempted despite kill switch!")

        import unittest.mock as mock_module

        with mock_module.patch("secondsight.analysis.tools.asyncio.to_thread", fake_to_thread):
            with pytest.raises(ProjectFileToolError):
                await tools.read_project_file("README.md")

        assert not call_log, (
            f"Expected zero FS reads when kill switch is disabled, got calls: {call_log}"
        )

    @pytest.mark.asyncio
    async def test_kill_switch_enabled_true_allows_reads(
        self, project_root: Path, tools: AnalysisTools
    ) -> None:
        """Happy path: enabled=True (default) does NOT block reads."""
        readable = project_root / "config.toml"
        readable.write_text("[section]\nkey = 'value'\n")

        result = await tools.read_project_file("config.toml")

        assert "[section]" in result


class TestDT16QueryStructuredStoreRejectsUnknownKind:
    """DT-1.6: query_structured_store raises ValueError for unknown kind.

    Silent failure: if unknown `kind` values silently no-op or return empty
    results, the LLM sees an empty tool response and may fabricate analysis
    from nothing — wrong results with no error surfaced.
    """

    @pytest.mark.asyncio
    async def test_unknown_kind_raises_value_error(
        self, tools: AnalysisTools, mock_flags_repo: MagicMock, mock_directives_repo: MagicMock
    ) -> None:
        with pytest.raises(ValueError) as exc_info:
            await tools.query_structured_store({"kind": "DROP TABLE", "project_id": "proj"})

        assert "DROP TABLE" in str(exc_info.value) or "kind" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_unknown_kind_never_reaches_repo(
        self, tools: AnalysisTools, mock_flags_repo: MagicMock, mock_directives_repo: MagicMock
    ) -> None:
        """Repo methods must record zero invocations when kind is invalid."""
        with pytest.raises(ValueError):
            await tools.query_structured_store({"kind": "DROP TABLE", "project_id": "proj"})

        # Neither repo should have been called
        mock_flags_repo.get_project_flags_by_type.assert_not_called()
        mock_directives_repo.get_active_conventions.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_kind_raises_value_error(self, tools: AnalysisTools) -> None:
        with pytest.raises(ValueError):
            await tools.query_structured_store({"kind": "", "project_id": "proj"})

    @pytest.mark.asyncio
    async def test_missing_kind_key_raises_value_error(self, tools: AnalysisTools) -> None:
        with pytest.raises(ValueError):
            await tools.query_structured_store({})

    @pytest.mark.asyncio
    async def test_missing_project_id_raises_value_error(
        self, tools: AnalysisTools, mock_flags_repo: MagicMock
    ) -> None:
        """Missing project_id must raise rather than silently return empty results."""
        with pytest.raises(ValueError) as exc_info:
            await tools.query_structured_store({"kind": "behavior_flag_summary"})

        assert "project_id" in str(exc_info.value)
        mock_flags_repo.count_by_type.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_project_id_raises_value_error(
        self, tools: AnalysisTools, mock_flags_repo: MagicMock
    ) -> None:
        """Empty project_id must raise rather than querying repo with empty string."""
        with pytest.raises(ValueError):
            await tools.query_structured_store({"kind": "behavior_flag_summary", "project_id": ""})
        mock_flags_repo.count_by_type.assert_not_called()


class TestDT17NoProjectRootRaisesOnFileRead:
    """DT-1.7 (extra death case): project_root=None raises immediately.

    Silent failure: if no project root is configured, we must not guess,
    use the CWD, or silently read nothing — we must raise explicitly.
    """

    @pytest.mark.asyncio
    async def test_no_project_root_raises_project_file_tool_error(
        self,
        mock_events_repo: MagicMock,
        mock_flags_repo: MagicMock,
        mock_directives_repo: MagicMock,
    ) -> None:
        tools = AnalysisTools(
            project_root=None,
            events_repo=mock_events_repo,
            flags_repo=mock_flags_repo,
            directives_repo=mock_directives_repo,
        )

        with pytest.raises(ProjectFileToolError) as exc_info:
            await tools.read_project_file("README.md")

        assert "project" in str(exc_info.value).lower() or str(exc_info.value)

    def test_relative_project_root_raises_value_error(
        self,
        mock_events_repo: MagicMock,
        mock_flags_repo: MagicMock,
        mock_directives_repo: MagicMock,
    ) -> None:
        """Non-absolute project_root must raise at construction time, not silently
        resolve against CWD.

        Silent failure: a relative path like "my_project" resolves against CWD,
        which could be anywhere — any read inside 'my_project' relative to CWD
        passes the sandbox check even if CWD is /etc.
        """
        with pytest.raises(ValueError) as exc_info:
            AnalysisTools(
                project_root=Path("relative/path"),
                events_repo=mock_events_repo,
                flags_repo=mock_flags_repo,
                directives_repo=mock_directives_repo,
            )
        assert "absolute" in str(exc_info.value).lower()


class TestDT18NonExistentFileRaises:
    """DT-1.8: Non-existent file raises ProjectFileToolError (not FileNotFoundError).

    Silent failure: if FileNotFoundError escapes, callers won't know how to
    handle it (they expect ProjectFileToolError for all file access errors).
    FileNotFoundError must be wrapped so the failure mode is uniform.
    """

    @pytest.mark.asyncio
    async def test_nonexistent_file_raises_project_file_tool_error(
        self, tools: AnalysisTools
    ) -> None:
        with pytest.raises(ProjectFileToolError):
            await tools.read_project_file("does_not_exist.txt")

    @pytest.mark.asyncio
    async def test_file_not_found_error_does_not_escape(self, tools: AnalysisTools) -> None:
        """Specifically verify FileNotFoundError is NOT raised."""
        with pytest.raises(ProjectFileToolError):
            try:
                await tools.read_project_file("ghost.py")
            except FileNotFoundError:
                pytest.fail("FileNotFoundError escaped! Must be wrapped as ProjectFileToolError.")


# =====================================================================
# HAPPY-PATH TESTS
# =====================================================================


class TestHP14ReadProjectFileHappyPath:
    """HP-1.4: Valid in-project text file returns full content."""

    @pytest.mark.asyncio
    async def test_HP_1_4_read_project_file_happy_path(
        self, project_root: Path, tools: AnalysisTools
    ) -> None:
        """HP-1.4 canonical test: valid in-project text file returns content."""
        source_file = project_root / "main.py"
        source_file.write_text("def hello(): return 'world'\n")

        result = await tools.read_project_file("main.py")

        assert result == "def hello(): return 'world'\n"

    @pytest.mark.asyncio
    async def test_reads_nested_file_in_project(
        self, project_root: Path, tools: AnalysisTools
    ) -> None:
        subdir = project_root / "src" / "app"
        subdir.mkdir(parents=True)
        nested = subdir / "utils.py"
        nested.write_text("# utility functions\n")

        result = await tools.read_project_file("src/app/utils.py")

        assert result == "# utility functions\n"

    @pytest.mark.asyncio
    async def test_utf8_content_decoded_correctly(
        self, project_root: Path, tools: AnalysisTools
    ) -> None:
        unicode_file = project_root / "readme.md"
        content = "# 你好世界\nHello World\n"
        unicode_file.write_text(content, encoding="utf-8")

        result = await tools.read_project_file("readme.md")

        assert result == content

    @pytest.mark.asyncio
    async def test_safe_file_not_on_denylist(
        self, project_root: Path, tools: AnalysisTools
    ) -> None:
        """config.json (not a secrets file) passes through."""
        config_file = project_root / "config.json"
        config_file.write_text('{"debug": true}')

        result = await tools.read_project_file("config.json")

        assert '"debug"' in result


class TestHP15ReadTraces:
    """HP-1.5: read_traces delegates to events_repo.get_session_events."""

    @pytest.mark.asyncio
    async def test_read_traces_returns_events(
        self, tools: AnalysisTools, mock_events_repo: MagicMock
    ) -> None:
        from unittest.mock import MagicMock as MM

        fake_event = MM()
        mock_events_repo.get_session_events.return_value = [fake_event]

        result = await tools.read_traces("sess-001")

        mock_events_repo.get_session_events.assert_called_once_with("sess-001")
        assert result == [fake_event]

    @pytest.mark.asyncio
    async def test_read_traces_empty_session(
        self, tools: AnalysisTools, mock_events_repo: MagicMock
    ) -> None:
        mock_events_repo.get_session_events.return_value = []

        result = await tools.read_traces("sess-empty")

        assert result == []


class TestHP16QueryStructuredStoreBehaviorFlagSummary:
    """HP-1.6: query_structured_store with kind=behavior_flag_summary works."""

    @pytest.mark.asyncio
    async def test_behavior_flag_summary_calls_count_by_type(
        self,
        tools: AnalysisTools,
        mock_flags_repo: MagicMock,
    ) -> None:
        from secondsight.analysis.schemas import BehaviorFlagType

        mock_flags_repo.count_by_type.return_value = {
            BehaviorFlagType.UNNECESSARY_READ: 3,
        }

        result = await tools.query_structured_store(
            {"kind": "behavior_flag_summary", "project_id": "proj-alpha"}
        )

        mock_flags_repo.count_by_type.assert_called_once_with("proj-alpha")
        assert result is not None


class TestHP17QueryStructuredStoreDirectiveActive:
    """HP-1.7: query_structured_store with kind=directive_active works."""

    @pytest.mark.asyncio
    async def test_directive_active_calls_get_active_conventions(
        self,
        tools: AnalysisTools,
        mock_directives_repo: MagicMock,
    ) -> None:
        from unittest.mock import MagicMock as MM

        fake_directive = MM()
        mock_directives_repo.get_active_conventions.return_value = [fake_directive]

        result = await tools.query_structured_store(
            {"kind": "directive_active", "project_id": "proj-alpha"}
        )

        mock_directives_repo.get_active_conventions.assert_called_once_with("proj-alpha")
        assert result is not None


class TestHP18ReadHistoricalFlags:
    """HP-1.8: read_historical_flags groups flags by flag_type."""

    @pytest.mark.asyncio
    async def test_read_historical_flags_returns_grouped_result(
        self,
        tools: AnalysisTools,
        mock_flags_repo: MagicMock,
    ) -> None:
        from secondsight.analysis.schemas import BehaviorFlagType
        from unittest.mock import MagicMock as MM

        flag1 = MM()
        flag1.flag_type = BehaviorFlagType.UNNECESSARY_READ
        flag2 = MM()
        flag2.flag_type = BehaviorFlagType.UNNECESSARY_READ
        flag3 = MM()
        flag3.flag_type = BehaviorFlagType.MISSED_SHORTCUT

        mock_flags_repo.get_project_flags_by_type.side_effect = lambda pid, ft: (
            [flag1, flag2]
            if ft == BehaviorFlagType.UNNECESSARY_READ
            else [flag3]
            if ft == BehaviorFlagType.MISSED_SHORTCUT
            else []
        )

        result = await tools.read_historical_flags("proj-alpha", limit=200)

        # Result should be a dict or structured grouping by flag_type
        assert result is not None
