"""Codex CLI adapter for CLIAnalysisDispatcher (Task 4).

Invocation pattern (from PoC Variant 1 -- 10/10 probes pass):
    codex exec --ephemeral -o <output_file> [--model <model>] -

Codex reads prompt from stdin (the '-' sentinel) and writes its last message
to the file specified by -o / --output-last-message.

This is fundamentally different from claude:
    - claude: prompt as positional arg, output via stdout
    - codex:  prompt via stdin (close after write), output via file path

Output file: the dispatcher must read the output_path file after the subprocess
exits to get the LLM response text. The path is returned by build_command() as
the second element of a (cmd, output_path) tuple.

Design decisions:
    - --ephemeral: prevents session files accumulating across dispatches.
    - Output file is placed in a system temp directory (NOT project_root) to
      avoid polluting the user's working tree. The dispatcher passes a tmpdir
      as project_root to build_command(). Cleanup is via TemporaryDirectory.
    - Stdin is closed after write (process.communicate handles this correctly).
    - Env filtering is centralized in the dispatcher (_filter_env), not here.
      Adapters own build_command() only (no extract_result -- codex writes plain text).

Codex stdin deadlock note (from task spec):
    Use process.communicate(input=prompt_bytes) rather than
    proc.stdin.write() + proc.stdin.close() to avoid the write-deadlock
    where the subprocess blocks waiting for output to be consumed while
    the parent blocks waiting for the subprocess to read from stdin.
"""

from __future__ import annotations

import uuid
from pathlib import Path


def build_command(
    model: str | None,
    prompt: str,
    project_root: Path,
) -> tuple[list[str], str]:
    """Build the codex CLI command for one-shot analysis dispatch.

    Args:
        model: Model override string, or None to use codex's own default.
        prompt: The fully-rendered analysis prompt. Passed via stdin by the dispatcher.
        project_root: Directory where the output file will be written.
            The dispatcher passes a system temp directory here (not the user's project
            root) to avoid polluting the working tree.

    Returns:
        tuple[list[str], str]: (command_list, output_file_path)
            - command_list: ready for asyncio.create_subprocess_exec(*cmd)
            - output_file_path: path where codex will write the last message;
              dispatcher reads this file after subprocess exits.

    Note:
        The prompt is NOT embedded in the command (codex reads from stdin via '-').
        The caller must pass prompt as stdin via process.communicate(input=prompt_bytes).
    """
    output_filename = f"codex-output-{uuid.uuid4().hex}.txt"
    output_path = str(project_root / output_filename)

    cmd = ["codex", "exec", "--ephemeral", "--skip-git-repo-check", "-o", output_path]
    if model:
        cmd.extend(["-m", model])
    cmd.append("-")  # Read prompt from stdin

    return cmd, output_path


__all__ = ["build_command"]
