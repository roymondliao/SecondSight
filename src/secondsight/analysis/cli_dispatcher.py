"""CLIAnalysisDispatcher — spawns coding agent CLI as one-shot subprocess (Task 4).

Supports: claude_code, codex.
Rejects: opencode (raises OpencodeNotSupportedError at entry).

Dispatch flow:
    1. Resolve agent (expand "auto" from state.json — read ONCE at entry)
    2. Reject opencode
    3. Render jinja2 prompt via Task 3's loader (_loader.render "analysis/cli_dispatch")
    4. Pick adapter (build_command only — env filtering is centralized here)
    5. Spawn subprocess with asyncio.create_subprocess_exec
    6. Timeout-bounded await via process.communicate(timeout=...)
    7. Parse response with retry loop (max 2 retries)
    8. Return AnalysisOutput

Death cases:
    DC1: timeout -> SIGTERM -> 1s grace -> SIGKILL -> return unknown
    DC2: schema mismatch -> retry (augmented prompt) -> failure after 2 retries
    DC3: empty behavior_flags is valid
    DC6: CLI exits non-zero -> failure with forensics, NO retry

Assumptions:
    - asyncio.create_subprocess_exec is used (not shell=True) -- no shell injection risk
    - process.communicate(input=...) handles stdin close correctly
    - Claude wraps output in {"type":"result","result":"..."} JSON envelope
    - Codex writes last message to a temp file specified by -o flag
    - SECONDSIGHT_* env vars are filtered in _filter_env() (centralized here, not in adapters)
      except for the internal hook-suppression flag injected by this dispatcher

Silent failure conditions:
    - If the LLM produces valid JSON that passes AnalysisOutput validation but
      contains semantically wrong behavior flags (wrong flag_type values mapped
      to wrong events), this dispatcher cannot detect that -- it's a prompt quality
      issue, not a schema issue.
    - If the output file for codex is written but truncated (disk full), we get
      a partial JSON that fails parse -- caught as json_decode failure.

Design choices:
    - Retry on json_decode and schema_mismatch only (not on non-zero exit code)
    - Non-zero exit: subprocess died, retrying the same command won't help
    - Retry augments prompt with the validation error message verbatim
    - State is read ONCE at dispatch entry (not per-retry) per task spec
    - OpencodeNotSupportedError is raised (not returned as failure) because it
      indicates a programming error in the caller: Task 6's pre-check should
      have rejected opencode before calling dispatch
    - Adapters do NOT own env filtering (build_env removed from adapters);
      env filtering is centralized in _filter_env() called by _run_once().
      This removes ghost-promise dead code from adapters.
    - state_missing path: dispatch() returns before _run_with_retry is entered;
      the architectural luck is that _run_with_retry never sees state_missing.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import ValidationError

from secondsight.analysis.cli_adapters import claude_code as _claude_code_adapter
from secondsight.analysis.cli_adapters import codex as _codex_adapter
from secondsight.analysis.output import AnalysisOutput
from secondsight.analysis.output_recovery import (
    FailureClass,
    RetryMode,
    build_recovery_error_details,
    classify_empty_output,
    classify_output_failure,
    decide_retry,
    normalize_llm_json_text,
    sanitize_error_details,
)
from secondsight.config.constants import BUILTIN_ANALYSIS_MAX_RETRY_COUNT_CAP
from secondsight.config.schema import AnalysisConfig
from secondsight.state import SecondSightState

# Grace period after SIGTERM before SIGKILL on timeout
_SIGKILL_GRACE_SECONDS: float = 1.0

# Sentinel value for cli_agent when agent could not be resolved
# (e.g., state_missing failure path). Cross-field invariant requires cli_agent
# to be non-None when dispatched_via='cli'; "unknown" is the explicit sentinel.
_UNKNOWN_AGENT: str = "unknown"

# Internal guard passed to analysis CLI subprocesses so globally-installed
# SecondSight hooks do not observe analysis sessions and recursively dispatch.
_HOOK_DISABLE_ENV_VAR: str = "SECONDSIGHT_DISABLE_HOOKS"


class OpencodeNotSupportedError(Exception):
    """Raised when default_agent resolves to 'opencode'.

    opencode is not supported by CLIAnalysisDispatcher.
    Task 6's pre-check should reject opencode at startup -- this error
    is a defense-in-depth guard in case the pre-check is bypassed.
    """


class CLIAnalysisDispatcher:
    """Spawns the coding agent CLI as a one-shot subprocess for analysis dispatch.

    Conforms to the AnalysisDispatcher Protocol (analysis/dispatcher.py).
    Task 6's ProjectAnalysisRuntime.dispatch() calls this uniformly alongside
    SDKAnalysisDispatcher — no mode-branching required at the caller.

    Args:
        config: AnalysisConfig (carries timeout_seconds, cli.default_agent, cli.models).
        state: SecondSightState or None.
            - SecondSightState: used to resolve default_agent="auto" to state.init_agent.
            - None: if default_agent="auto" and state is None, dispatch returns failure
              with an actionable error pointing to `secondsight init`.
    """

    def __init__(
        self,
        config: AnalysisConfig,
        state: SecondSightState | None,
    ) -> None:
        self._config = config
        self._state = state

    async def dispatch(
        self,
        session_id: str,
        session_payload: dict[str, Any],
        project_root: Path | None = None,
    ) -> AnalysisOutput:
        """Dispatch one analysis session to the CLI agent.

        Conforms to the AnalysisDispatcher Protocol (analysis/dispatcher.py).
        project_root is a keyword argument (was positional in Task 4) to match
        the shared Protocol signature used by Task 6's ProjectAnalysisRuntime.

        Args:
            session_id: ID of the session being analyzed.
            session_payload: The session data dict to include in the rendered prompt.
            project_root: Absolute path to the project root. Used as subprocess cwd.
                REQUIRED for CLI dispatch — raises ValueError if None or not provided.
                (SDK dispatcher accepts this same parameter but ignores it.)

        Returns:
            AnalysisOutput with status "success", "failure", or "unknown".
            Never raises except for OpencodeNotSupportedError and ValueError
            (the latter only when project_root is None).
        """
        # Validate project_root at entry — CLI genuinely needs it for subprocess cwd.
        # Fail loud here rather than get a confusing TypeError deep in _run_with_retry.
        if project_root is None:
            raise ValueError(
                "CLIAnalysisDispatcher.dispatch() requires project_root. "
                "Pass the absolute project path as project_root=<path>. "
                "This parameter is required for subprocess cwd (CLI mode)."
            )
        # Step 1: Resolve agent (read state ONCE at entry -- not per-retry)
        agent_name, failure_output = self._resolve_agent(session_id)
        if failure_output is not None:
            return failure_output

        # Step 2: Reject opencode
        if agent_name == "opencode":
            raise OpencodeNotSupportedError(
                "opencode is not supported by CLIAnalysisDispatcher. "
                "Task 6 pre-check should reject opencode before dispatch is attempted."
            )

        # Step 3: Determine model override
        model_override: str | None = None
        if agent_name == "claude_code":
            raw_model = self._config.cli.models.claude_code
        elif agent_name == "codex":
            raw_model = self._config.cli.models.codex
        else:
            raw_model = ""
        if raw_model:
            model_override = raw_model

        # Step 4: Render prompt via Task 3's jinja2 loader
        # Template: analysis/cli_dispatch.jinja2
        # SDK vs CLI asymmetry: SDK dispatcher uses pydantic-ai's output_type mechanism
        # so it doesn't need the explicit schema instruction. CLI dispatcher passes the
        # full AnalysisOutput schema as a JSON string in the prompt to guide the LLM.
        # Both dispatchers use _loader.render(); they just pick different template names.
        from secondsight.analysis.schemas import FLAG_DEFINITIONS, BehaviorFlagType
        from secondsight.prompts import _loader

        flag_defs_lines = [
            f"- {ft.value}: {FLAG_DEFINITIONS[ft]['description']}" for ft in BehaviorFlagType
        ]
        flag_definitions_block = "\n".join(flag_defs_lines)

        schema = AnalysisOutput.model_json_schema()

        base_prompt = _loader.render(
            "analysis/cli_dispatch",
            context={
                "flag_definitions_block": flag_definitions_block,
                "session_payload_json": json.dumps(session_payload, indent=2),
                "analysis_output_schema_json": json.dumps(schema, indent=2),
                "session_id": session_id,
                "cli_agent": agent_name,
            },
        )

        # Step 5-8: Spawn subprocess with retry loop
        return await self._run_with_retry(
            agent_name=agent_name,
            model=model_override,
            initial_prompt=base_prompt,
            project_root=project_root,
            session_id=session_id,
        )

    def _resolve_agent(self, session_id: str) -> tuple[str, AnalysisOutput | None]:
        """Resolve 'auto' to the init_agent from state.json.

        Returns:
            (agent_name, None) on success.
            ("", failure_output) if resolution fails.
        """
        default_agent = self._config.cli.default_agent
        if default_agent != "auto":
            return default_agent, None

        # "auto" path: requires state
        if self._state is None:
            logger.error(
                "CLI dispatch: default_agent='auto' but state.json is missing. "
                "Run 'secondsight init' to initialize state."
            )
            return "", _make_failure_output(
                session_id=session_id,
                reason=FailureClass.FATAL_EXECUTION_ERROR.value,
                failure_class=FailureClass.FATAL_EXECUTION_ERROR.value,
                agent_name=_UNKNOWN_AGENT,
                message=(
                    "default_agent='auto' requires state.json. "
                    "Run 'secondsight init' to set up the agent."
                ),
                extra_error_details={"executor_reason": "state_missing"},
            )

        agent_name = self._state.init_agent
        logger.debug(f"CLI dispatch: resolved 'auto' -> {agent_name!r} from state.json")
        return agent_name, None

    async def _run_with_retry(
        self,
        agent_name: str,
        model: str | None,
        initial_prompt: str,
        project_root: Path,
        session_id: str,
    ) -> AnalysisOutput:
        """Run subprocess with bounded output-repair retry on parse/schema failure."""
        current_prompt = initial_prompt
        last_feedback: str = ""
        last_reason: str = ""
        max_retries = (
            self._config.retry.output_repair_max_attempts if self._config.retry.enabled else 0
        )
        max_attempts = max_retries + 1

        for attempt in range(max_attempts):
            is_retry = attempt > 0
            if is_retry:
                logger.warning(
                    f"CLI dispatch: retry {attempt}/{max_retries} for session {session_id!r}. "
                    f"Previous error: {last_reason!r}"
                )
                current_prompt = _augment_prompt_with_error(initial_prompt, last_feedback)

            result = await self._run_once(
                agent_name=agent_name,
                model=model,
                prompt=current_prompt,
                project_root=project_root,
                session_id=session_id,
                attempt=attempt,
                max_attempts=max_attempts,
            )

            if result is None:
                # None means "retry" -- but should not happen since _run_once returns AnalysisOutput
                break

            # Non-retryable: return immediately
            if result.status == "unknown":
                return result  # timeout -- no point retrying

            # Success -- update retry_count to reflect actual attempt number
            if result.status == "success":
                if attempt > 0:
                    # Rebuild with corrected retry_count (LLM output always has 0)
                    return AnalysisOutput.model_validate(
                        {**result.model_dump(), "retry_count": attempt}
                    )
                return result

            # Retryable failure
            last_reason = (
                str(result.error_details.get("reason", "unknown"))
                if result.error_details
                else "unknown"
            )
            retry_mode = (
                str(result.error_details.get("retry_mode", RetryMode.NONE.value))
                if result.error_details
                else RetryMode.NONE.value
            )
            retry_exhausted = (
                bool(result.error_details.get("retry_exhausted", False))
                if result.error_details
                else False
            )
            last_feedback = (
                str(result.error_details.get("retry_feedback", "")) if result.error_details else ""
            )
            if retry_mode == RetryMode.OUTPUT_REPAIR.value and not retry_exhausted:
                continue
            return result

        # Should not reach here
        return _make_failure_output(
            session_id=session_id,
            reason="fatal_execution_error",
            failure_class=FailureClass.FATAL_EXECUTION_ERROR.value,
            agent_name=agent_name,
            message="Retry loop exhausted without returning -- this is a bug.",
            attempts=max_attempts,
            retry_exhausted=False,
            retry_mode=RetryMode.NONE.value,
        )

    async def _run_once(
        self,
        agent_name: str,
        model: str | None,
        prompt: str,
        project_root: Path,
        session_id: str,
        attempt: int,
        max_attempts: int,
    ) -> AnalysisOutput:
        """Spawn subprocess once, parse output, return AnalysisOutput."""
        # Build command + env (env filtering centralized here -- adapters do not own env)
        env = _filter_env(os.environ.copy())

        # For codex: need a temp dir for output file (NOT in project_root)
        output_path: str | None = None
        _tmpdir_ctx = None  # keep reference to prevent premature cleanup

        if agent_name == "codex":
            # Use system temp directory to avoid polluting project_root with output files.
            # TemporaryDirectory context is opened here so cleanup is automatic on
            # any exit path (exception, return, crash). The context manager object is
            # kept alive by _tmpdir_ctx for the duration of this method.
            _tmpdir_ctx = tempfile.TemporaryDirectory(prefix="secondsight_codex_")
            tmpdir = Path(_tmpdir_ctx.name)
            cmd, output_path = _codex_adapter.build_command(
                model=model, prompt=prompt, project_root=tmpdir
            )
            stdin_bytes = prompt.encode()
        else:
            cmd = _claude_code_adapter.build_command(
                model=model, prompt=prompt, project_root=project_root
            )
            stdin_bytes = None  # claude: prompt is positional arg, not stdin

        logger.debug(
            f"CLI dispatch: spawning {agent_name!r} (attempt {attempt + 1}), "
            f"cmd={cmd[0]!r}, cwd={project_root!r}"
        )

        # Spawn subprocess
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(project_root),
                env=env,
            )
        except FileNotFoundError as exc:
            logger.error(f"CLI dispatch: binary not found for {agent_name!r}: {exc}")
            if _tmpdir_ctx is not None:
                _tmpdir_ctx.cleanup()
            return _make_failure_output(
                session_id=session_id,
                reason=FailureClass.FATAL_EXECUTION_ERROR.value,
                failure_class=FailureClass.FATAL_EXECUTION_ERROR.value,
                agent_name=agent_name,
                exit_code=-1,
                stderr=str(exc),
                retry_count=attempt,
                attempts=attempt + 1,
                retry_exhausted=False,
                retry_mode=RetryMode.NONE.value,
                extra_error_details={"executor_reason": "subprocess_exit"},
            )

        # Await output with timeout
        stdout_raw: str = ""
        stderr_raw: str = ""
        timed_out = False

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes),
                timeout=self._config.timeout_seconds,
            )
            stdout_raw = stdout_bytes.decode(errors="replace")
            stderr_raw = stderr_bytes.decode(errors="replace")
        except asyncio.TimeoutError:
            timed_out = True
            logger.warning(
                f"CLI dispatch: timeout ({self._config.timeout_seconds}s) for "
                f"session {session_id!r}, agent {agent_name!r}"
            )
            # SIGTERM -> 1s grace -> SIGKILL
            # proc.terminate() sends SIGTERM on Unix; proc.kill() sends SIGKILL
            proc.terminate()
            try:
                post_stdout, post_stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=_SIGKILL_GRACE_SECONDS
                )
                stderr_raw = post_stderr.decode(errors="replace")
            except asyncio.TimeoutError:
                # Grace period expired -- escalate to SIGKILL
                proc.kill()
                try:
                    _, post_stderr = await proc.communicate()
                    stderr_raw = post_stderr.decode(errors="replace")
                except Exception:
                    pass
            except Exception:
                pass

        if _tmpdir_ctx is not None and timed_out:
            _tmpdir_ctx.cleanup()

        if timed_out:
            return _make_unknown_output(
                session_id=session_id,
                agent_name=agent_name,
                stderr=stderr_raw,
                attempts=attempt + 1,
            )

        # Check exit code
        if proc.returncode != 0:
            classified_failure = None
            decision = None
            failure_context: dict[str, Any] = {}
            if agent_name == "claude_code":
                evidence = _claude_code_adapter.extract_failure_evidence(
                    raw_stdout=stdout_raw,
                    stderr=stderr_raw,
                    exit_code=proc.returncode,
                )
                classified_failure = classify_output_failure(
                    RuntimeError(evidence.message or stderr_raw or stdout_raw),
                    evidence=evidence,
                )
                decision = decide_retry(
                    classified_failure,
                    attempt_number=attempt + 1,
                    max_attempts=max_attempts,
                    feedback_max_chars=self._config.retry.feedback_max_chars,
                )
                failure_context = classified_failure.details

            diagnostic_bits: list[str] = []
            if stderr_raw:
                diagnostic_bits.append(f"stderr: {stderr_raw[:200]!r}")
            if not stderr_raw and classified_failure is not None and classified_failure.error:
                diagnostic_bits.append(f"stdout message: {classified_failure.error[:200]!r}")
            if "api_error_status" in failure_context:
                diagnostic_bits.append(f"api_error_status={failure_context['api_error_status']!r}")
            if not diagnostic_bits:
                diagnostic_bits.append(f"stdout: {stdout_raw[:200]!r}")

            logger.warning(
                f"CLI dispatch: {agent_name!r} exited {proc.returncode} for "
                f"session {session_id!r}. {'; '.join(diagnostic_bits)}"
            )
            if _tmpdir_ctx is not None:
                _tmpdir_ctx.cleanup()

            reason = (
                decision.reason
                if decision is not None
                else FailureClass.FATAL_EXECUTION_ERROR.value
            )
            failure_class = (
                decision.failure_class.value
                if decision is not None
                else FailureClass.FATAL_EXECUTION_ERROR.value
            )
            retry_mode = decision.retry_mode.value if decision is not None else RetryMode.NONE.value
            error = classified_failure.error if classified_failure is not None else ""
            message = classified_failure.error if classified_failure is not None else ""
            return _make_failure_output(
                session_id=session_id,
                reason=reason,
                failure_class=failure_class,
                agent_name=agent_name,
                exit_code=proc.returncode,
                stderr=stderr_raw,
                error=error,
                retry_count=attempt,
                attempts=attempt + 1,
                retry_exhausted=False,
                retry_mode=retry_mode,
                message=message,
                extra_error_details=failure_context,
            )

        # For codex: read output from file
        if output_path is not None:
            try:
                stdout_raw = Path(output_path).read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.error(
                    f"CLI dispatch: codex output file unreadable at {output_path!r}: {exc}"
                )
                if _tmpdir_ctx is not None:
                    _tmpdir_ctx.cleanup()
                evidence = _codex_adapter.output_file_failure_evidence(
                    error=exc,
                    stderr=stderr_raw,
                )
                classified_failure = classify_output_failure(exc, evidence=evidence)
                decision = decide_retry(
                    classified_failure,
                    attempt_number=attempt + 1,
                    max_attempts=max_attempts,
                    feedback_max_chars=self._config.retry.feedback_max_chars,
                )
                return _make_failure_output(
                    session_id=session_id,
                    reason=decision.reason,
                    failure_class=decision.failure_class.value,
                    agent_name=agent_name,
                    stderr=stderr_raw,
                    retry_count=attempt,
                    error=classified_failure.error,
                    attempts=attempt + 1,
                    retry_exhausted=decision.reason == "retry_exhausted",
                    retry_mode=decision.retry_mode.value,
                    extra_error_details=classified_failure.details,
                )
            finally:
                # Cleanup temp directory (also cleans up the output file)
                if _tmpdir_ctx is not None:
                    _tmpdir_ctx.cleanup()
        elif _tmpdir_ctx is not None:
            _tmpdir_ctx.cleanup()

        # For claude: unwrap JSON envelope
        if agent_name == "claude_code":
            stdout_raw = _claude_code_adapter.extract_result(stdout_raw)

        # Normalize wrapper noise before validation.
        normalization = normalize_llm_json_text(stdout_raw)
        normalized_stdout = normalization.normalized_text
        if normalization.changed:
            logger.info(
                f"CLI dispatch: normalized output from {agent_name!r} "
                f"(session {session_id!r}, attempt {attempt + 1}, "
                f"strategy={normalization.strategy!r}, "
                f"raw_chars={len(stdout_raw)}, normalized_chars={len(normalized_stdout)})"
            )

        # Parse AnalysisOutput from normalized stdout
        if not normalized_stdout.strip():
            logger.warning(
                f"CLI dispatch: empty stdout from {agent_name!r} (session {session_id!r})"
            )
            classified_failure = classify_empty_output(source="stdout")
            decision = decide_retry(
                classified_failure,
                attempt_number=attempt + 1,
                max_attempts=max_attempts,
                feedback_max_chars=self._config.retry.feedback_max_chars,
            )
            return _make_failure_output(
                session_id=session_id,
                reason=decision.reason,
                failure_class=decision.failure_class.value,
                agent_name=agent_name,
                stderr=stderr_raw,
                error=classified_failure.error,
                retry_count=attempt,
                attempts=attempt + 1,
                retry_exhausted=decision.reason == "retry_exhausted",
                retry_mode=decision.retry_mode.value,
                extra_error_details=classified_failure.details,
                retry_feedback=decision.retry_feedback,
            )

        try:
            output = AnalysisOutput.model_validate_json(normalized_stdout)
            logger.info(
                f"CLI dispatch: success for session {session_id!r} via {agent_name!r} "
                f"(attempt {attempt + 1}, flags={len(output.behavior_flags)})"
            )
            # Capture stderr into error_details for forensics even on success.
            # error_details is None on clean success by cross-field convention,
            # but stderr is populated separately so forensics are available.
            # We embed stderr in a dict only when it has content -- empty stderr
            # produces a clean None error_details.
            if stderr_raw:
                return AnalysisOutput.model_validate(
                    {
                        **output.model_dump(),
                        "error_details": sanitize_error_details({"stderr": stderr_raw}),
                    }
                )
            return output
        except json.JSONDecodeError as exc:
            # Standalone json.JSONDecodeError (shouldn't fire since model_validate_json
            # wraps it in ValidationError, but kept as a belt-and-suspenders guard)
            classified_failure = classify_output_failure(exc)
            decision = decide_retry(
                classified_failure,
                attempt_number=attempt + 1,
                max_attempts=max_attempts,
                feedback_max_chars=self._config.retry.feedback_max_chars,
            )
            logger.warning(
                f"CLI dispatch: JSON decode error from {agent_name!r} "
                f"(session {session_id!r}, attempt {attempt + 1}): {exc}"
            )
            return _make_failure_output(
                session_id=session_id,
                reason=decision.reason,
                failure_class=decision.failure_class.value,
                agent_name=agent_name,
                stderr=stderr_raw,
                error=classified_failure.error,
                retry_count=attempt,
                attempts=attempt + 1,
                retry_exhausted=decision.reason == "retry_exhausted",
                retry_mode=decision.retry_mode.value,
                extra_error_details=classified_failure.details,
                retry_feedback=decision.retry_feedback,
            )
        except ValidationError as exc:
            classified_failure = classify_output_failure(exc)
            decision = decide_retry(
                classified_failure,
                attempt_number=attempt + 1,
                max_attempts=max_attempts,
                feedback_max_chars=self._config.retry.feedback_max_chars,
            )
            logger.log(
                "WARNING",
                f"CLI dispatch: {classified_failure.reason} from {agent_name!r} "
                f"(session {session_id!r}, attempt {attempt + 1}): {exc}",
            )
            return _make_failure_output(
                session_id=session_id,
                reason=decision.reason,
                failure_class=decision.failure_class.value,
                agent_name=agent_name,
                stderr=stderr_raw,
                error=classified_failure.error,
                retry_count=attempt,
                attempts=attempt + 1,
                retry_exhausted=decision.reason == "retry_exhausted",
                retry_mode=decision.retry_mode.value,
                extra_error_details=classified_failure.details,
                retry_feedback=decision.retry_feedback,
            )
        except Exception as exc:
            classified_failure = classify_output_failure(exc)
            decision = decide_retry(
                classified_failure,
                attempt_number=attempt + 1,
                max_attempts=max_attempts,
                feedback_max_chars=self._config.retry.feedback_max_chars,
            )
            logger.error(
                f"CLI dispatch: unexpected parse error from {agent_name!r} "
                f"(session {session_id!r}, attempt {attempt + 1}): {exc}"
            )
            return _make_failure_output(
                session_id=session_id,
                reason=decision.reason,
                failure_class=decision.failure_class.value,
                agent_name=agent_name,
                stderr=stderr_raw,
                error=classified_failure.error,
                retry_count=attempt,
                attempts=attempt + 1,
                retry_exhausted=decision.reason == "retry_exhausted",
                retry_mode=decision.retry_mode.value,
                extra_error_details=classified_failure.details,
                retry_feedback=decision.retry_feedback,
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _filter_env(env: dict[str, str]) -> dict[str, str]:
    """Filter SECONDSIGHT_* variables from an environment dict.

    Env filtering is centralized here (not in adapters) because:
    - Both adapters apply identical filtering
    - Centralizing removes duplicate logic and ghost-promise build_env() in adapters
    - Adapters are responsible for build_command() (and extract_result for claude) only

    The one intentional exception is the internal hook-suppression flag injected
    here. Analysis subprocesses must inherit it so agent-global hooks short-circuit
    and do not emit recursive session_end events for the analysis session itself.
    """
    filtered = {k: v for k, v in env.items() if not k.startswith("SECONDSIGHT_")}
    filtered[_HOOK_DISABLE_ENV_VAR] = "1"
    return filtered


def _augment_prompt_with_error(original_prompt: str, error_message: str) -> str:
    """Augment the prompt with the previous validation error for retry.

    The retry feedback is structured and bounded before being appended here.
    """
    return (
        original_prompt
        + "\n\n[IMPORTANT: Previous attempt failed with this error -- fix it]\n"
        + error_message
        + "\n\nReturn ONLY valid JSON, no markdown fences, no explanation.\n"
    )


def _make_failure_output(
    session_id: str,
    reason: str,
    agent_name: str,
    failure_class: str,
    exit_code: int | None = None,
    stderr: str = "",
    error: str = "",
    retry_count: int = 0,
    attempts: int = 1,
    retry_exhausted: bool = False,
    retry_mode: str = RetryMode.NONE.value,
    message: str = "",
    extra_error_details: dict[str, Any] | None = None,
    retry_feedback: str = "",
) -> AnalysisOutput:
    """Construct an AnalysisOutput with status='failure' and error_details.

    agent_name is REQUIRED (no default). Callers must pass the resolved agent name
    or _UNKNOWN_AGENT sentinel when the agent could not be resolved (e.g. state_missing).
    This prevents the "claude_code" lie when no agent was identified.
    """
    error_details = build_recovery_error_details(
        reason=reason,
        failure_class=failure_class,
        attempts=attempts,
        retry_exhausted=retry_exhausted,
        retry_mode=retry_mode,
        error=error,
        exit_code=exit_code,
        stderr=stderr,
        message=message,
        retry_feedback=retry_feedback,
        extra_error_details=extra_error_details,
    )

    return AnalysisOutput.model_validate(
        {
            "schema_version": "1.0",
            "session_id": session_id,
            "status": "failure",
            "behavior_flags": [],
            "session_summary": {
                "headline": "Analysis failed",
                "key_findings": [],
                "body": f"Dispatch failure: {reason}. {message}".strip(),
            },
            "dispatched_via": "cli",
            "cli_agent": agent_name,
            "primary_model": None,
            "fallback_used": False,
            "retry_count": min(retry_count, BUILTIN_ANALYSIS_MAX_RETRY_COUNT_CAP),
            "error_details": error_details,
        }
    )


def _make_unknown_output(
    session_id: str,
    agent_name: str,
    stderr: str = "",
    attempts: int = 1,
) -> AnalysisOutput:
    """Construct an AnalysisOutput with status='unknown' for timeout cases."""
    return AnalysisOutput.model_validate(
        {
            "schema_version": "1.0",
            "session_id": session_id,
            "status": "unknown",
            "behavior_flags": [],
            "session_summary": {
                "headline": "Analysis outcome unknown (timeout)",
                "key_findings": [],
                "body": "The CLI subprocess did not return within the configured timeout.",
            },
            "dispatched_via": "cli",
            "cli_agent": agent_name,
            "primary_model": None,
            "fallback_used": False,
            "retry_count": 0,
            "error_details": {
                "reason": FailureClass.TRANSPORT_TIMEOUT.value,
                "failure_class": FailureClass.TRANSPORT_TIMEOUT.value,
                "attempts": attempts,
                "retry_exhausted": False,
                "retry_mode": RetryMode.NONE.value,
                "stderr": sanitize_error_details(stderr),
            },
        }
    )


__all__ = [
    "CLIAnalysisDispatcher",
    "OpencodeNotSupportedError",
]
