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


def test_dt_parse_evaluator_output_handles_markdown_fenced_json() -> None:
    """Death test for bugfix/2026-05-20_user-prompt-injection-timeout Layer 3.

    Observed real outputs from `claude --print --model claude-haiku-4-5-20251001`
    under the classifier prompt (see root-cause.yaml profiling section):

        '```json\\n{"decision":"intervene","primary_category":"missing_target"}\\n```'
        '```json\\n{\\n  "decision": "intervene",\\n  "primary_category": "missing_scope"\\n}\\n```\\n\\n**Clar...'

    Haiku consistently wraps its JSON in markdown code fences and sometimes
    appends trailing prose ("**Clarification:** ..."). parse_evaluator_output
    currently calls json.loads() directly on this text — JSONDecodeError →
    pass_open(reason="malformed_output"). Result: even when Layer 1 (hook
    timeout) and Layer 2 (CLI subprocess latency) are resolved, every real
    INTERVENE verdict is silently downgraded to PASS.

    This test pins the contract: when the classifier returns a fenced JSON
    body whose decoded payload is a valid intervene verdict, parse_evaluator_output
    must produce INTERVENE — not malformed_output.

    Currently FAILS on the unmodified parser. Fixed by adding fence-stripping
    to parse_evaluator_output (or by switching the classifier output format).
    """
    fenced_intervene = '```json\n{"decision":"intervene","primary_category":"missing_target"}\n```'
    result = parse_evaluator_output(fenced_intervene)
    assert result.decision == PromptEvaluationDecision.INTERVENE, (
        "Real haiku-4-5 classifier output is markdown-fenced; parser silently "
        "downgrades it to PASS via JSONDecodeError → pass_open. Strip fences "
        "in parse_evaluator_output, or change the classifier output contract."
    )
    assert result.primary_category == PromptHitCategory.MISSING_TARGET

    # Second variant: trailing prose after the closing fence (also observed).
    fenced_with_trailing_prose = (
        "```json\n"
        '{\n  "decision": "intervene",\n  "primary_category": "missing_scope"\n}\n'
        "```\n\n**Clarification:** the user prompt is too short."
    )
    result2 = parse_evaluator_output(fenced_with_trailing_prose)
    assert result2.decision == PromptEvaluationDecision.INTERVENE
    assert result2.primary_category == PromptHitCategory.MISSING_SCOPE


def test_dt_classifier_prompt_is_loaded_from_prompts_folder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classifier instructions must live in prompts/, not in evaluator code."""
    from secondsight.feedback import prompt_evaluator

    calls: list[tuple[str, dict]] = []

    def fake_render(template_name: str, context: dict) -> str:
        calls.append((template_name, context))
        return "classifier prompt"

    monkeypatch.setattr(prompt_evaluator, "render", fake_render)

    assert prompt_evaluator._build_classifier_prompt("fix it") == "classifier prompt"  # noqa: SLF001
    assert calls == [("feedback/classifier", {"prompt": "fix it"})]
