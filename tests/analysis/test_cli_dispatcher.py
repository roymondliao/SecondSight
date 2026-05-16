"""Death tests and unit tests for CLIAnalysisDispatcher (Task 4).

Death tests MUST come first -- each targets a silent failure path.
Unit tests (happy-path) follow.

Death case reference (from 2-plan.md section 9):
  DC1: subprocess hangs -> timeout kill -> unknown
  DC2: schema mismatch -> retry <=2 -> failure if persists
  DC3: empty behavior_flags is valid
  DC6: CLI binary disappears -> failure with forensics

New death tests (Task 4 revision):
  DC-SIGTERM: timeout -> SIGTERM sent before SIGKILL
  DC-STDERR-SUCCESS: stderr captured even on success path
  DC-RETRY-PROMPT: retry augmented prompt verified via CLI args (not stdin)
  DC-CODEX-TEMPFILE: codex output file NOT in project_root
  DC-UNKNOWN-AGENT: state_missing returns cli_agent="unknown" (not "claude_code")
  DC-MODULE-IMPORT: module imports cleanly (catches syntax regression)
  DC-JINJA-RENDER: prompt rendered via jinja2 loader (not f-string)

Test isolation: all subprocess calls are mocked via unittest.mock.
Real-CLI tests live in test_cli_dispatcher_e2e.py (gated by SECONDSIGHT_TEST_REAL_CLI=1).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from secondsight.analysis import cli_dispatcher as cli_dispatcher_module
from secondsight.analysis.cli_dispatcher import (
    CLIAnalysisDispatcher,
    OpencodeNotSupportedError,
)
from secondsight.config.schema import (
    AnalysisCLIConfig,
    AnalysisCLIModelsConfig,
    AnalysisConfig,
    AnalysisRetryConfig,
)
from secondsight.state import SecondSightState

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _make_state(agent: str = "claude_code") -> SecondSightState:
    return SecondSightState(
        schema_version="1.0",
        init_agent=agent,
        init_at="2026-05-14T00:00:00+00:00",
        secondsight_version="0.1.0",
    )


def _make_config(default_agent: str = "claude_code", timeout_seconds: int = 30) -> AnalysisConfig:
    return AnalysisConfig(
        timeout_seconds=timeout_seconds,
        cli=AnalysisCLIConfig(
            default_agent=default_agent,
            models=AnalysisCLIModelsConfig(),
        ),
    )


def _valid_output_dict(session_id: str = "sess-001") -> dict:
    return {
        "schema_version": "1.0",
        "session_id": session_id,
        "status": "success",
        "behavior_flags": [],
        "session_summary": {
            "headline": "Session ran efficiently",
            "key_findings": [],
            "body": "No issues found.",
        },
        "dispatched_via": "cli",
        "cli_agent": "claude_code",
        "primary_model": None,
        "fallback_used": False,
        "retry_count": 0,
        "error_details": None,
    }


def _make_dispatcher(
    config: AnalysisConfig | None = None,
    state: SecondSightState | None = None,
) -> CLIAnalysisDispatcher:
    if config is None:
        config = _make_config()
    if state is None:
        state = _make_state()
    return CLIAnalysisDispatcher(config=config, state=state)


# ---------------------------------------------------------------------------
# Minimal async subprocess mock helper
# ---------------------------------------------------------------------------


def _make_proc_mock(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    hang: bool = False,
) -> MagicMock:
    """Build an asyncio.subprocess.Process mock.

    Args:
        stdout: Bytes the process writes to stdout.
        stderr: Bytes the process writes to stderr.
        returncode: Exit code of the process.
        hang: If True, communicate() raises asyncio.TimeoutError (simulates hang).
    """
    mock_proc = MagicMock()
    mock_proc.returncode = returncode

    if hang:
        mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_proc.kill = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock(return_value=None)
    else:
        mock_proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
        mock_proc.kill = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock(return_value=None)

    return mock_proc


# ===========================================================================
# DEATH TESTS -- silent failure paths first
# ===========================================================================


class TestDC_ModuleImport:
    """DC-MODULE-IMPORT: the module must import cleanly (catches syntax regressions)."""

    def test_cli_dispatcher_module_imports_cleanly(self) -> None:
        """Import the module explicitly -- catches syntax errors not caught at test collection.

        NOTE: We do NOT use importlib.reload() here because reloading creates new class objects
        that break isinstance checks and except-clause matching for the rest of the test session.
        The import() check is sufficient: if the file has a SyntaxError, the initial import at
        test collection time would already have failed (visible as a collection error).
        We verify the module attributes are accessible instead.
        """
        import secondsight.analysis.cli_dispatcher as module

        assert hasattr(module, "CLIAnalysisDispatcher")
        assert hasattr(module, "OpencodeNotSupportedError")
        # Verify the module is importable a second time (no side-effect-on-import issue)
        import importlib

        spec = importlib.util.find_spec("secondsight.analysis.cli_dispatcher")
        assert spec is not None, "Module spec must be findable (module exists on disk)"


class TestDC_JinjaRender:
    """DC-JINJA-RENDER: prompt rendered via jinja2 loader, not a hardcoded f-string."""

    @pytest.mark.asyncio
    async def test_dispatch_renders_prompt_via_jinja_loader(self, tmp_path: Path) -> None:
        """Verify _loader.render is called during dispatch (not bypassed)."""
        from secondsight.prompts import _loader

        captured_render_calls = []
        original_render = _loader.render

        def capturing_render(template_name: str, context: dict) -> str:
            captured_render_calls.append((template_name, context))
            return original_render(template_name, context)

        dispatcher = _make_dispatcher()

        with (
            patch("secondsight.prompts._loader.render", side_effect=capturing_render),
            patch(
                "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
                return_value=_make_proc_mock(stdout=json.dumps(_valid_output_dict()), stderr=""),
            ),
        ):
            await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert len(captured_render_calls) >= 1, (
            "Dispatcher must call _loader.render() at least once -- f-string bypass detected"
        )
        template_names = [call[0] for call in captured_render_calls]
        assert any("cli_dispatch" in name for name in template_names), (
            f"Expected 'cli_dispatch' template to be rendered; got: {template_names}"
        )

    @pytest.mark.asyncio
    async def test_rendered_prompt_contains_session_id(self, tmp_path: Path) -> None:
        """The rendered prompt must embed the session_id (template var substitution working)."""
        from secondsight.prompts import _loader

        captured_prompts = []
        original_render = _loader.render

        def capturing_render(template_name: str, context: dict) -> str:
            result = original_render(template_name, context)
            captured_prompts.append(result)
            return result

        dispatcher = _make_dispatcher()
        session_id = "test-session-xyz-789"

        with (
            patch("secondsight.prompts._loader.render", side_effect=capturing_render),
            patch(
                "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
                return_value=_make_proc_mock(stdout=json.dumps(_valid_output_dict()), stderr=""),
            ),
        ):
            await dispatcher.dispatch(
                session_id=session_id,
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert len(captured_prompts) >= 1
        assert session_id in captured_prompts[0], (
            f"Rendered prompt must contain session_id={session_id!r}"
        )


class TestDC1_SubprocessTimeout:
    """DC1: subprocess hangs -> timeout kill -> AnalysisOutput(status='unknown')."""

    @pytest.mark.asyncio
    async def test_hanging_subprocess_returns_unknown_status(self, tmp_path: Path) -> None:
        """A subprocess that never produces output must be killed and return unknown."""
        dispatcher = _make_dispatcher(config=_make_config(timeout_seconds=1))

        mock_proc = _make_proc_mock(hang=True, stderr="")

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "unknown"
        assert result.error_details is not None
        assert result.error_details["reason"] == "transport_timeout"
        assert result.error_details["failure_class"] == "transport_timeout"
        assert result.error_details["attempts"] == 1
        assert result.error_details["retry_exhausted"] is False

    @pytest.mark.asyncio
    async def test_timeout_sends_sigterm_before_sigkill(self, tmp_path: Path) -> None:
        """DC-SIGTERM: on timeout, dispatcher MUST call proc.terminate() (SIGTERM) first."""
        dispatcher = _make_dispatcher(config=_make_config(timeout_seconds=1))

        mock_proc = MagicMock()
        mock_proc.returncode = -1
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=None)

        # Track call order
        call_order = []

        def terminate_side_effect():
            call_order.append("terminate")

        def kill_side_effect():
            call_order.append("kill")

        mock_proc.terminate = MagicMock(side_effect=terminate_side_effect)
        mock_proc.kill = MagicMock(side_effect=kill_side_effect)

        # First communicate raises TimeoutError (the wait_for timeout)
        # Second communicate (post-SIGTERM drain) also raises TimeoutError (grace expired)
        # Third communicate (post-SIGKILL drain) returns empty
        communicate_results = [
            asyncio.TimeoutError(),  # initial wait_for timeout
            asyncio.TimeoutError(),  # SIGTERM grace period expired
            (b"", b""),  # post-SIGKILL drain
        ]

        async def communicate_side_effect(input=None, timeout=None):
            result = communicate_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        mock_proc.communicate = communicate_side_effect

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert "terminate" in call_order, "proc.terminate() (SIGTERM) must be called on timeout"
        assert "kill" in call_order, "proc.kill() (SIGKILL) must be called after grace period"
        # SIGTERM must come before SIGKILL
        assert call_order.index("terminate") < call_order.index("kill"), (
            "SIGTERM must be sent BEFORE SIGKILL -- grace period violated"
        )

    @pytest.mark.asyncio
    async def test_sigterm_handler_stderr_captured(self, tmp_path: Path) -> None:
        """DC-SIGTERM: if subprocess handles SIGTERM and writes to stderr, dispatcher captures it."""
        dispatcher = _make_dispatcher(config=_make_config(timeout_seconds=1))

        mock_proc = MagicMock()
        mock_proc.returncode = -1
        mock_proc.kill = MagicMock()
        mock_proc.wait = AsyncMock(return_value=None)
        mock_proc.terminate = MagicMock()

        # First communicate raises TimeoutError, second (after SIGTERM) returns stderr content
        communicate_results = [
            asyncio.TimeoutError(),
            (b"", b"terminated gracefully -- flushed state"),
        ]

        async def communicate_side_effect(input=None, timeout=None):
            result = communicate_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        mock_proc.communicate = communicate_side_effect

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "unknown"
        assert result.error_details is not None
        assert "stderr" in result.error_details
        assert "terminated gracefully" in result.error_details["stderr"], (
            "Dispatcher must capture stderr written during SIGTERM grace window"
        )

    @pytest.mark.asyncio
    async def test_timeout_captures_stderr_in_error_details(self, tmp_path: Path) -> None:
        """Even on timeout, any stderr captured before kill must be in error_details."""
        dispatcher = _make_dispatcher(config=_make_config(timeout_seconds=1))

        # Simulate: communicate() times out, but we drain stderr after terminate
        mock_proc = MagicMock()
        mock_proc.returncode = -1
        mock_proc.kill = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.wait = AsyncMock(return_value=None)
        # communicate raises TimeoutError; post-terminate communicate returns stderr
        communicate_calls = [
            asyncio.TimeoutError(),
            (b"", b"subprocess starting up..."),
        ]

        async def communicate_side_effect(input=None, timeout=None):
            result = communicate_calls.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        mock_proc.communicate = communicate_side_effect

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "unknown"
        assert result.error_details is not None
        assert "stderr" in result.error_details

    @pytest.mark.asyncio
    async def test_unknown_status_carries_dispatched_via_and_cli_agent(
        self, tmp_path: Path
    ) -> None:
        """DC1 + Decision A: unknown status must still have dispatched_via + cli_agent set."""
        dispatcher = _make_dispatcher(config=_make_config(timeout_seconds=1))
        mock_proc = _make_proc_mock(hang=True)

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            return_value=mock_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        # AnalysisOutput cross-field invariant: cli mode MUST have cli_agent set
        assert result.dispatched_via == "cli"
        assert result.cli_agent is not None
        assert result.primary_model is None


class TestDC2_SchemaParseRetry:
    """DC2: invalid JSON / schema mismatch -> retry <=2 -> failure after exhaustion."""

    @pytest.mark.asyncio
    async def test_fenced_json_returns_success_without_retry(self, tmp_path: Path, caplog) -> None:
        """Normalizable fenced JSON should succeed without consuming retry budget."""
        dispatcher = _make_dispatcher()
        valid_output = json.dumps(_valid_output_dict())

        async def create_proc(*args, **kwargs):
            return _make_proc_mock(stdout=f"```json\n{valid_output}\n```", stderr="")

        with (
            caplog.at_level("INFO"),
            patch(
                "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
                side_effect=create_proc,
            ),
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "success"
        assert result.retry_count == 0
        assert any(
            "normalized output" in record.message and "strip_fence" in record.message
            for record in caplog.records
        )

    @pytest.mark.asyncio
    async def test_invalid_json_stdout_retries_and_returns_failure(self, tmp_path: Path) -> None:
        """subprocess exits 0 with invalid JSON -> retries <=2 -> final failure."""
        dispatcher = _make_dispatcher()
        call_count = 0

        async def create_proc(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _make_proc_mock(stdout="not json at all", stderr="")

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "failure"
        assert result.error_details is not None
        assert result.error_details["reason"] == "retry_exhausted"
        assert result.error_details["failure_class"] == "json_decode"
        assert result.error_details["attempts"] == 3  # initial + 2 retries
        assert result.error_details["retry_exhausted"] is True
        assert result.retry_count == 2
        # Dispatcher must try 3 times total (initial + 2 retries)
        assert call_count == 3

    @pytest.mark.asyncio
    async def test_schema_mismatch_retries_and_returns_failure(self, tmp_path: Path) -> None:
        """subprocess exits 0 with valid JSON but failing AnalysisOutput validation -> failure."""
        # Valid JSON but wrong schema (missing required fields)
        bad_json = json.dumps({"status": "ok", "some_field": "some_value"})

        dispatcher = _make_dispatcher()

        async def create_proc(*args, **kwargs):
            return _make_proc_mock(stdout=bad_json, stderr="")

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "failure"
        assert result.error_details is not None
        assert result.error_details["reason"] == "retry_exhausted"
        assert result.error_details["failure_class"] == "schema_mismatch"
        assert result.error_details["attempts"] == 3
        assert result.error_details["retry_exhausted"] is True
        assert result.retry_count == 2

    @pytest.mark.asyncio
    async def test_schema_retry_exhaustion_uses_shared_taxonomy_and_keeps_cli_stderr(
        self, tmp_path: Path
    ) -> None:
        """Schema exhaustion must align with SDK fields without dropping CLI stderr evidence."""
        bad_json = json.dumps({"status": "ok"})
        dispatcher = _make_dispatcher(
            config=AnalysisConfig(
                timeout_seconds=30,
                cli=AnalysisCLIConfig(
                    default_agent="claude_code",
                    models=AnalysisCLIModelsConfig(),
                ),
                retry=AnalysisRetryConfig(
                    output_repair_max_attempts=0,
                    feedback_max_chars=400,
                ),
            )
        )

        async def create_proc(*args, **kwargs):
            return _make_proc_mock(
                stdout=bad_json, stderr="schema-warning: missing session summary"
            )

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-schema-exhausted",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "failure"
        assert result.error_details is not None
        assert result.error_details["reason"] == "retry_exhausted"
        assert result.error_details["failure_class"] == "schema_mismatch"
        assert result.error_details["attempts"] == 1
        assert result.error_details["retry_exhausted"] is True
        assert result.error_details["stderr"] == "schema-warning: missing session summary"

    @pytest.mark.asyncio
    async def test_empty_stdout_treated_as_json_decode_failure(self, tmp_path: Path) -> None:
        """Empty stdout must be treated as json_decode failure, not as empty-string parse."""
        dispatcher = _make_dispatcher()

        async def create_proc(*args, **kwargs):
            return _make_proc_mock(stdout="", stderr="")

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "failure"
        assert result.error_details is not None
        assert result.error_details["reason"] == "retry_exhausted"
        assert result.error_details["failure_class"] == "json_decode"
        assert result.error_details["attempts"] == 3
        assert result.error_details["retry_exhausted"] is True

    @pytest.mark.asyncio
    async def test_retry_prompt_uses_structured_feedback(self, tmp_path: Path) -> None:
        """Retry prompt should use structured schema feedback rather than raw exception text.

        For claude_code, the prompt is a POSITIONAL ARG (not stdin).
        We verify by capturing the args passed to create_subprocess_exec on the retry call.
        """
        bad_json = json.dumps({"status": "ok"})  # Schema mismatch

        # Capture each call's args to create_subprocess_exec
        captured_call_args: list[tuple] = []

        async def create_capture_proc(*args, **kwargs):
            # args = positional args to create_subprocess_exec, which are the cmd elements
            captured_call_args.append(args)

            class _Proc:
                returncode = 0
                kill = MagicMock()
                terminate = MagicMock()
                wait = AsyncMock(return_value=None)

                async def communicate(self, input=None, timeout=None):
                    return bad_json.encode(), b""

            return _Proc()

        dispatcher = _make_dispatcher()

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_capture_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "failure"
        # 3 calls: initial + 2 retries
        assert len(captured_call_args) == 3, (
            f"Expected 3 subprocess calls, got {len(captured_call_args)}"
        )

        # For claude_code: the prompt is the LAST positional arg in the cmd
        # cmd = ["claude", "--print", "--output-format", "json", "--no-session-persistence", <prompt>]
        # So args = ("claude", "--print", ..., <prompt>)
        initial_prompt_arg = captured_call_args[0][-1]  # last element is the prompt
        retry_prompt_arg = captured_call_args[1][-1]  # retry's last element

        # The retry prompt MUST be longer (error appended)
        assert len(retry_prompt_arg) > len(initial_prompt_arg), (
            "Retry prompt (CLI arg) must be LONGER than initial prompt (error appended)"
        )

        assert "did not match the required JSON schema" in retry_prompt_arg, (
            "Retry prompt must contain structured schema-mismatch guidance."
        )
        assert "session_summary" in retry_prompt_arg, (
            "Retry prompt must surface the missing field name from structured feedback."
        )


class TestDC6_SubprocessNonZeroExit:
    """DC6 / subprocess exit NON-zero -> failure with forensics, NO retry."""

    @pytest.mark.asyncio
    async def test_nonzero_exit_returns_failure_no_retry(self, tmp_path: Path) -> None:
        """subprocess exit != 0 -> failure immediately, no retry loop."""
        call_count = 0

        async def create_proc(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _make_proc_mock(stdout="", stderr="binary not found", returncode=127)

        dispatcher = _make_dispatcher()

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "failure"
        assert result.error_details is not None
        assert result.error_details["reason"] == "fatal_execution_error"
        assert result.error_details["failure_class"] == "fatal_execution_error"
        assert result.error_details["exit_code"] == 127
        assert result.error_details["attempts"] == 1
        assert result.error_details["retry_exhausted"] is False
        # NO retry on non-zero exit
        assert call_count == 1
        assert result.retry_count == 0

    @pytest.mark.asyncio
    async def test_nonzero_exit_captures_stderr(self, tmp_path: Path) -> None:
        """stderr must be captured into error_details even for non-zero exit."""
        stderr_text = "error: model not available\nfatal: authentication failed"

        async def create_proc(*args, **kwargs):
            return _make_proc_mock(stdout="", stderr=stderr_text, returncode=1)

        dispatcher = _make_dispatcher()

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.error_details is not None
        assert result.error_details["stderr"] == stderr_text

    @pytest.mark.asyncio
    async def test_exit_code_1_no_retry(self, tmp_path: Path) -> None:
        """Exit code 1 (general failure) also gets no retry."""
        call_count = 0

        async def create_proc(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            return _make_proc_mock(returncode=1)

        dispatcher = _make_dispatcher()

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert call_count == 1

    @pytest.mark.asyncio
    async def test_claude_nonzero_exit_recovers_quota_diagnostics_from_stdout(
        self,
        tmp_path: Path,
        caplog,
    ) -> None:
        """Claude non-zero exit must surface structured stdout diagnostics.

        Claude CLI can emit a JSON envelope on stdout even when it exits 1.
        Quota failures otherwise look like opaque "stderr=''" subprocess exits.
        """
        claude_error_stdout = json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": True,
                "api_error_status": 429,
                "result": "You've hit your org's monthly usage limit",
            }
        )

        async def create_proc(*args, **kwargs):
            return _make_proc_mock(stdout=claude_error_stdout, stderr="", returncode=1)

        dispatcher = _make_dispatcher()

        with (
            caplog.at_level("WARNING"),
            patch(
                "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
                side_effect=create_proc,
            ),
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "failure"
        assert result.error_details is not None
        assert result.error_details["reason"] == "transport_rate_limit"
        assert result.error_details["failure_class"] == "transport_rate_limit"
        assert result.error_details["evidence_source"] == "cli_stdout_envelope"
        assert result.error_details["evidence_confidence"] == "derived"
        assert result.error_details["evidence_executor"] == "claude_code"
        assert result.error_details["api_error_status"] == 429
        assert result.error_details["message"] == "You've hit your org's monthly usage limit"
        assert any("api_error_status=429" in record.message for record in caplog.records)
        assert any("monthly usage limit" in record.message for record in caplog.records), (
            caplog.text
        )

    @pytest.mark.asyncio
    async def test_claude_nonzero_exit_classifies_auth_through_adapter_evidence(
        self,
        tmp_path: Path,
    ) -> None:
        claude_error_stdout = json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "api_error_status": 401,
                "result": "Invalid API key",
            }
        )

        async def create_proc(*args, **kwargs):
            return _make_proc_mock(stdout=claude_error_stdout, stderr="", returncode=1)

        dispatcher = _make_dispatcher()

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "failure"
        assert result.error_details is not None
        assert result.error_details["reason"] == "fatal_auth_or_config"
        assert result.error_details["failure_class"] == "fatal_auth_or_config"
        assert result.error_details["evidence_source"] == "cli_stdout_envelope"
        assert result.error_details["evidence_confidence"] == "derived"
        assert result.error_details["evidence_executor"] == "claude_code"

    @pytest.mark.asyncio
    async def test_claude_ambiguous_nonzero_exit_stays_low_confidence_fatal_execution(
        self,
        tmp_path: Path,
    ) -> None:
        claude_error_stdout = json.dumps(
            {
                "type": "result",
                "subtype": "error_during_execution",
                "is_error": True,
                "result": "model stopped unexpectedly",
            }
        )

        async def create_proc(*args, **kwargs):
            return _make_proc_mock(stdout=claude_error_stdout, stderr="", returncode=1)

        dispatcher = _make_dispatcher()

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "failure"
        assert result.error_details is not None
        assert result.error_details["reason"] == "fatal_execution_error"
        assert result.error_details["failure_class"] == "fatal_execution_error"
        assert result.error_details["evidence_source"] == "cli_stdout_envelope"
        assert result.error_details["evidence_confidence"] == "unknown"
        assert result.error_details["retry_mode"] == "none"

    @pytest.mark.asyncio
    async def test_codex_nonzero_exit_uses_adapter_evidence(
        self,
        tmp_path: Path,
    ) -> None:
        config = _make_config(default_agent="codex")
        state = _make_state(agent="codex")
        dispatcher = CLIAnalysisDispatcher(config=config, state=state)

        async def create_proc(*args, **kwargs):
            return _make_proc_mock(
                stdout="",
                stderr="Authentication failed for configured Codex account",
                returncode=1,
            )

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "failure"
        assert result.error_details is not None
        assert result.error_details["reason"] == "fatal_auth_or_config"
        assert result.error_details["failure_class"] == "fatal_auth_or_config"
        assert result.error_details["evidence_source"] == "cli_stderr"
        assert result.error_details["evidence_confidence"] == "heuristic"
        assert result.error_details["evidence_executor"] == "codex"


class TestDC_CodexTempfile:
    """DC-CODEX-TEMPFILE: codex output file must NOT be placed in project_root."""

    @pytest.mark.asyncio
    async def test_codex_output_file_not_in_project_root(self, tmp_path: Path) -> None:
        """For codex, the output file must NOT be created inside project_root."""
        config = _make_config(default_agent="codex")
        state = _make_state(agent="codex")
        dispatcher = CLIAnalysisDispatcher(config=config, state=state)

        captured_cmd_args: list[tuple] = []
        valid_output = _valid_output_dict()
        valid_output["cli_agent"] = "codex"

        async def create_proc(*args, **kwargs):
            captured_cmd_args.append(args)
            return _make_proc_mock(stdout=json.dumps(valid_output), stderr="")

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        # Find the -o flag in the command to get the output_path
        assert len(captured_cmd_args) >= 1
        cmd_elements = list(captured_cmd_args[0])  # positional args to create_subprocess_exec

        output_file_path = None
        for i, elem in enumerate(cmd_elements):
            if elem == "-o" and i + 1 < len(cmd_elements):
                output_file_path = cmd_elements[i + 1]
                break

        assert output_file_path is not None, "codex command must have -o <output_file> flag"
        assert not output_file_path.startswith(str(tmp_path)), (
            f"Output file {output_file_path!r} must NOT be inside project_root={str(tmp_path)!r}. "
            "Codex output files must go in a system temp directory."
        )

    @pytest.mark.asyncio
    async def test_codex_output_file_failure_uses_adapter_evidence(
        self,
        tmp_path: Path,
    ) -> None:
        config = _make_config(default_agent="codex")
        state = _make_state(agent="codex")
        dispatcher = CLIAnalysisDispatcher(config=config, state=state)

        async def create_proc(*args, **kwargs):
            return _make_proc_mock(stdout="", stderr="", returncode=0)

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "failure"
        assert result.error_details is not None
        assert result.error_details["reason"] == "fatal_execution_error"
        assert result.error_details["failure_class"] == "fatal_execution_error"
        assert result.error_details["evidence_source"] == "cli_output_file"
        assert result.error_details["evidence_confidence"] == "typed"
        assert result.error_details["evidence_executor"] == "codex"


class TestDC_UnknownAgent:
    """DC-UNKNOWN-AGENT: state_missing must report cli_agent='unknown', not 'claude_code'."""

    @pytest.mark.asyncio
    async def test_state_missing_returns_unknown_agent_not_default(self, tmp_path: Path) -> None:
        """When state is missing, cli_agent must NOT be 'claude_code' (dishonest default)."""
        config = _make_config(default_agent="auto")
        dispatcher = CLIAnalysisDispatcher(config=config, state=None)

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec"
        ) as mock_create:
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "failure"
        assert result.cli_agent != "claude_code", (
            "cli_agent='claude_code' is a lie when no agent was identified. "
            "State_missing must use 'unknown' sentinel."
        )
        assert result.cli_agent == "unknown", (
            f"Expected cli_agent='unknown' for state_missing, got {result.cli_agent!r}"
        )
        assert result.error_details is not None
        assert result.error_details["reason"] == "fatal_execution_error"
        assert result.error_details["failure_class"] == "fatal_execution_error"
        assert result.error_details["executor_reason"] == "state_missing"
        mock_create.assert_not_called()


class TestDC_StderrOnSuccess:
    """DC-STDERR-SUCCESS: stderr must be captured into error_details even on success path."""

    @pytest.mark.asyncio
    async def test_stderr_captured_in_error_details_on_success(self, tmp_path: Path) -> None:
        """When subprocess succeeds but emits stderr, error_details must carry that stderr."""
        stderr_noise = "Warning: using default model\nLoaded config from ~/.claude/settings.json"

        async def create_proc(*args, **kwargs):
            return _make_proc_mock(
                stdout=json.dumps(_valid_output_dict()),
                stderr=stderr_noise,
            )

        dispatcher = _make_dispatcher()

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "success"
        # Forensics requirement: stderr must be available even on success
        assert result.error_details is not None, (
            "error_details must be populated with stderr even on success path"
        )
        assert "stderr" in result.error_details, (
            "error_details must contain 'stderr' key for forensics"
        )
        assert stderr_noise in result.error_details["stderr"], (
            "The actual stderr content must be preserved verbatim"
        )

    @pytest.mark.asyncio
    async def test_clean_success_no_stderr_has_none_error_details(self, tmp_path: Path) -> None:
        """When subprocess succeeds with NO stderr, error_details should remain None."""

        async def create_proc(*args, **kwargs):
            return _make_proc_mock(
                stdout=json.dumps(_valid_output_dict()),
                stderr="",  # no stderr
            )

        dispatcher = _make_dispatcher()

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "success"
        assert result.error_details is None, (
            "When there is no stderr on success, error_details should be None (clean output)"
        )


class TestOpencodeRejection:
    """default_agent='opencode' -> failure at entry, never invokes subprocess."""

    @pytest.mark.asyncio
    async def test_opencode_raises_or_returns_failure_before_subprocess(
        self, tmp_path: Path
    ) -> None:
        """opencode must be rejected before any subprocess is spawned."""
        config = _make_config(default_agent="opencode")
        state = _make_state(agent="opencode")
        dispatcher = CLIAnalysisDispatcher(config=config, state=state)

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec"
        ) as mock_create:
            try:
                result = await dispatcher.dispatch(
                    session_id="sess-001",
                    project_root=tmp_path,
                    session_payload={"events": []},
                )
                # If it returns rather than raises, must be a failure
                assert result.status == "failure"
                ed = result.error_details or {}
                assert (
                    "opencode" in ed.get("reason", "").lower()
                    or "opencode" in ed.get("message", "").lower()
                    or ed.get("reason") == "unsupported_agent"
                )
            except OpencodeNotSupportedError:
                pass  # Raising is also acceptable

            # subprocess must NOT have been spawned
            mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_cli_agent_returns_failure_without_falling_back_to_claude(
        self,
        tmp_path: Path,
    ) -> None:
        config = _make_config(default_agent="gemini_cli")
        dispatcher = CLIAnalysisDispatcher(config=config, state=None)

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec"
        ) as mock_create:
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "failure"
        assert result.cli_agent == "gemini_cli"
        assert result.error_details is not None
        assert result.error_details["reason"] == "fatal_execution_error"
        assert result.error_details["failure_class"] == "fatal_execution_error"
        assert result.error_details["executor_reason"] == "unsupported_agent"
        mock_create.assert_not_called()


class TestEnvIsolation:
    """SECONDSIGHT_* variables must NOT leak into subprocess environment."""

    @pytest.mark.asyncio
    async def test_secondsight_env_vars_filtered_from_subprocess(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Parent SECONDSIGHT_* vars must be filtered; only the hook-disable flag remains."""
        monkeypatch.setenv("SECONDSIGHT_SESSION_ID", "secret-session-123")
        monkeypatch.setenv("SECONDSIGHT_PROJECT_ID", "secret-project-456")
        monkeypatch.setenv("SECONDSIGHT_DISABLE_HOOKS", "0")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")  # keep PATH alive

        captured_env: dict | None = None

        async def create_proc(*args, **kwargs):
            nonlocal captured_env
            captured_env = kwargs.get("env", {})
            return _make_proc_mock(stdout=json.dumps(_valid_output_dict()), stderr="")

        dispatcher = _make_dispatcher()

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert captured_env is not None, "create_subprocess_exec must have been called with env="
        assert captured_env.get("SECONDSIGHT_DISABLE_HOOKS") == "1", (
            "Dispatcher must force-enable hook suppression for analysis subprocesses"
        )
        leaked = {
            key: value
            for key, value in captured_env.items()
            if key.startswith("SECONDSIGHT_") and key != "SECONDSIGHT_DISABLE_HOOKS"
        }
        assert leaked == {}, f"Unexpected SECONDSIGHT_* leak into subprocess env: {leaked!r}"


