"""Death-first tests for the hit injection wrapper helper (task-2).

Execution order per samsara: DT-1/DT-2/DT-3 first (silent failure paths),
then unit tests.
"""

from __future__ import annotations

import json

import jinja2
import pytest

# ===========================================================================
# DEATH TESTS
# ===========================================================================


def test_dt_wrapper_handles_json_unsafe_characters() -> None:
    """DT-1: Quotes / backslashes / newlines in prompt must survive JSON round-trip.

    Silent failure: if template variable substitution produces output that breaks
    JSON serialisation, the shell hook silently sends malformed JSON to Claude Code.
    Claude Code rejects the hook output with no user-visible error; the agent
    proceeds without the executability self-check injected.
    """
    from secondsight.feedback.hit_injection import render_wrapper

    prompt = 'fix the "auth bug\\path" with multi\nline issue'
    rendered = render_wrapper(prompt)

    # Before JSON round-trip: assert the raw rendered text actually contains
    # the original prompt verbatim. The round-trip check below is necessary
    # but NOT sufficient; if Jinja2 escapes characters into something that
    # happens to round-trip, the substring assertions would silently pass.
    assert prompt in rendered, (
        "rendered wrapper must contain the original prompt verbatim (autoescape regression check)"
    )

    # The rendered text must survive being embedded in the hook JSON payload.
    payload = json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": rendered,
            }
        }
    )
    recovered = json.loads(payload)
    additional_context = recovered["hookSpecificOutput"]["additionalContext"]

    # The original prompt characters must be present in the recovered text.
    assert '"auth bug' in additional_context, (
        "Double-quote from prompt was not preserved in JSON round-trip"
    )
    assert "\\path" in additional_context, (
        "Backslash from prompt was not preserved in JSON round-trip"
    )
    assert "multi\nline" in additional_context, (
        "Newline from prompt was not preserved in JSON round-trip"
    )


def test_dt_wrapper_passes_through_empty_prompt() -> None:
    """DT-2: Empty / whitespace-only prompt must return empty string — no wrap.

    Silent failure: if an empty prompt (e.g. slash command, accidental Enter)
    triggers the wrapper, the agent receives a meta-instruction asking it to
    evaluate an empty string for executability. The agent may stall, produce
    noise, or return an unexpected response. The hook contract for "no injection"
    is additionalContext == "", which the shell hook in task-3 translates to
    no stdout emission.
    """
    from secondsight.feedback.hit_injection import render_wrapper

    assert render_wrapper("") == "", "Empty string prompt must return empty string"
    assert render_wrapper("   \n\t  ") == "", "Whitespace-only prompt must return empty string"


def test_dt_wrapper_raises_on_missing_template(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DT-3: Missing template file must raise — not return empty string silently.

    Silent failure: if the template file is missing and render_wrapper catches
    (or the loader silently returns) the error, the hook emits an empty
    additionalContext for every prompt. SecondSight's hit injection is fully
    disabled. No error is surfaced to the operator. The failure looks identical
    to "no prompts matched the bypass condition".

    The fail-open semantics (don't block the user if injection fails) belong to
    the shell hook in task-3 — not to the Python helper. The helper's contract
    is: raise loudly; the caller decides whether to fail open.
    """
    import secondsight.prompts._loader as _loader_module
    from secondsight.feedback.hit_injection import render_wrapper

    def fake_get_template(name: str) -> None:
        raise jinja2.TemplateNotFound(name)

    monkeypatch.setattr(_loader_module._env, "get_template", fake_get_template)

    with pytest.raises(jinja2.TemplateNotFound):
        render_wrapper("some real prompt that should trigger the wrapper")


def test_dt_wrapper_raises_on_empty_render_for_nonempty_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DT-4: if template returns empty string for non-empty prompt, raise loudly.

    Silent failure: a future template change that conditionally emits nothing
    would cause render_wrapper to return "" — indistinguishable from the bypass
    path (empty/whitespace prompts). The shell hook would suppress all injection
    silently, with no error surfaced to the operator.

    The guard raises RuntimeError so that task-3's fail-open layer can log it
    rather than swallowing the failure as "normal bypass".
    """
    import secondsight.feedback.hit_injection as _hit_injection_module
    from secondsight.feedback.hit_injection import render_wrapper

    def fake_render(template_name: str, *, context: dict) -> str:  # noqa: ARG001
        return ""

    # Patch the `render` name as it lives in the hit_injection module's namespace,
    # not in _loader — because hit_injection.py does `from ... import render`.
    monkeypatch.setattr(_hit_injection_module, "render", fake_render)

    with pytest.raises(RuntimeError, match="template rendered empty output"):
        render_wrapper("a real prompt that should not produce empty output")


# ===========================================================================
# UNIT TESTS
# ===========================================================================


def test_wrapper_contains_original_prompt() -> None:
    """Happy path: rendered wrapper must include the user's original prompt text."""
    from secondsight.feedback.hit_injection import render_wrapper

    prompt = "add error handling to the login endpoint"
    rendered = render_wrapper(prompt)

    assert prompt in rendered, "Original prompt text must appear verbatim in wrapper output"


def test_wrapper_contains_executability_framing() -> None:
    """Happy path: wrapper must contain the executability self-check instruction.

    This pins the structural contract between the wrapper template and the
    main agent. If this test fails after a template edit, reviewers know the
    framing changed and must re-evaluate whether the agent's self-check logic
    still holds.
    """
    from secondsight.feedback.hit_injection import render_wrapper

    rendered = render_wrapper("update the user profile endpoint")

    # The wrapper must contain the core executability question (case-insensitive
    # because wording may be adjusted within the iteration).
    rendered_lower = rendered.lower()
    assert "execut" in rendered_lower, (
        "Wrapper must contain executability framing (e.g. 'execute', 'executability')"
    )


def test_wrapper_single_line_whitespace_prompt_is_bypassed() -> None:
    """Edge: single space (not empty string) must also be treated as empty."""
    from secondsight.feedback.hit_injection import render_wrapper

    assert render_wrapper(" ") == "", "Single-space prompt must return empty string"


def test_wrapper_unicode_prompt_survives_json_round_trip() -> None:
    """Happy path: Unicode (CJK, emoji) in prompt must survive JSON serialisation."""
    from secondsight.feedback.hit_injection import render_wrapper

    prompt = "修復登入端點的錯誤 🔐"
    rendered = render_wrapper(prompt)

    payload = json.dumps({"additionalContext": rendered}, ensure_ascii=False)
    recovered = json.loads(payload)["additionalContext"]

    assert "修復" in recovered, "CJK characters must survive JSON round-trip"
    assert "🔐" in recovered, "Emoji must survive JSON round-trip"


@pytest.mark.parametrize(
    ("agent", "prompt", "expected"),
    [
        ("claude_code", "/help", True),
        ("claude_code", "# remember this", True),
        ("codex", "/commit", True),
        ("codex", "# remember this", False),
        ("opencode", "/help", False),
        ("claude_code", "fix the auth bug", False),
    ],
)
def test_should_bypass_wrapper_agent_prefixes(agent: str, prompt: str, expected: bool) -> None:
    from secondsight.feedback.hit_injection import should_bypass_wrapper

    assert should_bypass_wrapper(prompt, agent) is expected
