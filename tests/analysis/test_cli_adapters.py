"""Unit tests for CLI adapter command builders (Task 4).

Each adapter has these public functions:
    build_command(model, prompt, project_root) -> list[str]         (claude_code)
    build_command(model, prompt, project_root) -> tuple[list, str]  (codex)
    extract_result(raw_stdout) -> str                               (claude_code only)

NOTE: build_env() was removed from adapters in Task 4 revision.
Env filtering is now centralized in CLIAnalysisDispatcher._filter_env().
Tests for SECONDSIGHT_* env filtering live in test_cli_dispatcher.py::TestEnvIsolation.

Tests cover:
- Command structure (binary name, flags)
- Model flag inclusion/omission
- Claude envelope extraction
"""

from __future__ import annotations

import json
from pathlib import Path


from secondsight.analysis.cli_adapters.claude_code import (
    build_command as claude_build_command,
    extract_result,
)
from secondsight.analysis.cli_adapters.codex import (
    build_command as codex_build_command,
)


# ===========================================================================
# Claude Code adapter
# ===========================================================================


class TestClaudeCodeBuildCommand:
    def test_command_starts_with_claude(self, tmp_path: Path) -> None:
        cmd = claude_build_command(model=None, prompt="analyze this", project_root=tmp_path)
        assert cmd[0] == "claude"

    def test_command_includes_print_flag(self, tmp_path: Path) -> None:
        cmd = claude_build_command(model=None, prompt="analyze this", project_root=tmp_path)
        assert "--print" in cmd

    def test_command_includes_json_output_format(self, tmp_path: Path) -> None:
        cmd = claude_build_command(model=None, prompt="analyze this", project_root=tmp_path)
        assert "--output-format" in cmd
        idx = cmd.index("--output-format")
        assert cmd[idx + 1] == "json"

    def test_command_includes_no_session_persistence(self, tmp_path: Path) -> None:
        cmd = claude_build_command(model=None, prompt="analyze this", project_root=tmp_path)
        assert "--no-session-persistence" in cmd

    def test_model_flag_included_when_provided(self, tmp_path: Path) -> None:
        cmd = claude_build_command(
            model="claude-3-5-haiku-20241022",
            prompt="analyze this",
            project_root=tmp_path,
        )
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "claude-3-5-haiku-20241022"

    def test_model_flag_omitted_when_none(self, tmp_path: Path) -> None:
        cmd = claude_build_command(model=None, prompt="analyze this", project_root=tmp_path)
        assert "--model" not in cmd

    def test_prompt_is_last_positional_arg(self, tmp_path: Path) -> None:
        prompt_text = "analyze this session carefully"
        cmd = claude_build_command(model=None, prompt=prompt_text, project_root=tmp_path)
        assert cmd[-1] == prompt_text

    def test_command_is_list_of_strings(self, tmp_path: Path) -> None:
        cmd = claude_build_command(model=None, prompt="test", project_root=tmp_path)
        assert isinstance(cmd, list)
        assert all(isinstance(part, str) for part in cmd)


class TestClaudeCodeExtractResult:
    """Tests for the JSON envelope extraction function."""

    def test_extracts_result_from_claude_envelope(self) -> None:
        envelope = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "result": '{"status": "success"}',
                "duration_ms": 1234,
            }
        )
        extracted = extract_result(envelope)
        assert extracted == '{"status": "success"}'

    def test_returns_raw_when_no_envelope(self) -> None:
        raw = '{"status": "success", "direct": true}'
        extracted = extract_result(raw)
        assert extracted == raw

    def test_returns_raw_when_invalid_json(self) -> None:
        raw = "not json at all"
        extracted = extract_result(raw)
        assert extracted == raw

    def test_returns_raw_when_envelope_missing_result_key(self) -> None:
        envelope = json.dumps({"type": "result", "no_result_key": "value"})
        extracted = extract_result(envelope)
        assert extracted == envelope


# ===========================================================================
# Codex adapter
# ===========================================================================


class TestCodexBuildCommand:
    def test_command_starts_with_codex(self, tmp_path: Path) -> None:
        cmd, _ = codex_build_command(model=None, prompt="analyze this", project_root=tmp_path)
        assert cmd[0] == "codex"

    def test_command_includes_exec_subcommand(self, tmp_path: Path) -> None:
        cmd, _ = codex_build_command(model=None, prompt="analyze this", project_root=tmp_path)
        assert "exec" in cmd

    def test_command_includes_ephemeral_flag(self, tmp_path: Path) -> None:
        cmd, _ = codex_build_command(model=None, prompt="analyze this", project_root=tmp_path)
        assert "--ephemeral" in cmd

    def test_command_includes_output_last_message_flag(self, tmp_path: Path) -> None:
        """Codex writes last message to a file via -o / --output-last-message."""
        cmd, output_path = codex_build_command(
            model=None, prompt="analyze this", project_root=tmp_path
        )
        assert "-o" in cmd or "--output-last-message" in cmd
        assert output_path is not None

    def test_output_path_is_within_given_project_root(self, tmp_path: Path) -> None:
        """Output file path must be under the given project_root directory."""
        cmd, output_path = codex_build_command(
            model=None, prompt="analyze this", project_root=tmp_path
        )
        assert output_path is not None
        p = Path(output_path)
        assert p.is_absolute() or str(p).startswith(str(tmp_path))

    def test_stdin_sentinel_present(self, tmp_path: Path) -> None:
        """Codex reads prompt from stdin -- command must end with '-' or stdin mode."""
        cmd, _ = codex_build_command(model=None, prompt="analyze this", project_root=tmp_path)
        # codex exec reads from stdin when last arg is '-'
        assert cmd[-1] == "-"

    def test_model_flag_included_when_provided(self, tmp_path: Path) -> None:
        cmd, _ = codex_build_command(model="gpt-4o", prompt="analyze this", project_root=tmp_path)
        assert "-m" in cmd or "--model" in cmd
        # Find the model value
        model_found = False
        for i, part in enumerate(cmd):
            if part in ("-m", "--model") and i + 1 < len(cmd):
                assert cmd[i + 1] == "gpt-4o"
                model_found = True
        assert model_found

    def test_model_flag_omitted_when_none(self, tmp_path: Path) -> None:
        cmd, _ = codex_build_command(model=None, prompt="analyze this", project_root=tmp_path)
        assert "-m" not in cmd
        assert "--model" not in cmd

    def test_command_is_list_of_strings(self, tmp_path: Path) -> None:
        cmd, _ = codex_build_command(model=None, prompt="test", project_root=tmp_path)
        assert isinstance(cmd, list)
        assert all(isinstance(part, str) for part in cmd)

    def test_returns_tuple_of_cmd_and_output_path(self, tmp_path: Path) -> None:
        """codex_build_command returns (cmd_list, output_path_str)."""
        result = codex_build_command(model=None, prompt="test", project_root=tmp_path)
        assert isinstance(result, tuple)
        assert len(result) == 2
        cmd, output_path = result
        assert isinstance(cmd, list)
        assert isinstance(output_path, str)
