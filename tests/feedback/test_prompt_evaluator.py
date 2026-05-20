"""Death-first tests for the UserPromptSubmit ambiguity evaluator."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from secondsight.config.schema import (
    AnalysisCLIConfig,
    AnalysisCLIModelsConfig,
    AnalysisConfig,
    GeneralConfig,
)
from secondsight.feedback.prompt_evaluator import (
    PromptEvaluation,
    PromptEvaluationDecision,
    evaluate_user_prompt,
    parse_evaluator_output,
)
from secondsight.feedback.prompt_guidance import PromptHitCategory


def _cli_config(timeout_seconds: int = 1) -> AnalysisConfig:
    return AnalysisConfig(
        timeout_seconds=timeout_seconds,
        cli=AnalysisCLIConfig(
            default_agent="claude_code",
            models=AnalysisCLIModelsConfig(),
        ),
    )


def _proc(
    *,
    stdout: str,
    stderr: str = "",
    returncode: int = 0,
    hang: bool = False,
) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    proc.terminate = MagicMock()
    proc.kill = MagicMock()
    if hang:
        proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
    else:
        proc.communicate = AsyncMock(return_value=(stdout.encode(), stderr.encode()))
    return proc


# ===========================================================================
# DEATH TESTS
# ===========================================================================


@pytest.mark.asyncio
async def test_dt_cli_mode_evaluator_subprocess_forces_hook_disable_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DC2: CLI classification subprocesses must not run hook-enabled."""
    captured_env: dict[str, str] | None = None

    async def fake_create_subprocess_exec(*args, **kwargs):
        nonlocal captured_env
        captured_env = kwargs["env"]
        return _proc(stdout=json.dumps({"decision": "pass", "primary_category": None}))

    monkeypatch.setattr(
        "secondsight.feedback.prompt_evaluator.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )
    monkeypatch.setenv("SECONDSIGHT_DISABLE_HOOKS", "0")
    monkeypatch.setenv("SECONDSIGHT_PORT", "8420")

    result = await evaluate_user_prompt(
        prompt="fix this",
        mode_config=GeneralConfig(mode="cli"),
        analysis_config=_cli_config(),
        project_root=tmp_path,
        session_id="sess-1",
    )

    assert result.decision == PromptEvaluationDecision.PASS
    assert captured_env is not None
    assert captured_env.get("SECONDSIGHT_DISABLE_HOOKS") == "1"
    leaked = {
        key: value
        for key, value in captured_env.items()
        if key.startswith("SECONDSIGHT_") and key != "SECONDSIGHT_DISABLE_HOOKS"
    }
    assert leaked == {}


@pytest.mark.asyncio
async def test_dt_malformed_evaluator_json_fails_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DC3: malformed classifier output must produce no guidance, not a block."""

    async def fake_create_subprocess_exec(*args, **kwargs):
        return _proc(stdout="not json")

    monkeypatch.setattr(
        "secondsight.feedback.prompt_evaluator.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    result = await evaluate_user_prompt(
        prompt="fix it",
        mode_config=GeneralConfig(mode="cli"),
        analysis_config=_cli_config(),
        project_root=tmp_path,
        session_id=None,
    )

    assert result == PromptEvaluation.pass_open(reason="malformed_output")


@pytest.mark.asyncio
async def test_dt_evaluator_timeout_fails_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DC3: evaluator timeout must fail open instead of blocking the prompt."""
    proc = _proc(stdout="", hang=True)

    async def fake_create_subprocess_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(
        "secondsight.feedback.prompt_evaluator.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    result = await evaluate_user_prompt(
        prompt="fix it",
        mode_config=GeneralConfig(mode="cli"),
        analysis_config=_cli_config(timeout_seconds=1),
        project_root=tmp_path,
        session_id="sess-timeout",
    )

    assert result == PromptEvaluation.pass_open(reason="timeout")
    proc.terminate.assert_called_once()
    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_dt_cli_auto_without_resolved_state_fails_open_before_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI auto must not silently route prompt classification to the wrong agent."""
    spawned = False

    async def fake_create_subprocess_exec(*args, **kwargs):
        nonlocal spawned
        spawned = True
        return _proc(stdout=json.dumps({"decision": "pass", "primary_category": None}))

    monkeypatch.setattr(
        "secondsight.feedback.prompt_evaluator.asyncio.create_subprocess_exec",
        fake_create_subprocess_exec,
    )

    result = await evaluate_user_prompt(
        prompt="fix it",
        mode_config=GeneralConfig(mode="cli"),
        analysis_config=AnalysisConfig(
            timeout_seconds=1,
            cli=AnalysisCLIConfig(
                default_agent="auto",
                models=AnalysisCLIModelsConfig(),
            ),
        ),
        project_root=tmp_path,
        session_id="sess-auto",
    )

    assert result == PromptEvaluation.pass_open(reason="state_missing")
    assert spawned is False


# ===========================================================================
# UNIT TESTS
# ===========================================================================


def test_parse_evaluator_output_accepts_intervention_category() -> None:
    result = parse_evaluator_output(
        json.dumps(
            {
                "decision": "intervene",
                "primary_category": "missing_scope",
            }
        )
    )

    assert result.decision == PromptEvaluationDecision.INTERVENE
    assert result.primary_category == PromptHitCategory.MISSING_SCOPE


def test_parse_evaluator_output_uncertain_or_invalid_category_fails_open() -> None:
    assert parse_evaluator_output('{"decision":"pass","primary_category":null}').decision == (
        PromptEvaluationDecision.PASS
    )
    assert parse_evaluator_output(
        '{"decision":"intervene","primary_category":"unknown"}'
    ) == PromptEvaluation.pass_open(reason="malformed_output")
