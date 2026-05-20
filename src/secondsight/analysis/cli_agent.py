"""CLI-backed AnalysisAgent implementation for per-segment analysis.

This adapter lets the documented orchestrator pipeline (segment -> summary ->
aggregate) run in CLI mode without collapsing the whole session into one
one-shot prompt. Each protocol method renders its own prompt and asks the
configured coding-agent CLI to return JSON matching the requested schema.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from collections.abc import Sequence
from pathlib import Path
from typing import TypeVar

from loguru import logger
from pydantic import BaseModel, ValidationError

from secondsight.analysis.agent import AnalysisAgentError
from secondsight.analysis.cli_dispatcher import (
    _CLI_ADAPTERS,
    _augment_prompt_with_error,
    _filter_env,
)
from secondsight.analysis.output_recovery import normalize_llm_json_text
from secondsight.analysis.prompts.aggregate import AggregateOutput
from secondsight.analysis.prompts.summary import SummaryOutput
from secondsight.analysis.schemas import SegmentAnalysis
from secondsight.config.schema import AnalysisConfig
from secondsight.state import SecondSightState

_OutputT = TypeVar("_OutputT", bound=BaseModel)
_SIGKILL_GRACE_SECONDS: float = 1.0


class CLIAnalysisAgent:
    """AnalysisAgent protocol implementation using the coding-agent CLI."""

    def __init__(
        self,
        *,
        config: AnalysisConfig,
        state: SecondSightState | None,
        project_root: Path,
    ) -> None:
        self._config = config
        self._state = state
        self._project_root = project_root

    async def analyze_segments(
        self,
        prompts: Sequence[str],
    ) -> list[SegmentAnalysis]:
        results: list[SegmentAnalysis] = []
        for i, prompt in enumerate(prompts):
            try:
                results.append(await self._run_prompt(prompt, SegmentAnalysis))
            except AnalysisAgentError as exc:
                exc.add_note(f"batch failed at prompt index {i}")
                raise
        return results

    async def aggregate_flag_type(self, prompt: str) -> AggregateOutput:
        return await self._run_prompt(prompt, AggregateOutput)

    async def summarize_session(self, prompt: str) -> SummaryOutput:
        return await self._run_prompt(prompt, SummaryOutput)

    def _resolve_agent(self) -> tuple[str, str | None]:
        default_agent = self._config.cli.default_agent
        if default_agent == "auto":
            if self._state is None:
                raise AnalysisAgentError(
                    "CLI analysis requires state.json when analysis.cli.default_agent='auto'. "
                    "Run `secondsight init` or set [analysis.cli].default_agent explicitly."
                )
            agent_name = self._state.init_agent
        else:
            agent_name = default_agent

        if agent_name == "opencode":
            raise AnalysisAgentError(
                "opencode is not supported for CLI analysis dispatch. Use claude_code or codex."
            )

        adapter = _CLI_ADAPTERS.get(agent_name)
        if adapter is None:
            raise AnalysisAgentError(
                f"Unsupported CLI agent {agent_name!r}. Check [analysis.cli].default_agent."
            )

        raw_model = getattr(self._config.cli.models, adapter.model_config_field, "")
        model_override = raw_model or None
        return agent_name, model_override

    async def _run_prompt(
        self,
        prompt: str,
        output_type: type[_OutputT],
    ) -> _OutputT:
        agent_name, model_override = self._resolve_agent()
        max_attempts = (
            self._config.retry.output_repair_max_attempts + 1 if self._config.retry.enabled else 1
        )
        base_prompt = prompt
        current_prompt = prompt
        last_error = ""

        for attempt_number in range(1, max_attempts + 1):
            raw_output = await self._run_once(
                agent_name=agent_name,
                model_override=model_override,
                prompt=current_prompt,
            )
            normalization = normalize_llm_json_text(raw_output)
            normalized = normalization.normalized_text
            if normalization.changed:
                logger.info(
                    "CLI analysis agent: normalized output from {!r} for {} "
                    "(attempt {}/{}, strategy={!r}, raw_chars={}, normalized_chars={})",
                    agent_name,
                    output_type.__name__,
                    attempt_number,
                    max_attempts,
                    normalization.strategy,
                    len(raw_output),
                    len(normalized),
                )

            if not normalized.strip():
                last_error = "Empty output from CLI analysis agent."
            else:
                try:
                    return output_type.model_validate_json(normalized)
                except ValidationError as exc:
                    last_error = str(exc)

            if attempt_number < max_attempts:
                feedback = last_error[: self._config.retry.feedback_max_chars]
                current_prompt = _augment_prompt_with_error(base_prompt, feedback)
                continue

            raise AnalysisAgentError(
                f"CLI analysis agent failed to produce valid {output_type.__name__} "
                f"after {max_attempts} attempt(s): {last_error}"
            )

        raise AnalysisAgentError(
            f"CLI analysis agent exhausted retries without returning {output_type.__name__}."
        )

    async def _run_once(
        self,
        *,
        agent_name: str,
        model_override: str | None,
        prompt: str,
    ) -> str:
        env = _filter_env(os.environ.copy())
        adapter = _CLI_ADAPTERS[agent_name]

        output_path: str | None = None
        tmpdir_ctx: tempfile.TemporaryDirectory[str] | None = None
        if adapter.output_mode == "file":
            tmpdir_ctx = tempfile.TemporaryDirectory(prefix="secondsight_cli_agent_")
            tmpdir = Path(tmpdir_ctx.name)
            cmd, output_path = adapter.build_command(
                model=model_override,
                prompt=prompt,
                project_root=tmpdir,
            )
            stdin_bytes = prompt.encode()
        else:
            cmd = adapter.build_command(
                model=model_override,
                prompt=prompt,
                project_root=self._project_root,
            )
            stdin_bytes = None

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._project_root),
                env=env,
            )
        except FileNotFoundError as exc:
            if tmpdir_ctx is not None:
                tmpdir_ctx.cleanup()
            raise AnalysisAgentError(f"CLI binary not found for {agent_name!r}: {exc}") from exc

        stdout_raw = ""
        stderr_raw = ""
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes),
                timeout=self._config.timeout_seconds,
            )
            stdout_raw = stdout_bytes.decode(errors="replace")
            stderr_raw = stderr_bytes.decode(errors="replace")
        except asyncio.TimeoutError as exc:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.communicate(), timeout=_SIGKILL_GRACE_SECONDS)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.communicate()
            finally:
                if tmpdir_ctx is not None:
                    tmpdir_ctx.cleanup()
            raise AnalysisAgentError(
                f"CLI analysis agent timed out after {self._config.timeout_seconds}s "
                f"for {agent_name!r}."
            ) from exc

        if proc.returncode != 0:
            if tmpdir_ctx is not None:
                tmpdir_ctx.cleanup()
            detail = stderr_raw.strip() or stdout_raw.strip() or f"exit code {proc.returncode}"
            raise AnalysisAgentError(
                f"CLI analysis agent {agent_name!r} exited {proc.returncode}: {detail[:500]}"
            )

        try:
            if output_path is not None:
                return Path(output_path).read_text(encoding="utf-8", errors="replace")

            if adapter.extract_result is not None:
                return adapter.extract_result(stdout_raw)
            return stdout_raw
        finally:
            if tmpdir_ctx is not None:
                tmpdir_ctx.cleanup()


__all__ = ["CLIAnalysisAgent"]