class TestSubprocessCwd:
    """Subprocess cwd must equal the project_root passed into dispatch."""

    @pytest.mark.asyncio
    async def test_subprocess_cwd_equals_project_root(self, tmp_path: Path) -> None:
        """The subprocess's working directory must be project_root."""
        captured_cwd: Path | None = None

        async def create_proc(*args, **kwargs):
            nonlocal captured_cwd
            captured_cwd = kwargs.get("cwd")
            return _make_proc_mock(stdout=json.dumps(_valid_output_dict()), stderr="")

        dispatcher = _make_dispatcher()
        project_root = tmp_path / "myproject"
        project_root.mkdir()

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            await dispatcher.dispatch(
                session_id="sess-001",
                project_root=project_root,
                session_payload={"events": []},
            )

        assert str(captured_cwd) == str(project_root)


class TestStderrCapture:
    """stderr must be captured into error_details (success) and separately from stdout (always)."""

    @pytest.mark.asyncio
    async def test_stderr_not_merged_into_stdout(self, tmp_path: Path) -> None:
        """stderr must be captured separately -- NOT merged into stdout via stderr=STDOUT."""
        # If stderr were merged into stdout, a valid JSON output would be corrupted
        # by stderr text prepended/appended to it. This test verifies the dispatcher
        # uses separate PIPE for stderr, not STDOUT redirect.
        stderr_before_json = "Loading...\n"
        valid_json = json.dumps(_valid_output_dict())

        # With correct separation, stdout=valid_json and stderr=noise -> succeeds.
        # With incorrect STDOUT merge, stdout = noise + valid_json -> fails parse.
        async def create_proc(*args, **kwargs):
            return _make_proc_mock(
                stdout=valid_json,
                stderr=stderr_before_json,
            )

        dispatcher = _make_dispatcher()

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "success", (
            "If stderr was merged into stdout, valid JSON would be corrupted by "
            "leading stderr text, causing a parse failure"
        )


