"""Death tests for DC7: no implicit env fallback in LLMRouter key injection.

DC7 contract:
- If config has ANTHROPIC_API_KEY = "" (empty) AND env $ANTHROPIC_API_KEY="sk-from-env",
  resolved_keys["anthropic"] MUST equal "" (NOT "sk-from-env").
  LLMRouter init MUST raise RouterTerminalError mentioning "no provider keys resolvable".

These are DEATH tests: they target silent failure paths where the old behaviour
(reading os.environ implicitly) would appear to work but uses a key that isn't
registered through the config contract. The damage: production rotations of
configured keys have no effect because the router silently falls back to whatever
is in the process environment.
"""

from __future__ import annotations

import pytest

from secondsight.config.loader import _build_providers_config, _resolve_provider_keys
from secondsight.sdk._specs import ModelSpec
from secondsight.sdk.router import LLMRouter, RouterTerminalError


# ---------------------------------------------------------------------------
# DC7 — empty config string does NOT fall back to env
# ---------------------------------------------------------------------------


def test_dc7_empty_config_key_does_not_read_env(monkeypatch):
    """DC7: config has ANTHROPIC_API_KEY = "" AND env has the key set.

    The resolved_keys dict MUST contain "" for "anthropic", NOT the env value.
    The loader's _resolve_provider_keys must NOT call os.environ["ANTHROPIC_API_KEY"]
    when the config value is already "".
    """
    # Set env — this must NOT be used
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env-should-not-be-used")

    # Config has empty string (no ${VAR} interpolation)
    providers_doc = {
        "providers": {
            "anthropic": {"ANTHROPIC_API_KEY": ""},
            "openai": {"OPENAI_API_KEY": ""},
            "custom": {},
        }
    }
    providers_config = _build_providers_config(providers_doc)
    resolved = _resolve_provider_keys(providers_config)

    # DC7: must be "" — NOT "sk-from-env-should-not-be-used"
    assert resolved.get("anthropic", "") == "", (
        f"DC7 VIOLATED: resolved_keys['anthropic'] = {resolved.get('anthropic')!r} "
        f"but config had empty string. The loader MUST NOT read os.environ as fallback."
    )


def test_dc7_all_empty_router_raises_at_init_not_dispatch(monkeypatch):
    """DC7: ALL provider keys are empty -> RouterTerminalError at LLMRouter.__init__.

    The error MUST be raised at construction time, NOT at first dispatch call.
    Silent failure mode: router accepts empty keys silently, dispatch fires,
    API returns 401, AnalysisOutput status="failure" with no alert.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env-ignored")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-from-env-ignored")

    resolved_keys: dict[str, str] = {
        "anthropic": "",
        "openai": "",
        "custom": "",
    }

    with pytest.raises(RouterTerminalError) as exc_info:
        LLMRouter(
            primary=ModelSpec(name="claude-haiku-4-5-20251001", provider="anthropic"),
            fallbacks=[],
            resolved_keys=resolved_keys,
        )

    error_msg = str(exc_info.value)
    assert "no provider keys resolvable" in error_msg.lower(), (
        f"RouterTerminalError message should mention 'no provider keys resolvable', got: {error_msg!r}"
    )


def test_dc7_var_interpolation_does_reach_env():
    """DC7 complement: when config HAS ${VAR} syntax, env IS read (this is the correct path).

    This test documents the intended contrast with DC7:
    - "${ANTHROPIC_API_KEY}" in config → loader replaces it with env value at parse time.
    - The resolved key is then the actual env value — passed explicitly into router.
    - Router does NOT read env itself; the resolved value comes from the loader.
    """
    # Simulate what the loader produces after ${VAR} interpolation
    # (The real interpolation is tested in test_loader_v2.py)
    resolved_keys: dict[str, str] = {
        "anthropic": "sk-from-env-via-interpolation",
        "openai": "",
        "custom": "",
    }

    # Should NOT raise — at least one key is resolvable
    router = LLMRouter(
        primary=ModelSpec(name="claude-haiku-4-5-20251001", provider="anthropic"),
        fallbacks=[],
        resolved_keys=resolved_keys,
    )
    assert router is not None


def test_dc7_partial_keys_only_configured_providers_usable():
    """Only providers with non-empty keys should be usable for routing.

    A router configured with a primary that has an empty key must raise,
    even if another provider has a key configured.
    """
    resolved_keys: dict[str, str] = {
        "anthropic": "",  # empty — cannot use anthropic primary
        "openai": "sk-openai-configured",
        "custom": "",
    }

    # anthropic primary with empty anthropic key -> should raise
    with pytest.raises(RouterTerminalError) as exc_info:
        LLMRouter(
            primary=ModelSpec(name="claude-haiku-4-5-20251001", provider="anthropic"),
            fallbacks=[],
            resolved_keys=resolved_keys,
        )

    # The error must name which provider key was missing
    error_msg = str(exc_info.value)
    assert "anthropic" in error_msg.lower(), (
        f"RouterTerminalError should name the missing provider 'anthropic', got: {error_msg!r}"
    )
