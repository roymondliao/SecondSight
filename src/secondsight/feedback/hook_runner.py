"""Internal hook runtime helpers for CLI-driven hook execution."""

from __future__ import annotations

import json
import os

from secondsight.feedback.hit_injection import render_wrapper, should_bypass_wrapper


def build_user_prompt_hook_output(
    payload_json: str,
    *,
    agent: str | None = None,
    secondsight_home: str | None = None,
) -> tuple[str, list[str]]:
    """Return hook stdout JSON plus any diagnostics for UserPromptSubmit."""
    diagnostics: list[str] = []
    config_path = os.path.join(
        secondsight_home or os.environ.get("SECONDSIGHT_HOME") or _default_secondsight_home(),
        "config.toml",
    )
    hit_injection_enabled = True
    try:
        import tomllib

        with open(config_path, "rb") as f:
            doc = tomllib.load(f)
        raw = doc.get("feedback", {}).get("hit_injection_enabled", True)
        if type(raw) is not bool:
            diagnostics.append(
                f"hit_injection_enabled: invalid value {raw!r}; "
                "expected bool true/false; defaulting to true"
            )
        else:
            hit_injection_enabled = raw
    except FileNotFoundError:
        hit_injection_enabled = True
    except Exception as exc:
        diagnostics.append(f"hit_injection config read error: {exc!r}; defaulting to true")

    if not hit_injection_enabled:
        return "", diagnostics

    try:
        payload = json.loads(payload_json) if payload_json else {}
    except json.JSONDecodeError as exc:
        diagnostics.append(f"hit_injection payload parse error: {exc}")
        return "", diagnostics
    if not isinstance(payload, dict):
        diagnostics.append(
            "hit_injection payload shape error: expected object payload for UserPromptSubmit"
        )
        return "", diagnostics

    prompt = payload.get("prompt", "")
    if not isinstance(prompt, str):
        diagnostics.append("hit_injection payload shape error: prompt must be a string")
        return "", diagnostics

    if should_bypass_wrapper(prompt, agent or os.environ.get("SECONDSIGHT_AGENT", "claude_code")):
        return "", diagnostics

    wrapper = render_wrapper(prompt)
    if not wrapper:
        return "", diagnostics

    return (
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": wrapper,
                }
            }
        ),
        diagnostics,
    )


def _default_secondsight_home() -> str:
    return os.path.join(os.path.expanduser("~"), ".secondsight")