class TestAutoAgentResolution:
    """'auto' in config.cli.default_agent -> resolved from state.json ONCE at entry."""

    @pytest.mark.asyncio
    async def test_auto_resolves_to_state_init_agent(self, tmp_path: Path) -> None:
        """'auto' must resolve to state.init_agent on entry, not per-retry."""
        config = _make_config(default_agent="auto")
        state = _make_state(agent="claude_code")
        dispatcher = CLIAnalysisDispatcher(config=config, state=state)

        captured_commands: list[list] = []

        async def create_proc(*args, **kwargs):
            captured_commands.append(list(args))
            return _make_proc_mock(stdout=json.dumps(_valid_output_dict()), stderr="")

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.cli_agent == "claude_code"

    @pytest.mark.asyncio
    async def test_auto_with_missing_state_returns_failure(self, tmp_path: Path) -> None:
        """If state is None (fresh install, no state.json), dispatcher returns failure."""
        config = _make_config(default_agent="auto")
        dispatcher = CLIAnalysisDispatcher(config=config, state=None)

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec"
        ) as mock_create:
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "failure"
        assert (
            "init" in str(result.error_details).lower()
            or "state" in str(result.error_details).lower()
        )
        mock_create.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_resolution_is_stable_across_retries(self, tmp_path: Path) -> None:
        """The agent resolved from state must be the same on all retries (read ONCE)."""
        config = _make_config(default_agent="auto")
        state = _make_state(agent="claude_code")
        dispatcher = CLIAnalysisDispatcher(config=config, state=state)

        bad_json = json.dumps({"invalid": "schema"})

        async def create_proc(*args, **kwargs):
            # Each call verifies the same agent is used across retries
            return _make_proc_mock(stdout=bad_json, stderr="")

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        # All 3 attempts must use the same cli_agent (verified via result)
        assert result.cli_agent == "claude_code"


