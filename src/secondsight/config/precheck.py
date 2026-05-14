"""Server startup pre-check for SecondSight configuration (Task 6).

precheck() validates that the server can start cleanly given the current config.
It runs ONCE at server startup (not at each dispatch) and fails hard on
misconfiguration — the server does NOT start in degraded mode.

Design decisions:
    - precheck() returns PrecheckResult, never raises. This makes it testable
      and keeps the startup path clean (no try/except at the call site).
    - Failure is hard: if precheck returns fail, the server must exit non-zero.
      No degraded mode, no "try anyway and see what happens."
    - Binary path resolution (shutil.which) is done at precheck time and the
      resolved path is logged at INFO level for DC6 forensics. If the binary
      later disappears (DC6), the log captures the last-known-good path.
    - Precheck does NOT validate that the LLM API actually responds — it only
      checks that the key is non-empty (configured). Network validation at
      startup would add latency and flakiness for transient connectivity issues.

Death cases closed here:
    DC5: state.json missing when mode=cli + default_agent="auto" → state_missing
    DC5: CLI binary not in PATH → cli_binary_missing
    DC5: default_agent="opencode" (explicit or via state) → opencode_not_supported
    DC5: agent name not in _KNOWN_CLI_AGENTS (typo, case mismatch, foreign agent) → unknown_agent
    DC7: All provider keys empty in mode=sdk → no_providers
    DC-PRIMARY-MISSING: sdk.primary_model is empty → primary_model_missing

DC6 forensics:
    On successful precheck for cli mode, INFO log line contains the resolved binary
    path (shutil.which result). This creates a post-mortem anchor: if the binary
    disappears after server start, the log shows what path was valid at startup.

Silent failure conditions (see scar report):
    - precheck() does NOT verify the binary is actually the correct version.
      A renamed/corrupt binary at the resolved path would pass precheck.
    - precheck() does NOT test LLM API connectivity. An empty key is caught,
      but an invalid key (wrong format, expired) passes precheck and fails at
      first dispatch.
    - precheck() resolves the binary path once. If PATH changes after precheck,
      the warning does not fire.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field

from loguru import logger

from secondsight.config.schema import SecondSightConfig
from secondsight.state import SecondSightState

# Map of known CLI agent names to their binary name used by shutil.which
_AGENT_BINARY_MAP: dict[str, str] = {
    "claude_code": "claude",
    "codex": "codex",
}

# Agents that are explicitly not supported by CLI dispatch
_UNSUPPORTED_CLI_AGENTS: frozenset[str] = frozenset({"opencode"})

# Complete set of valid agent strings that may appear as effective_agent AFTER auto-resolution.
# Includes: supported agents and unsupported-but-recognised agents.
# Any string NOT in this set is unknown and precheck fails with reason="unknown_agent".
#
# NOTE: 'auto' is intentionally EXCLUDED from this set.
#   'auto' is a config-level sentinel consumed by the resolution branch in _check_cli_mode
#   BEFORE the enum check runs. The only way effective_agent=='auto' can reach the enum
#   check is if state.init_agent=='auto' (state corruption). That should fail cleanly with
#   reason='unknown_agent', not cause a KeyError in _AGENT_BINARY_MAP.
_KNOWN_CLI_AGENTS: frozenset[str] = (
    frozenset(_AGENT_BINARY_MAP.keys()) | _UNSUPPORTED_CLI_AGENTS
)


@dataclass(frozen=True)
class PrecheckResult:
    """Result of a server startup pre-check.

    Attributes:
        is_ok: True if precheck passed; False if it failed.
        reason: Short machine-readable failure reason. None on success.
            Known values: "state_missing", "cli_binary_missing",
            "opencode_not_supported", "no_providers", "primary_model_missing",
            "unknown_agent", "unknown_mode".
        message: Human-readable description. Non-empty on failure; may be empty/
            "ok" on success. Should be actionable: tell the user what to do.
    """

    is_ok: bool
    reason: str | None = field(default=None)
    message: str = field(default="")

    @classmethod
    def ok(cls) -> "PrecheckResult":
        """Construct a passing PrecheckResult."""
        return cls(is_ok=True, reason=None, message="ok")

    @classmethod
    def fail(cls, *, reason: str, message: str) -> "PrecheckResult":
        """Construct a failing PrecheckResult.

        Args:
            reason: Short machine-readable failure code.
            message: Human-readable actionable message (what to do to fix it).
        """
        return cls(is_ok=False, reason=reason, message=message)


def precheck(
    config: SecondSightConfig,
    state: SecondSightState | None,
    resolved_keys: dict | None = None,
) -> PrecheckResult:
    """Run server startup pre-check validation.

    Validates the config is consistent and all required resources are available.
    Returns PrecheckResult — never raises.

    Args:
        config: The resolved SecondSightConfig (all ${VAR} interpolation done).
        state: SecondSightState loaded from ~/.secondsight/state.json, or None
            if the file is absent (fresh install).
        resolved_keys: Optional dict of resolved provider keys (pre-interpolated).
            Accepted for spec contract compliance (Task 5 expected this signature).
            Not used by current implementation which reads from config.providers
            directly (config loader performs ${VAR} interpolation before this
            call, so config.providers.*.KEY fields are already resolved).
            If provided, the keys here should match config.providers values.

    Returns:
        PrecheckResult.ok() if all checks pass.
        PrecheckResult.fail(reason=..., message=...) if any check fails.
        The FIRST failure found is returned (short-circuit evaluation).

    Silent failure conditions:
        - The binary is checked for existence via shutil.which, not executability.
          A binary at the path that is not executable would pass precheck.
        - SDK provider keys are checked for non-empty, not for validity.
          An expired or malformed API key passes precheck and fails at first dispatch.
    """
    mode = config.general.mode

    if mode == "cli":
        return _check_cli_mode(config=config, state=state)
    elif mode == "sdk":
        return _check_sdk_mode(config=config)
    else:
        # Unknown mode — should not happen since loader validates mode
        # but fail explicitly in case the loader validation is bypassed.
        return PrecheckResult.fail(
            reason="unknown_mode",
            message=(
                f"[general].mode={mode!r} is not recognized. "
                f"Valid modes: 'cli', 'sdk'. Check config.toml."
            ),
        )


def _check_cli_mode(
    config: SecondSightConfig,
    state: SecondSightState | None,
) -> PrecheckResult:
    """Run pre-check validations for mode=cli.

    Resolution order for the effective agent:
    1. If default_agent != "auto": use default_agent directly.
    2. If default_agent == "auto": read state.init_agent.
       If state is None: fail with state_missing.

    Then (enum-first validation):
    3. If effective agent is NOT in _KNOWN_CLI_AGENTS: fail with unknown_agent.
       This catches typos ("claude-code"), case mismatches ("CLAUDE_CODE"), and
       completely unknown agents ("gemini_cli") BEFORE any binary lookup.
    4. If effective agent is "opencode": fail with opencode_not_supported.
    5. Look up binary in PATH via shutil.which.
       If not found: fail with cli_binary_missing.
    6. Log resolved binary path at INFO for DC6 forensics.
    """
    default_agent = config.analysis.cli.default_agent

    # Resolve "auto" → state.init_agent
    if default_agent == "auto":
        if state is None:
            logger.error(
                "precheck [mode=cli]: default_agent='auto' but state.json is missing. "
                "The server cannot start without knowing which CLI agent to use. "
                "Run `secondsight init --agent <claude_code|codex>` first."
            )
            return PrecheckResult.fail(
                reason="state_missing",
                message=(
                    "mode=cli requires state.json when default_agent='auto'. "
                    "Run `secondsight init --agent <claude_code|codex>` before starting the server."
                ),
            )
        effective_agent = state.init_agent
        logger.debug(f"precheck [mode=cli]: resolved 'auto' -> {effective_agent!r} from state.json")
    else:
        effective_agent = default_agent

    # Step 0: enum validation — reject any agent string not in the known set.
    # This runs BEFORE the opencode check and before binary lookup, so the failure
    # reason is honest: "unknown_agent" means we don't recognise the name at all,
    # not that a binary is missing (we don't even know what binary to look for).
    # Catches: typos ("claude-code"), case mismatches ("CLAUDE_CODE"), and
    # completely foreign agents ("gemini_cli").
    # NOTE: "auto" is NOT in _KNOWN_CLI_AGENTS (intentionally excluded — see module-level comment).
    # If effective_agent is "auto" here, it means state.init_agent was "auto" (state corruption).
    # That will be caught here and rejected with reason="unknown_agent", which is correct.
    if effective_agent not in _KNOWN_CLI_AGENTS:
        _valid_agent_names = sorted(_KNOWN_CLI_AGENTS)
        logger.error(
            f"precheck [mode=cli]: agent={effective_agent!r} is not a recognised agent name. "
            f"Valid agent values: {_valid_agent_names}. "
            f"Check state.json or [analysis.cli].default_agent in config.toml."
        )
        return PrecheckResult.fail(
            reason="unknown_agent",
            message=(
                f"agent={effective_agent!r} is not a recognised agent name. "
                f"Valid: {_valid_agent_names}. "
                f"Check state.json or [analysis.cli].default_agent in config.toml."
            ),
        )

    # Reject opencode (known but not supported in CLI mode)
    if effective_agent in _UNSUPPORTED_CLI_AGENTS:
        logger.error(
            f"precheck [mode=cli]: agent={effective_agent!r} is not supported. "
            f"opencode CLI mode is out of scope in this release. "
            f"Set default_agent to 'claude_code' or 'codex' in config.toml."
        )
        return PrecheckResult.fail(
            reason="opencode_not_supported",
            message=(
                "opencode CLI mode is out of scope in this release; "
                "set [analysis.cli].default_agent to 'claude_code' or 'codex'."
            ),
        )

    # Look up binary in PATH — effective_agent is guaranteed to be in _AGENT_BINARY_MAP
    # at this point: we have passed the unknown_agent check (not in _KNOWN_CLI_AGENTS
    # implies not in _AGENT_BINARY_MAP), and we have passed the opencode check
    # (_UNSUPPORTED_CLI_AGENTS agents don't have binaries and are rejected above).
    # The only remaining agents are those in _AGENT_BINARY_MAP ("claude_code", "codex").
    binary_name = _AGENT_BINARY_MAP[effective_agent]

    resolved_path = shutil.which(binary_name)
    if resolved_path is None:
        logger.error(
            f"precheck [mode=cli]: '{binary_name}' binary not found in PATH. "
            f"Install the {effective_agent!r} CLI before starting the server."
        )
        return PrecheckResult.fail(
            reason="cli_binary_missing",
            message=(
                f"`{binary_name}` CLI not found in PATH. "
                f"Install the {effective_agent!r} CLI and ensure it is in PATH."
            ),
        )

    # DC6 forensics: log the resolved binary path so if the binary disappears
    # after server startup, the post-mortem log captures the last-known-good path.
    logger.info(
        f"precheck [mode=cli]: agent={effective_agent!r} binary resolved to "
        f"{resolved_path!r}. Path captured for forensics (DC6)."
    )
    return PrecheckResult.ok()


def _check_sdk_mode(config: SecondSightConfig) -> PrecheckResult:
    """Run pre-check validations for mode=sdk.

    Checks:
    1. sdk.primary_model is non-empty.
    2. At least one provider key is non-empty (anthropic, openai, or custom).
    """
    primary_model = config.analysis.sdk.primary_model
    if not primary_model:
        logger.error(
            "precheck [mode=sdk]: sdk.primary_model is empty. "
            "Set [analysis.sdk].primary_model in config.toml."
        )
        return PrecheckResult.fail(
            reason="primary_model_missing",
            message=(
                "mode=sdk requires [analysis.sdk].primary_model to be set. "
                "Set it to a model name like 'claude-haiku-4-5-20251001' or 'gpt-4o'."
            ),
        )

    # Check that at least one provider has a non-empty key
    providers = config.providers
    has_anthropic = bool(providers.anthropic.ANTHROPIC_API_KEY)
    has_openai = bool(providers.openai.OPENAI_API_KEY)
    has_custom = bool(providers.custom.API_KEY)

    if not (has_anthropic or has_openai or has_custom):
        logger.error(
            "precheck [mode=sdk]: no provider API keys are configured. "
            "Set at least one of: [providers.anthropic].ANTHROPIC_API_KEY, "
            "[providers.openai].OPENAI_API_KEY, or [providers.custom].API_KEY."
        )
        return PrecheckResult.fail(
            reason="no_providers",
            message=(
                "mode=sdk requires at least one provider key to be resolvable. "
                "If you intended to use $ANTHROPIC_API_KEY from shell env, write "
                '`ANTHROPIC_API_KEY = "${ANTHROPIC_API_KEY}"` in config.toml '
                "under [providers.anthropic]."
            ),
        )

    logger.info(
        f"precheck [mode=sdk]: primary_model={primary_model!r}, "
        f"providers configured: "
        f"anthropic={has_anthropic}, openai={has_openai}, custom={has_custom}."
    )
    return PrecheckResult.ok()


__all__ = ["PrecheckResult", "precheck"]
