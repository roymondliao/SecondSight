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
    except (json.JSONDecodeError, ValueError):
        pass
    # Fall through: not a JSON envelope or not the expected shape -- return as-is
    return raw_stdout


__all__ = ["build_command", "extract_result"]
