"""Mode-aware ambiguity evaluator for UserPromptSubmit hit guidance."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from loguru import logger

from secondsight.analysis.cli_adapters import claude_code as _claude_code_adapter
from secondsight.analysis.cli_adapters import codex as _codex_adapter
from secondsight.analysis.cli_dispatcher import _filter_env
from secondsight.config.loader import _resolve_provider_keys
from secondsight.config.schema import (
    AnalysisConfig,
    GeneralConfig,
    ProvidersConfig,
)
from secondsight.feedback.prompt_guidance import PromptHitCategory
from secondsight.prompts._loader import render

_SIGKILL_GRACE_SECONDS: float = 1.0


class PromptEvaluationDecision(StrEnum):
    """Closed evaluator decision set."""

    PASS = "pass"
    INTERVENE = "intervene"


@dataclass(frozen=True)
class PromptEvaluation:
    """Normalized evaluator result used by the injection route."""

    decision: PromptEvaluationDecision
    primary_category: PromptHitCategory | None
    failure_reason: str | None = None

    @classmethod
    def pass_open(cls, *, reason: str) -> "PromptEvaluation":
        return cls(
            decision=PromptEvaluationDecision.PASS,
            primary_category=None,
            failure_reason=reason,
        )


def _build_classifier_prompt(prompt: str) -> str:
    return render("feedback/classifier", context={"prompt": prompt})


def parse_evaluator_output(raw_output: str) -> PromptEvaluation:
    """Parse classifier JSON, failing open on malformed or unsupported output."""
    try:
        payload = json.loads(raw_output)
    except json.JSONDecodeError:
        return PromptEvaluation.pass_open(reason="malformed_output")
    if not isinstance(payload, dict):
        return PromptEvaluation.pass_open(reason="malformed_output")

    raw_decision = payload.get("decision")
    try:
        decision = PromptEvaluationDecision(str(raw_decision))
    except ValueError:
        return PromptEvaluation.pass_open(reason="malformed_output")

    raw_category = payload.get("primary_category")
    if decision is PromptEvaluationDecision.PASS:
        if raw_category is not None:
            return PromptEvaluation.pass_open(reason="malformed_output")
        return PromptEvaluation(decision=decision, primary_category=None)

    if raw_category is None:
        return PromptEvaluation.pass_open(reason="malformed_output")
    try:
        category = PromptHitCategory(str(raw_category))
    except ValueError:
        return PromptEvaluation.pass_open(reason="malformed_output")
    return PromptEvaluation(decision=decision, primary_category=category)


async def evaluate_user_prompt(
    *,
    prompt: str,
    mode_config: GeneralConfig,
    analysis_config: AnalysisConfig,
    project_root: Path,
    session_id: str | None,
    providers_config: ProvidersConfig | None = None,
    resolved_cli_agent: str | None = None,
) -> PromptEvaluation:
    """Classify a user prompt according to the configured runtime mode.

    This function is deliberately exception-free for callers: provider errors,
    CLI failures, malformed JSON, and timeouts all degrade to ``pass`` so the
    user prompt continues without guidance.
    """
    try:
        if mode_config.mode == "cli":
            return await _evaluate_via_cli(
                prompt=prompt,
                analysis_config=analysis_config,
                project_root=project_root,
                session_id=session_id,
                resolved_cli_agent=resolved_cli_agent,
            )
        if mode_config.mode == "sdk":
            return await _evaluate_via_sdk(
                prompt=prompt,
                analysis_config=analysis_config,
                providers_config=providers_config,
                session_id=session_id,
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Prompt evaluator failed open: mode={mode} session_id={session_id} error={err}",
            mode=mode_config.mode,
            session_id=session_id,
            err=exc,
        )
        return PromptEvaluation.pass_open(reason="provider_failure")

    logger.warning("Prompt evaluator unknown mode failed open: mode={mode}", mode=mode_config.mode)
    return PromptEvaluation.pass_open(reason="invalid_mode")


async def _evaluate_via_cli(
    *,
    prompt: str,
    analysis_config: AnalysisConfig,
    project_root: Path,
    session_id: str | None,
    resolved_cli_agent: str | None,
) -> PromptEvaluation:
    classifier_prompt = _build_classifier_prompt(prompt)
    agent_name = analysis_config.cli.default_agent
    if agent_name == "auto":
        if not resolved_cli_agent:
            return PromptEvaluation.pass_open(reason="state_missing")
        agent_name = resolved_cli_agent
    model = _model_override_for_agent(analysis_config, agent_name)
    env = _filter_env(os.environ.copy())

    output_path: str | None = None
    tmpdir_ctx: tempfile.TemporaryDirectory[str] | None = None
    stdin_bytes: bytes | None = None
    extract_result = None

    if agent_name == "claude_code":
        cmd = _claude_code_adapter.build_command(
            model=model,
            prompt=classifier_prompt,
            project_root=project_root,
        )
        extract_result = _claude_code_adapter.extract_result
    elif agent_name == "codex":
        tmpdir_ctx = tempfile.TemporaryDirectory(prefix="secondsight_prompt_eval_")
        cmd, output_path = _codex_adapter.build_command(
            model=model,
            prompt=classifier_prompt,
            project_root=Path(tmpdir_ctx.name),
        )
        stdin_bytes = classifier_prompt.encode()
    else:
        return PromptEvaluation.pass_open(reason="unsupported_cli_agent")

    try:
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else None,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(project_root),
                env=env,
            )
        except (FileNotFoundError, OSError) as exc:
            logger.warning(
                "Prompt evaluator CLI spawn failed open: agent={agent} error={err}",
                agent=agent_name,
                err=exc,
            )
            return PromptEvaluation.pass_open(reason="provider_failure")

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=stdin_bytes),
                timeout=analysis_config.timeout_seconds,
            )
        except asyncio.TimeoutError:
            await _terminate_process_after_timeout(proc)
            logger.warning(
                "Prompt evaluator CLI timeout failed open: session_id={session_id}",
                session_id=session_id,
            )
            return PromptEvaluation.pass_open(reason="timeout")

        stdout = stdout_bytes.decode(errors="replace")
        stderr = stderr_bytes.decode(errors="replace")
        if proc.returncode != 0:
            logger.warning(
                "Prompt evaluator CLI exited non-zero and failed open: agent={agent} "
                "returncode={returncode} stderr={stderr}",
                agent=agent_name,
                returncode=proc.returncode,
                stderr=stderr[:300],
            )
            return PromptEvaluation.pass_open(reason="provider_failure")

        if output_path is not None:
            try:
                stdout = Path(output_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return PromptEvaluation.pass_open(reason="provider_failure")
        elif extract_result is not None:
            stdout = extract_result(stdout)

        return parse_evaluator_output(stdout)
    finally:
        if tmpdir_ctx is not None:
            tmpdir_ctx.cleanup()


async def _evaluate_via_sdk(
    *,
    prompt: str,
    analysis_config: AnalysisConfig,
    providers_config: ProvidersConfig | None,
    session_id: str | None,
) -> PromptEvaluation:
    if providers_config is None:
        return PromptEvaluation.pass_open(reason="provider_failure")
    try:
        from secondsight.sdk._specs import ModelSpec
        from secondsight.sdk.model_selection import _infer_provider
        from secondsight.sdk.router import LLMRouter
    except ImportError as exc:
        logger.warning("Prompt evaluator SDK imports failed open: {err}", err=exc)
        return PromptEvaluation.pass_open(reason="provider_failure")

    try:
        resolved_keys = _resolve_provider_keys(providers_config)
        primary_model = analysis_config.sdk.primary_model
        if not primary_model:
            return PromptEvaluation.pass_open(reason="provider_failure")
        primary = ModelSpec(name=primary_model, provider=_infer_provider(primary_model))
        fallbacks: list[ModelSpec] = []
        if analysis_config.sdk.fallback_model:
            fallback_model = analysis_config.sdk.fallback_model
            fallbacks.append(
                ModelSpec(name=fallback_model, provider=_infer_provider(fallback_model))
            )
        router = LLMRouter(
            primary=primary,
            fallbacks=fallbacks,
            resolved_keys=resolved_keys,
            per_call_timeout_s=float(analysis_config.timeout_seconds),
            chain_total_timeout_s=float(analysis_config.timeout_seconds) * 1.5,
        )
        output = await router.call(
            model_input=_build_classifier_prompt(prompt),
            output_type=dict[str, Any],
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Prompt evaluator SDK timeout failed open: session_id={session_id}",
            session_id=session_id,
        )
        return PromptEvaluation.pass_open(reason="timeout")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Prompt evaluator SDK provider failed open: {err}", err=exc)
        return PromptEvaluation.pass_open(reason="provider_failure")

    return parse_evaluator_output(json.dumps(output))


def _model_override_for_agent(analysis_config: AnalysisConfig, agent_name: str) -> str | None:
    if agent_name == "claude_code" and analysis_config.cli.models.claude_code:
        return analysis_config.cli.models.claude_code
    if agent_name == "codex" and analysis_config.cli.models.codex:
        return analysis_config.cli.models.codex
    return None


async def _terminate_process_after_timeout(proc: Any) -> None:
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    except Exception:
        return
    try:
        await asyncio.wait_for(proc.communicate(), timeout=_SIGKILL_GRACE_SECONDS)
        return
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            return
        except Exception:
            return
        try:
            await proc.communicate()
        except Exception:
            return
    except Exception:
        return


__all__ = [
    "PromptEvaluation",
    "PromptEvaluationDecision",
    "evaluate_user_prompt",
    "parse_evaluator_output",
]