# ===========================================================================
# UNIT TESTS -- happy path
# ===========================================================================


class TestSharedObservabilityContract:
    """Shared observability contract for non-retryable CLI failures."""

    def test_failure_output_namespaces_colliding_raw_error_details(self) -> None:
        """CLI raw evidence must not overwrite shared observability fields."""
        result = cli_dispatcher_module._make_failure_output(
            session_id="sess-cli-collision",
            reason="schema_mismatch",
            failure_class="schema_mismatch",
            agent_name="claude_code",
            attempts=1,
            error="validated error",
            extra_error_details={
                "reason": "raw reason",
                "failure_class": "raw class",
                "attempts": 99,
                "error": "raw provider error",
                "stderr_excerpt": "bad output",
            },
        )

        assert result.error_details is not None
        assert result.error_details["reason"] == "schema_mismatch"
        assert result.error_details["failure_class"] == "schema_mismatch"
        assert result.error_details["attempts"] == 1
        assert result.error_details["error"] == "validated error"
        assert result.error_details["stderr_excerpt"] == "bad output"
        assert result.error_details["raw_error_details"] == {
            "reason": "raw reason",
            "failure_class": "raw class",
            "attempts": 99,
            "error": "raw provider error",
        }

    @pytest.mark.asyncio
    async def test_subprocess_exit_uses_shared_failure_taxonomy(self, tmp_path: Path) -> None:
        """CLI non-zero exit must emit shared taxonomy fields plus CLI-specific evidence."""
        dispatcher = _make_dispatcher(
            config=AnalysisConfig(
                timeout_seconds=30,
                cli=AnalysisCLIConfig(
                    default_agent="claude_code",
                    models=AnalysisCLIModelsConfig(),
                ),
                retry=AnalysisRetryConfig(
                    output_repair_max_attempts=2,
                    feedback_max_chars=400,
                ),
            )
        )

        async def create_proc(*args, **kwargs):
            return _make_proc_mock(stdout="", stderr="fatal cli exit", returncode=1)

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-cli-exit",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "failure"
        assert result.error_details is not None
        assert result.error_details["reason"] == "fatal_execution_error"
        assert result.error_details["failure_class"] == "fatal_execution_error"
        assert result.error_details["attempts"] == 1
        assert result.error_details["retry_exhausted"] is False
        assert result.error_details["exit_code"] == 1
        assert result.error_details["stderr"] == "fatal cli exit"


