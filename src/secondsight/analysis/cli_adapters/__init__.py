"""CLI adapters for CLIAnalysisDispatcher (Task 4).

Each adapter module implements:
    build_command(model, prompt, project_root) -> list[str]       (claude_code)
    build_command(model, prompt, project_root) -> tuple[list, str] (codex)
    extract_result(raw_stdout) -> str                             (claude_code only)

Adapters do NOT implement build_env. Env filtering is centralized in
CLIAnalysisDispatcher._filter_env() to eliminate duplicate code.
The extract_result asymmetry is intentional: claude wraps output in a JSON
envelope, codex writes plain text to a file. Adapters own only what differs.

The dispatcher imports the adapters directly by name rather than via a registry
to maintain explicit, static-analysis-friendly imports. See SUPPORTED_AGENTS for
the set of supported agent names.

Adding a new adapter:
    1. Create src/secondsight/analysis/cli_adapters/<name>.py
    2. Add SUPPORTED_AGENTS entry
    3. Update CLIAnalysisDispatcher._run_once() with the new agent branch
"""

from __future__ import annotations

# Supported agent names -- used by Task 6 pre-check to validate default_agent
SUPPORTED_AGENTS: frozenset[str] = frozenset({"claude_code", "codex"})

__all__ = ["SUPPORTED_AGENTS"]
