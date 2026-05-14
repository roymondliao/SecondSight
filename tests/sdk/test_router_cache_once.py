"""Death tests for DC8: resolved keys are cached once at load time.

DC8 contract:
- Keys resolved at config load time are fixed for the lifetime of the LLMRouter.
- Mid-flight env mutation has NO effect on the api_key sent to providers.
- Key rotation requires server restart (documented behavior).

Silent failure mode: if the router re-reads os.environ at dispatch time,
a key rotation (invalidating the old key) would cause the next dispatch to
use the NEW env key even though the server was not restarted, breaking the
cache-once guarantee. Worse: if the env is UNSET after load, the router
would silently get None or "" and send "Authorization: Bearer None" to the API.
"""

from __future__ import annotations

from typing import Any

import pytest

from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from secondsight.sdk._specs import ModelSpec
from secondsight.sdk.router import LLMRouter


# ---------------------------------------------------------------------------
# DC8 — api_key in constructed provider is snapshot, not live env reference
# ---------------------------------------------------------------------------


def test_dc8_provider_constructed_with_snapshot_key(monkeypatch):
    """DC8: AnthropicProvider is constructed with the resolved key at router init time.

    After router is constructed with resolved_keys["anthropic"] = "sk-A",
    changing the env to "sk-B" must NOT change the provider's api_key.

    We verify by inspecting the actual provider instance that the router's
    agent_factory builds. The provider is constructed once at factory-call time;
    the resolved key is closed over, not re-read from env.
    """
    # Step 1: set env to sk-A and construct router
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-A-original")

    resolved_keys = {
        "anthropic": "sk-A-original",  # this is what the loader resolved
        "openai": "",
        "custom": "",
    }

    router = LLMRouter(
        primary=ModelSpec(name="claude-haiku-4-5-20251001", provider="anthropic"),
        fallbacks=[],
        resolved_keys=resolved_keys,
    )

    # Step 2: mutate env to sk-B (simulates key rotation)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-B-rotated")

    # Step 3: invoke the agent_factory to build a provider instance
    # (this is what LLMRouter.call() does internally)
    spec = ModelSpec(name="claude-haiku-4-5-20251001", provider="anthropic")
    agent = router._agent_factory(spec)  # type: ignore[attr-defined]

    # Step 4: introspect the agent's model to find the provider
    # PydanticAI Agent stores its model; an AnthropicModel wraps an AnthropicProvider
    # which has a .client attribute. The client's api_key is the real api_key sent.
    model = agent._model  # type: ignore[attr-defined]
    assert isinstance(model, AnthropicModel), (
        f"Expected AnthropicModel for anthropic provider, got {type(model).__name__}"
    )

    # Access the underlying client's api_key — this is what gets sent in Authorization header
    actual_key = model.client.api_key  # AnthropicProvider.client is the anthropic.AsyncAnthropic
    assert actual_key == "sk-A-original", (
        f"DC8 VIOLATED: provider api_key is {actual_key!r} after env mutation. "
        f"The router must use the snapshot key 'sk-A-original', not the current env 'sk-B-rotated'."
    )


def test_dc8_unset_env_after_load_does_not_affect_router(monkeypatch):
    """DC8: unsetting env after load must NOT affect dispatched requests.

    If the router re-reads env at dispatch time, unsetting ANTHROPIC_API_KEY
    after construction would cause the provider to get None as api_key.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-valid-at-load")

    resolved_keys = {"anthropic": "sk-valid-at-load", "openai": "", "custom": ""}
    router = LLMRouter(
        primary=ModelSpec(name="claude-haiku-4-5-20251001", provider="anthropic"),
        fallbacks=[],
        resolved_keys=resolved_keys,
    )

    # Unset env after construction
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # The agent_factory should still produce a provider with the original key
    spec = ModelSpec(name="claude-haiku-4-5-20251001", provider="anthropic")
    agent = router._agent_factory(spec)  # type: ignore[attr-defined]
    model = agent._model  # type: ignore[attr-defined]

    actual_key = model.client.api_key
    assert actual_key == "sk-valid-at-load", (
        f"DC8 VIOLATED: api_key is {actual_key!r} after env unset. "
        f"Router must retain the key it was constructed with."
    )


@pytest.mark.asyncio
async def test_dc8_dispatch_uses_snapshot_key_not_current_env(monkeypatch):
    """DC8: when dispatch fires, it uses the snapshot api_key, not whatever env says now.

    Uses the PRODUCTION _make_explicit_agent_factory (not a mock factory) so that the
    test actually exercises the production code path that constructs AnthropicProvider.
    A patched AnthropicProvider captures the api_key passed to its constructor.

    Previous version used a mock factory that closed over the outer `resolved_keys`
    dict directly — the assertion passed trivially regardless of production code.
    This version forces the real factory to construct a provider with the mutated env
    and asserts the provider still got the original snapshot key.
    """
    from unittest.mock import patch as _patch

    from secondsight.sdk.router import _make_explicit_agent_factory

    original_key = "sk-A-at-load-time"
    rotated_key = "sk-B-rotated-post-load"

    monkeypatch.setenv("ANTHROPIC_API_KEY", original_key)

    resolved_keys = {"anthropic": original_key, "openai": "", "custom": ""}

    # Build the production factory with the original snapshot.
    production_factory = _make_explicit_agent_factory(resolved_keys)

    # Rotate the env — the production factory's closed-over _keys must NOT see this.
    monkeypatch.setenv("ANTHROPIC_API_KEY", rotated_key)

    # Patch AnthropicProvider to capture the api_key it was constructed with.
    captured_keys: list[str] = []
    _OriginalAnthropicProvider = AnthropicProvider

    class _CapturingAnthropicProvider(_OriginalAnthropicProvider):
        def __init__(self, **kwargs: Any) -> None:
            captured_keys.append(kwargs.get("api_key", ""))
            super().__init__(**kwargs)

    # Invoke the production factory with the patch in place.
    with _patch(
        "secondsight.sdk.router.AnthropicProvider",
        side_effect=_CapturingAnthropicProvider,
    ):
        spec = ModelSpec(name="claude-haiku-4-5-20251001", provider="anthropic")
        production_factory(spec)

    assert len(captured_keys) == 1, (
        f"AnthropicProvider should have been constructed exactly once, got {len(captured_keys)}"
    )
    assert captured_keys[0] == original_key, (
        f"DC8 VIOLATED: production factory passed api_key={captured_keys[0]!r} to AnthropicProvider "
        f"but should have used the snapshot key {original_key!r}, not the rotated env {rotated_key!r}. "
        f"The factory is reading live env instead of the closed-over snapshot."
    )