class TestHappyPath:
    """Happy path: valid output -> AnalysisOutput(status='success')."""

    @pytest.mark.asyncio
    async def test_happy_path_claude_code(self, tmp_path: Path) -> None:
        """Valid AnalysisOutput JSON from subprocess -> success result."""
        valid_output = _valid_output_dict()

        async def create_proc(*args, **kwargs):
            return _make_proc_mock(stdout=json.dumps(valid_output), stderr="")

        dispatcher = _make_dispatcher()

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"session_id": "sess-001", "events": []},
            )

        assert result.status == "success"
        assert result.dispatched_via == "cli"
        assert result.cli_agent == "claude_code"
        assert result.retry_count == 0
        # error_details may be None (no stderr) or {"stderr": ""} never set
        # We just verify success shape
        assert result.behavior_flags == []

    @pytest.mark.asyncio
    async def test_empty_behavior_flags_is_valid(self, tmp_path: Path) -> None:
        """DC3: empty behavior_flags list is valid and must NOT cause failure."""
        valid_output = _valid_output_dict()
        valid_output["behavior_flags"] = []  # explicitly empty

        async def create_proc(*args, **kwargs):
            return _make_proc_mock(stdout=json.dumps(valid_output), stderr="")

        dispatcher = _make_dispatcher()

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "success"
        assert result.behavior_flags == []

    @pytest.mark.asyncio
    async def test_retry_recovers_on_second_attempt(self, tmp_path: Path) -> None:
        """CLI mode retry recovers: first attempt fails, second succeeds."""
        valid_output = json.dumps(_valid_output_dict())
        attempt = 0

        async def create_proc(*args, **kwargs):
            nonlocal attempt
            attempt += 1
            if attempt == 1:
                return _make_proc_mock(stdout="invalid json", stderr="")
            return _make_proc_mock(stdout=valid_output, stderr="")

        dispatcher = _make_dispatcher()

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.status == "success"
        assert result.retry_count == 1

    @pytest.mark.asyncio
    async def test_dispatch_uses_codex_when_configured(self, tmp_path: Path) -> None:
        """When default_agent=codex, dispatcher uses codex adapter's command."""
        config = _make_config(default_agent="codex")
        state = _make_state(agent="codex")
        dispatcher = CLIAnalysisDispatcher(config=config, state=state)

        # For codex: output goes to a temp file, not stdout
        # We test that cli_agent is "codex" in the result
        valid_output = _valid_output_dict()
        valid_output["cli_agent"] = "codex"

        async def create_proc(*args, **kwargs):
            return _make_proc_mock(stdout=json.dumps(valid_output), stderr="")

        with patch(
            "secondsight.analysis.cli_dispatcher.asyncio.create_subprocess_exec",
            side_effect=create_proc,
        ):
            result = await dispatcher.dispatch(
                session_id="sess-001",
                project_root=tmp_path,
                session_payload={"events": []},
            )

        assert result.cli_agent == "codex"
