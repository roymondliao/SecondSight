"""Claude Code CLI adapter for CLIAnalysisDispatcher (Task 4).

Invocation pattern (from PoC Variant 1 -- 10/10 probes pass):
    claude --print --output-format json --no-session-persistence [--model <model>] <prompt>

Output wrapping: claude --output-format json returns:
    {"type": "result", "subtype": "success", "result": "<actual text>", ...}
The actual LLM response text is in the "result" key.
Callers (CLIAnalysisDispatcher) must unwrap this envelope before parsing AnalysisOutput.

Assumptions:
    - claude binary is in PATH (caller is responsible for pre-check at startup)
    - claude auth is via browser OAuth or ~/.claude/settings.json (already configured)
    - --no-session-persistence prevents sessions from accumulating on disk across dispatches
    - Prompt is passed as the last positional argument (not via stdin)
      because claude ignores stdin in --print mode by default

Design decisions:
    - build_command() receives the fully-rendered prompt string.
      The dispatcher renders the jinja2 template and passes the result here.
      This keeps the adapter pure (no prompt logic).
    - model=None means "let claude use its own default" (Decision E5).
      Non-empty model means pass --model <value>.
    - Env filtering is centralized in the dispatcher (_filter_env), not here.
      Adapters own build_command() and extract_result() only.
      extract_result() is only on claude because codex writes to a file (no envelope).
"""

from __future__ import annotations

import json
from pathlib import Path

from secondsight.analysis.output_recovery import (
    EvidenceConfidence,
    ExecutorFailureEvidence,
    FailureClass,
)

_AUTH_MARKERS = (
    "api key",
    "authentication",
    "unauthorized",
    "forbidden",
    "credential",
    "invalid key",
)
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "quota",
    "usage limit",
    "monthly usage limit",
)


def build_command(
    model: str | None,
    prompt: str,
    project_root: Path,
) -> list[str]:
    """Build the claude CLI command for one-shot analysis dispatch.

    Args:
        model: Model override string, or None to use claude's own default.
        prompt: The fully-rendered analysis prompt to pass to claude.
        project_root: The project root directory (used by caller as cwd; not in command itself).

    Returns:
        list[str]: Command + arguments ready for asyncio.create_subprocess_exec(*cmd).

    Note:
        project_root is accepted as a parameter for interface symmetry with codex.build_command
        but is not embedded in the command -- it is used as cwd by the dispatcher.
    """
    cmd = ["claude", "--print", "--output-format", "json", "--no-session-persistence"]
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)
    return cmd


def extract_result(raw_stdout: str) -> str:
    """Extract the actual LLM response text from claude's JSON envelope.

    Claude --output-format json wraps the response in:
        {"type": "result", "subtype": "success", "result": "<text>", ...}

    Args:
        raw_stdout: Raw stdout from the claude subprocess.

    Returns:
        The "result" field value if envelope present; raw_stdout unchanged otherwise.
        Callers must still handle JSON parse errors after this extraction.
    """
    try:
        outer = json.loads(raw_stdout)
        if isinstance(outer, dict) and "result" in outer:
            return str(outer["result"])
    except json.JSONDecodeError, ValueError:
        pass
    # Fall through: not a JSON envelope or not the expected shape -- return as-is
    return raw_stdout


def extract_failure_evidence(
    *,
    raw_stdout: str,
    stderr: str,
    exit_code: int | None,
) -> ExecutorFailureEvidence:
    """Extract Claude-owned failure evidence from non-zero CLI output."""

    payload = _load_json_object(raw_stdout)
    raw: dict[str, object] = {"exit_code": exit_code}
    source = "cli_exit"
    message = stderr.strip() or raw_stdout.strip()

    if payload is not None:
        source = "cli_stdout_envelope"
        if payload.get("is_error") is True:
            raw["cli_reported_error"] = True
        if "api_error_status" in payload:
            raw["api_error_status"] = payload["api_error_status"]
        if "subtype" in payload:
            raw["subtype"] = payload["subtype"]
        result_text = str(payload.get("result", "")).strip()
        if result_text:
            message = result_text[:500]
            raw["message"] = message
    elif stderr.strip():
        source = "cli_stderr"

    failure_class, reason, confidence = _classify_claude_evidence(raw=raw, message=message)
    return ExecutorFailureEvidence(
        source=source,
        executor="claude_code",
        failure_class=failure_class,
        reason=reason,
        message=message[:500],
        raw=raw,
        confidence=confidence,
    )


def _load_json_object(raw_stdout: str) -> dict[str, object] | None:
    try:
        payload = json.loads(raw_stdout)
    except json.JSONDecodeError:
        return None
    except TypeError, ValueError:
        return None
    if isinstance(payload, dict):
        return payload
    return None


def _classify_claude_evidence(
    *,
    raw: dict[str, object],
    message: str,
) -> tuple[FailureClass, str, EvidenceConfidence]:
    status = raw.get("api_error_status")
    if status in {401, 403}:
        return (
            FailureClass.FATAL_AUTH_OR_CONFIG,
            FailureClass.FATAL_AUTH_OR_CONFIG.value,
            EvidenceConfidence.DERIVED,
        )
    if status == 429:
        return (
            FailureClass.TRANSPORT_RATE_LIMIT,
            FailureClass.TRANSPORT_RATE_LIMIT.value,
            EvidenceConfidence.DERIVED,
        )

    lower_message = message.lower()
    if any(marker in lower_message for marker in _AUTH_MARKERS):
        return (
            FailureClass.FATAL_AUTH_OR_CONFIG,
            FailureClass.FATAL_AUTH_OR_CONFIG.value,
            EvidenceConfidence.HEURISTIC,
        )
    if any(marker in lower_message for marker in _RATE_LIMIT_MARKERS):
        return (
            FailureClass.TRANSPORT_RATE_LIMIT,
            FailureClass.TRANSPORT_RATE_LIMIT.value,
            EvidenceConfidence.HEURISTIC,
        )
    return (
        FailureClass.FATAL_EXECUTION_ERROR,
        FailureClass.FATAL_EXECUTION_ERROR.value,
        EvidenceConfidence.UNKNOWN,
    )


__all__ = ["build_command", "extract_failure_evidence", "extract_result"]
