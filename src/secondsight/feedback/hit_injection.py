"""Hit injection wrapper helper (task-2).

Renders the executability self-check meta-instruction that the main agent will
evaluate as part of its own inference, replacing the sidecar-classifier approach.

Design contract:
- render_wrapper returns "" for empty/whitespace-only prompts — no meta-instruction
  surface when there is nothing to wrap.
- render_wrapper raises on any rendering failure — fail-open semantics belong to
  the calling shell hook (task-3), not here.
- Zero subprocess, zero external classifier: hit detection is a property of the
  main agent's own reasoning, invoked via UserPromptSubmit hook additionalContext.
"""

from __future__ import annotations

from secondsight.prompts._loader import render

# S-1: module-level constant to avoid magic string at the call site.
_TEMPLATE_NAME = "feedback/hit_injection_wrapper"
_AGENT_BYPASS_PREFIXES: dict[str, tuple[str, ...]] = {
    # Claude Code keeps the prompt-improver-style slash + memorize bypasses.
    "claude_code": ("/", "#"),
    # Codex also exposes slash-command style prompts; keep this small until
    # we have a verified broader control-surface contract for Codex.
    "codex": ("/", "$"),
    # OpenCode has no verified bypass prefixes in this codebase yet.
    "opencode": (),
}


def should_bypass_wrapper(prompt: str, agent: str | None) -> bool:
    """Return True when this prompt should skip hit-injection wrapping.

    This is intentionally conservative: only known agent-control prefixes are
    bypassed.  The hook then emits no additionalContext so the original prompt
    proceeds unchanged through the agent's native command surface.
    """
    if not prompt:
        return False
    normalized_agent = (agent or "").strip().lower()
    prefixes = _AGENT_BYPASS_PREFIXES.get(normalized_agent, ())
    return any(prompt.startswith(prefix) for prefix in prefixes)


def render_wrapper(prompt: str) -> str:
    """Render the executability self-check wrapper around the given user prompt.

    Args:
        prompt: The raw user prompt text submitted via UserPromptSubmit.

    Returns:
        Rendered wrapper string ready for use as ``additionalContext`` in the
        hook's ``hookSpecificOutput`` payload.  Returns the empty string for
        empty or whitespace-only prompts — the hook contract for "no injection".

        Note: the empty-string → "no injection" contract is assumed and must
        be confirmed by the task-3 implementer at hook integration time.

        IMPORTANT for callers (shell hook, task-3): the rendered string may
        contain double quotes, backslashes, and newlines.  The caller MUST
        serialise this string using a proper JSON encoder (e.g. ``json.dumps``
        in Python, or ``jq --arg`` in shell) — never via naive string
        interpolation or printf-based concatenation.  DT-1 in
        ``tests/feedback/test_hit_injection.py`` pins this contract.

    Raises:
        jinja2.exceptions.TemplateError: any failure in template loading or
            rendering propagates uncaught.  Subclasses include TemplateNotFound
            (template missing from package), UndefinedError (template references
            a context variable not provided), and TemplateSyntaxError (template
            has malformed Jinja2 syntax — possible after a template edit).
            The shell hook (task-3) is responsible for fail-open handling.
    """
    if not prompt.strip():
        return ""
    result = render(_TEMPLATE_NAME, context={"prompt": prompt})
    if not result:
        raise RuntimeError(
            "hit_injection_wrapper template rendered empty output for a "
            "non-empty prompt; template contract is violated"
        )
    return result
