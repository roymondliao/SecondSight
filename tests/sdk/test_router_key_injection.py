"""Tests for explicit key injection into LLMRouter — provider construction contract.

Death tests target:
- AnthropicProvider is constructed with explicit api_key (NOT implicit env read)
- OpenAIProvider is constructed with explicit api_key
- Agent is NOT constructed via Agent(model_id_string) shortcut without explicit provider
- Fallback path: fallback_used flag + error_details with both errors on DC4

Unit tests cover:
- Happy path: valid key → provider constructed correctly
- Provider construction per-spec: correct provider class for each provider name
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIModel

from secondsight.sdk._specs import ModelSpec
from secondsight.sdk.router import LLMRouter, RouterTerminalError


# ---------------------------------------------------------------------------
# DT: Agent NOT constructed via Agent(model_id_string) shortcut
# ---------------------------------------------------------------------------


def _collect_agent_shortcut_violations(source: str, filename: str) -> list[str]:
    """Parse source and return a list of Agent(...) shortcut violations found.

    A violation is any Agent( call that:
    - Has a bare string or name as the first positional arg (implicit model_id shortcut)
    - Has model= kwarg as a string literal (string model without explicit provider)

    Agent(model_id_string) relies on implicit env for api_key — forbidden by Decision E1.
    """
    tree = ast.parse(source)
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "Agent":
            if node.args:
                first_arg = node.args[0]
                if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                    violations.append(
                        f"{filename}:{node.lineno}: Agent({first_arg.value!r}...) — "
                        f"string shortcut without explicit provider"
                    )
                elif isinstance(first_arg, ast.Name):
                    violations.append(
                        f"{filename}:{node.lineno}: Agent({first_arg.id}...) — "
                        f"positional name may be a model_id string shortcut; "
                        f"use keyword model= with explicit provider instance"
                    )
            for kw in node.keywords:
                if kw.arg == "model" and isinstance(kw.value, ast.Constant):
                    if isinstance(kw.value.value, str):
                        violations.append(
                            f"{filename}:{node.lineno}: Agent(model={kw.value.value!r}) — "
                            f"string model without explicit provider"
                        )
    return violations


def test_no_implicit_model_string_in_default_agent_factory():
    """DT: NO file under src/secondsight/sdk/ may use Agent(model_id) shortcut.

    Agent(model_id) relies on implicit env for api_key. The production factory
    must use Agent(model=explicit_model_instance) where the model was constructed
    with an explicit provider. We verify this by parsing ALL .py files under sdk/
    (not just router.py) — sdk/agent.py also constructs Agent instances and must
    be guarded.

    This is a static source check — it fails the moment someone reintroduces
    the implicit path in ANY sdk/ file, even if tests that mock the factory pass.
    """
    sdk_dir = Path(__file__).parent.parent.parent / "src" / "secondsight" / "sdk"
    all_violations: list[str] = []

    for py_file in sorted(sdk_dir.glob("*.py")):
        source = py_file.read_text()
        violations = _collect_agent_shortcut_violations(source, py_file.name)
        all_violations.extend(violations)

    assert not all_violations, (
        "sdk/ files contain Agent( calls without explicit provider:\n"
        + "\n".join(f"  - {v}" for v in all_violations)
        + "\n\nAll Agent constructions must use an explicit provider model instance, "
        + "e.g. Agent(model=AnthropicModel('...', provider=AnthropicProvider(api_key=...)))."
    )


# ---------------------------------------------------------------------------
# DT: Explicit provider construction per spec
# ---------------------------------------------------------------------------


def test_anthropic_provider_constructed_with_explicit_key():
    """DT: When provider='anthropic', AnthropicProvider(api_key=...) is used explicitly.

    Verifies by inspecting the model instance in the constructed agent.
    The api_key must be the one from resolved_keys, not from os.environ.
    """
    resolved_keys = {
        "anthropic": "sk-ant-explicit-key",
        "openai": "",
        "custom": "",
    }
    router = LLMRouter(
        primary=ModelSpec(name="claude-haiku-4-5-20251001", provider="anthropic"),
        fallbacks=[],
        resolved_keys=resolved_keys,
    )

    spec = ModelSpec(name="claude-haiku-4-5-20251001", provider="anthropic")
    agent = router._agent_factory(spec)  # type: ignore[attr-defined]
    model = agent._model  # type: ignore[attr-defined]

    assert isinstance(model, AnthropicModel), (
        f"Expected AnthropicModel for anthropic provider, got {type(model).__name__}"
    )
    assert model.client.api_key == "sk-ant-explicit-key", (
        f"AnthropicModel api_key mismatch: {model.client.api_key!r}"
    )


def test_openai_provider_constructed_with_explicit_key():
    """DT: When provider='openai', OpenAIProvider(api_key=...) is used explicitly."""
    resolved_keys = {
        "anthropic": "",
        "openai": "sk-openai-explicit-key",
        "custom": "",
    }
    router = LLMRouter(
        primary=ModelSpec(name="gpt-4o-mini", provider="openai"),
        fallbacks=[],
        resolved_keys=resolved_keys,
    )

    spec = ModelSpec(name="gpt-4o-mini", provider="openai")
    agent = router._agent_factory(spec)  # type: ignore[attr-defined]
    model = agent._model  # type: ignore[attr-defined]

    assert isinstance(model, OpenAIModel), (
        f"Expected OpenAIModel for openai provider, got {type(model).__name__}"
    )
    assert model.client.api_key == "sk-openai-explicit-key", (
        f"OpenAIModel api_key mismatch: {model.client.api_key!r}"
    )


# ---------------------------------------------------------------------------
# DT: RouterTerminalError at init for missing key (not at dispatch)
# ---------------------------------------------------------------------------


def test_router_raises_at_init_when_primary_provider_key_missing():
    """DT: RouterTerminalError at LLMRouter.__init__ when primary provider key is empty.

    Silent failure mode: router allows empty key silently, dispatch fires,
    API returns 401, AnalysisOutput status='failure'. No alert fires because
    the failure is inside the response body, not an exception.
    """
    resolved_keys = {"anthropic": "", "openai": "", "custom": ""}

    with pytest.raises(RouterTerminalError) as exc_info:
        LLMRouter(
            primary=ModelSpec(name="claude-haiku-4-5-20251001", provider="anthropic"),
            fallbacks=[],
            resolved_keys=resolved_keys,
        )

    assert exc_info.value is not None, "RouterTerminalError must be raised at init"


def test_router_raises_at_init_not_at_dispatch():
    """DT: The error must fire at construction time, never silently pass construction
    and explode at first call() invocation.
    """
    resolved_keys = {"anthropic": "", "openai": "", "custom": ""}

    raised_at_init = False
    try:
        LLMRouter(
            primary=ModelSpec(name="claude-haiku-4-5-20251001", provider="anthropic"),
            fallbacks=[],
            resolved_keys=resolved_keys,
        )
    except RouterTerminalError:
        raised_at_init = True

    assert raised_at_init, (
        "RouterTerminalError must be raised at LLMRouter.__init__, "
        "not deferred to .call(). A deferred error is a silent failure."
    )


# ---------------------------------------------------------------------------
# Unit tests: happy path construction
# ---------------------------------------------------------------------------


def test_happy_path_anthropic_key_resolved():
    """Happy path: valid anthropic key → router constructs without error."""
    resolved_keys = {"anthropic": "sk-ant-valid", "openai": "", "custom": ""}
    router = LLMRouter(
        primary=ModelSpec(name="claude-haiku-4-5-20251001", provider="anthropic"),
        fallbacks=[],
        resolved_keys=resolved_keys,
    )
    assert router is not None


def test_happy_path_openai_key_resolved():
    """Happy path: valid openai key → router constructs without error."""
    resolved_keys = {"anthropic": "", "openai": "sk-openai-valid", "custom": ""}
    router = LLMRouter(
        primary=ModelSpec(name="gpt-4o-mini", provider="openai"),
        fallbacks=[],
        resolved_keys=resolved_keys,
    )
    assert router is not None


def test_factory_for_fallback_spec_uses_correct_provider():
    """Agent factory must select the correct provider class based on spec.provider."""
    resolved_keys = {
        "anthropic": "sk-ant-primary",
        "openai": "sk-openai-fallback",
        "custom": "",
    }
    router = LLMRouter(
        primary=ModelSpec(name="claude-haiku-4-5-20251001", provider="anthropic"),
        fallbacks=[ModelSpec(name="gpt-4o-mini", provider="openai")],
        resolved_keys=resolved_keys,
    )

    # Primary spec → AnthropicModel
    primary_spec = ModelSpec(name="claude-haiku-4-5-20251001", provider="anthropic")
    primary_agent = router._agent_factory(primary_spec)  # type: ignore[attr-defined]
    assert isinstance(primary_agent._model, AnthropicModel)  # type: ignore[attr-defined]

    # Fallback spec → OpenAIModel
    fallback_spec = ModelSpec(name="gpt-4o-mini", provider="openai")
    fallback_agent = router._agent_factory(fallback_spec)  # type: ignore[attr-defined]
    assert isinstance(fallback_agent._model, OpenAIModel)  # type: ignore[attr-defined]


def test_resolved_keys_required_parameter():
    """resolved_keys must be a required parameter — no default None fallback.

    If resolved_keys defaults to None, a caller that forgets to pass it would
    silently get the old implicit-env behavior. The parameter must be required.
    """
    import inspect

    sig = inspect.signature(LLMRouter.__init__)

    assert "resolved_keys" in sig.parameters, (
        "LLMRouter.__init__ must have a 'resolved_keys' parameter"
    )

    param = sig.parameters["resolved_keys"]
    assert param.default is inspect.Parameter.empty, (
        "resolved_keys must have NO default value (required parameter). "
        "A default of None would allow callers to bypass the contract."
    )
