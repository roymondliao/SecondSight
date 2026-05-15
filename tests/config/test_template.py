"""Death + unit tests for config/template.py (config-unification task-4).

Death tests — silent failure paths first:

  DT-init-1: config.toml already exists → write_config_if_needed() does NOT
              overwrite it. File content must be byte-identical after the call.

  DT-init-2: config.toml exists but is malformed TOML → write_config_if_needed()
              does NOT overwrite it, returns an error message. exit 0 is not
              enforced here (that's cli/init.py's responsibility) but the
              function must return a message containing "malformed" and must
              NOT raise.

  DT-init-3: config.toml does not exist → write_config_if_needed() creates it
              and the result is parseable by tomllib.load() (valid TOML).

  DT-init-4: generated config.toml contains all expected top-level sections.
              The exhaustive schema-coverage check lives in
              test_template_schema_contract.py (compares against the locked
              config.example.toml). This test only smoke-checks that the
              template has the major sections that operators need to find;
              it gives a faster failure signal when generate_config_template()
              is touched directly without running the full contract test.

  DT-init-5: config.toml exists and has ALL template keys already → diff is
              empty → write_config_if_needed() returns an "already up-to-date"
              message and does NOT write.
              ALSO: config.toml exists but is missing a key that the template
              has → diff is non-empty → write_config_if_needed() returns a
              "new keys available" message and does NOT write.

Unit tests:
  - get_template_keys() returns a set of dotted key paths
  - diff_against_template() returns only keys missing from existing file
  - generate_config_template() is idempotent (same output every call)
  - generate_config_template() contains the expected comment header
"""

from __future__ import annotations

import sys
from pathlib import Path

# tomllib is stdlib in Python 3.11+; tomli is the backport for earlier versions.
if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[import,no-reuse-dep]


# ---------------------------------------------------------------------------
# DT-init-1: existing config.toml is NOT overwritten
# ---------------------------------------------------------------------------


def test_death_existing_config_not_overwritten(tmp_path: Path) -> None:
    """If config.toml already exists, write_config_if_needed() must leave it
    byte-identical. Overwriting it would silently destroy operator customisation.
    """
    from secondsight.config.template import write_config_if_needed

    secondsight_home = tmp_path / ".secondsight"
    secondsight_home.mkdir()
    config_path = secondsight_home / "config.toml"
    original_content = "# custom config\n[retention]\nraw_traces_ttl_days = 999\n"
    config_path.write_text(original_content, encoding="utf-8")

    write_config_if_needed(secondsight_home)

    assert config_path.read_text(encoding="utf-8") == original_content, (
        "write_config_if_needed() must not overwrite existing config.toml"
    )


# ---------------------------------------------------------------------------
# DT-init-2: malformed config.toml is NOT overwritten, returns error message
# ---------------------------------------------------------------------------


def test_death_malformed_config_not_overwritten_and_message_returned(tmp_path: Path) -> None:
    """If config.toml exists but is malformed TOML, write_config_if_needed()
    must NOT overwrite it (operator's data is preserved) and must return an
    error message. It must NOT raise an exception.
    """
    from secondsight.config.template import write_config_if_needed

    secondsight_home = tmp_path / ".secondsight"
    secondsight_home.mkdir()
    config_path = secondsight_home / "config.toml"
    malformed_content = "this is not = valid toml [ broken\n"
    config_path.write_text(malformed_content, encoding="utf-8")

    # Must not raise
    result = write_config_if_needed(secondsight_home)

    # File must be untouched
    assert config_path.read_text(encoding="utf-8") == malformed_content, (
        "write_config_if_needed() must not overwrite malformed config.toml"
    )

    # Must include error indication
    assert "malformed" in result.lower(), (
        f"write_config_if_needed() must return a message containing 'malformed', got: {result!r}"
    )


# ---------------------------------------------------------------------------
# DT-init-3: non-existent config.toml is created and is valid TOML
# ---------------------------------------------------------------------------


def test_death_generated_config_is_valid_toml(tmp_path: Path) -> None:
    """If config.toml does not exist, write_config_if_needed() must create it
    with content that tomllib.load() can parse without error.
    A template that produces invalid TOML would silently fail at parse time
    much later (when the loader tries to read it).
    """
    from secondsight.config.template import write_config_if_needed

    secondsight_home = tmp_path / ".secondsight"
    secondsight_home.mkdir()
    config_path = secondsight_home / "config.toml"

    assert not config_path.exists(), "precondition: config.toml must not exist"

    write_config_if_needed(secondsight_home)

    assert config_path.exists(), "write_config_if_needed() must create config.toml"

    # Must parse without error
    with config_path.open("rb") as f:
        parsed = tomllib.load(f)
    assert isinstance(parsed, dict), "parsed config.toml must be a dict"


# ---------------------------------------------------------------------------
# DT-init-4: generated config.toml contains all expected sections
# ---------------------------------------------------------------------------


# Smoke-test list of top-level sections operators expect to see when they
# `cat ~/.secondsight/config.toml` for the first time. The full schema
# coverage check (key-set equality against the locked example) lives in
# test_template_schema_contract.py; this constant is intentionally a SUBSET
# focused on the most operator-visible sections.
_EXPECTED_SECTIONS = [
    "general",
    "providers",
    "analysis",
    "retention",
    "server",
]


def test_death_generated_config_contains_all_expected_sections(tmp_path: Path) -> None:
    """The generated config.toml must contain the major operator-facing sections.
    A missing section would mean operators have no template to edit and would
    be silently unable to configure that subsystem. This is a smoke test —
    test_template_schema_contract.py owns the exhaustive schema match.
    """
    from secondsight.config.template import generate_config_template

    template_str = generate_config_template()
    data = tomllib.loads(template_str)

    assert "general" in data, "template must have [general] section (mode + log_level)"
    assert "mode" in data["general"], "template must have general.mode (cli|sdk dispatch)"
    assert "providers" in data, "template must have [providers.*] sections (SDK auth)"
    assert "analysis" in data, "template must have [analysis] section"
    assert "cli" in data["analysis"], "template must have [analysis.cli] subsection"
    assert "sdk" in data["analysis"], "template must have [analysis.sdk] subsection"
    assert "retention" in data, "template must have [retention] section"
    assert "server" in data, "template must have [server] section (host/port/auto_start)"


# ---------------------------------------------------------------------------
# DT-init-5a: existing up-to-date config → "already up-to-date" message, no write
# ---------------------------------------------------------------------------


def test_death_up_to_date_config_returns_uptodate_message_no_write(tmp_path: Path) -> None:
    """If config.toml already has all template keys, write_config_if_needed()
    must return a message indicating up-to-date status and must NOT write to the file.
    """
    from secondsight.config.template import generate_config_template, write_config_if_needed

    secondsight_home = tmp_path / ".secondsight"
    secondsight_home.mkdir()
    config_path = secondsight_home / "config.toml"

    # Write a valid config that has the same structure as the template
    template_content = generate_config_template()
    config_path.write_text(template_content, encoding="utf-8")
    original_mtime = config_path.stat().st_mtime

    result = write_config_if_needed(secondsight_home)

    assert "up-to-date" in result.lower(), (
        f"write_config_if_needed() must return 'up-to-date' message when no diff, got: {result!r}"
    )

    # File must not be re-written (mtime unchanged)
    assert config_path.stat().st_mtime == original_mtime, (
        "write_config_if_needed() must not re-write file when already up-to-date"
    )
    assert config_path.read_text(encoding="utf-8") == template_content, "content must be unchanged"


# ---------------------------------------------------------------------------
# DT-init-5b: existing config missing new keys → diff message, no write
# ---------------------------------------------------------------------------


def test_death_config_with_missing_keys_returns_diff_message_no_write(tmp_path: Path) -> None:
    """If config.toml exists but is missing some template keys (version upgrade
    scenario), write_config_if_needed() must return a diff message and NOT write.
    Silent overwrite here would destroy operator customisations.
    """
    from secondsight.config.template import write_config_if_needed

    secondsight_home = tmp_path / ".secondsight"
    secondsight_home.mkdir()
    config_path = secondsight_home / "config.toml"

    # Write a minimal valid config that is missing some template keys
    minimal_content = "[retention]\nraw_traces_ttl_days = 90\n"
    config_path.write_text(minimal_content, encoding="utf-8")
    original_content = config_path.read_text(encoding="utf-8")

    result = write_config_if_needed(secondsight_home)

    # Must NOT overwrite
    assert config_path.read_text(encoding="utf-8") == original_content, (
        "write_config_if_needed() must not write when keys are missing from existing config"
    )

    # Must indicate new keys are available
    assert "new key" in result.lower(), (
        f"write_config_if_needed() must mention 'new key' in diff message, got: {result!r}"
    )


# ---------------------------------------------------------------------------
# Unit tests for generate_config_template()
# ---------------------------------------------------------------------------


def test_generate_config_template_is_idempotent() -> None:
    """Two consecutive calls to generate_config_template() must return
    byte-identical strings. Non-idempotent template generation would make
    diffing unreliable.
    """
    from secondsight.config.template import generate_config_template

    first = generate_config_template()
    second = generate_config_template()
    assert first == second, "generate_config_template() must be idempotent"


def test_generate_config_template_contains_comment_header() -> None:
    """Template must contain the expected comment header explaining how to use
    the config file. Without this, operators have no in-file guidance.
    """
    from secondsight.config.template import generate_config_template

    template = generate_config_template()
    assert "SecondSight Configuration" in template, (
        "template must contain 'SecondSight Configuration' in comment header"
    )
    assert "secondsight init" in template, (
        "template must reference 'secondsight init' in comment header"
    )
    assert "secondsight config validate" in template, (
        "template must reference 'secondsight config validate' in comment"
    )


def test_generate_config_template_references_env_usage() -> None:
    """Template must mention .env file for secrets. Without this hint, operators
    may put API keys directly in config.toml (a security risk).
    """
    from secondsight.config.template import generate_config_template

    template = generate_config_template()
    assert ".env" in template, "template must mention .env file for secrets"
    assert "ANTHROPIC_API_KEY" in template, (
        "template must give an example env var (ANTHROPIC_API_KEY)"
    )


# ---------------------------------------------------------------------------
# Unit tests for get_template_keys()
# ---------------------------------------------------------------------------


def test_get_template_keys_returns_set_of_dotted_paths() -> None:
    """get_template_keys() must return a non-empty set of dotted key paths."""
    from secondsight.config.template import get_template_keys

    keys = get_template_keys()
    assert isinstance(keys, set), "get_template_keys() must return a set"
    assert len(keys) > 0, "get_template_keys() must return non-empty set"
    # All entries must be strings
    assert all(isinstance(k, str) for k in keys), "all keys must be strings"


def test_get_template_keys_contains_expected_leaf_keys() -> None:
    """get_template_keys() must include key paths for all known leaf keys
    in the template.
    """
    from secondsight.config.template import get_template_keys

    keys = get_template_keys()
    # Sentinel keys spanning every major section of the locked schema. The
    # exhaustive set match lives in test_template_schema_contract.py; this set
    # is a smaller hand-picked sample (one or two per section) for fast
    # localized failure when only get_template_keys() is touched.
    expected = {
        "general.mode",
        "general.log_level",
        "providers.anthropic.ANTHROPIC_API_KEY",
        "providers.openai.OPENAI_API_KEY",
        "providers.custom.API_KEY",
        "analysis.timeout_seconds",
        "analysis.cli.default_agent",
        "analysis.cli.models.claude_code",
        "analysis.sdk.primary_model",
        "analysis.sdk.fallback_model",
        "observation.session_timeout_minutes",
        "server.host",
        "server.port",
        "storage.sqlite.cache_size_mb",
        "feedback.convention_injection_budget",
        "retention.raw_traces_ttl_days",
        "retention.analysis_ttl_days",
    }
    missing_from_keys = expected - keys
    assert not missing_from_keys, (
        f"get_template_keys() is missing expected key paths: {missing_from_keys}"
    )


# ---------------------------------------------------------------------------
# Unit tests for diff_against_template()
# ---------------------------------------------------------------------------


def test_diff_against_template_empty_when_all_keys_present(tmp_path: Path) -> None:
    """If existing config has all template keys, diff must be empty."""
    from secondsight.config.template import diff_against_template, generate_config_template

    config_path = tmp_path / "config.toml"
    config_path.write_text(generate_config_template(), encoding="utf-8")

    result = diff_against_template(config_path)
    assert result == [], (
        f"diff_against_template() must return [] when all keys present, got: {result!r}"
    )


def test_diff_against_template_returns_missing_keys(tmp_path: Path) -> None:
    """If existing config is missing some keys, diff must list them."""
    from secondsight.config.template import diff_against_template

    config_path = tmp_path / "config.toml"
    # Minimal config: only has retention section
    config_path.write_text(
        "[retention]\nraw_traces_ttl_days = 90\nanalysis_ttl_days = 365\ncleanup_after_analysis = false\n",
        encoding="utf-8",
    )

    result = diff_against_template(config_path)
    assert isinstance(result, list), "diff_against_template() must return a list"
    assert len(result) > 0, "must detect missing keys"
    # analysis keys should be in the diff
    assert any("analysis" in k for k in result), (
        f"analysis keys must appear in diff, got: {result!r}"
    )


def test_diff_against_template_no_false_positives(tmp_path: Path) -> None:
    """diff_against_template() must NOT report extra user keys as missing.
    A user may add their own keys; those are irrelevant to the diff.
    """
    from secondsight.config.template import diff_against_template, generate_config_template

    template = generate_config_template()
    # Add a user-defined section that does not exist in the template
    extra_content = template + "\n[user_custom_section]\ncustom_user_key = 42\n"
    config_path = tmp_path / "config.toml"
    config_path.write_text(extra_content, encoding="utf-8")

    result = diff_against_template(config_path)
    # diff_against_template returns keys that are in the template but missing
    # from the existing file. Extra user-added keys (here under a custom section)
    # must not appear in the diff output.
    assert "user_custom_section.custom_user_key" not in result, (
        "diff must not report user-added keys as missing template keys"
    )
    # Also verify the result is empty — the existing file contains all template keys
    assert result == [], (
        "diff must be empty when existing file contains all template keys (plus extras)"
    )


# ---------------------------------------------------------------------------
# Unit tests for write_config_if_needed() — new file creation
# ---------------------------------------------------------------------------


def test_write_config_if_needed_creates_file_and_returns_generated_message(tmp_path: Path) -> None:
    """When config.toml does not exist, write_config_if_needed() must create
    it and return a message containing 'generated'.
    """
    from secondsight.config.template import write_config_if_needed

    secondsight_home = tmp_path / ".secondsight"
    secondsight_home.mkdir()
    config_path = secondsight_home / "config.toml"

    result = write_config_if_needed(secondsight_home)

    assert config_path.exists(), "config.toml must be created"
    assert "generated" in result.lower(), (
        f"result message must contain 'generated', got: {result!r}"
    )


def test_write_config_if_needed_creates_parent_dir_if_missing(tmp_path: Path) -> None:
    """write_config_if_needed() must create ~/.secondsight/ if it doesn't exist."""
    from secondsight.config.template import write_config_if_needed

    secondsight_home = tmp_path / ".secondsight"
    # Do NOT create the dir — let write_config_if_needed create it
    assert not secondsight_home.exists()

    write_config_if_needed(secondsight_home)

    assert (secondsight_home / "config.toml").exists(), (
        "write_config_if_needed() must create parent dir and config.toml"
    )


def test_write_config_if_needed_dry_run_does_not_create_file(tmp_path: Path) -> None:
    """In dry_run mode, write_config_if_needed() must NOT create the file."""
    from secondsight.config.template import write_config_if_needed

    secondsight_home = tmp_path / ".secondsight"
    secondsight_home.mkdir()
    config_path = secondsight_home / "config.toml"

    result = write_config_if_needed(secondsight_home, dry_run=True)

    assert not config_path.exists(), "dry_run=True must not create config.toml"
    # But it should still indicate what would happen
    assert "would generate" in result.lower() or "generated" in result.lower(), (
        f"dry_run message must describe what would happen, got: {result!r}"
    )


# ---------------------------------------------------------------------------
# DT-vocab-1: message vocabulary constants are substrings of actual return values
# ---------------------------------------------------------------------------
#
# Silent-failure path: if MSG_GENERATED / MSG_MALFORMED / MSG_NEW_KEYS /
# MSG_UP_TO_DATE exist but the actual message strings in write_config_if_needed()
# are changed without updating the constants (or vice versa), the _render_text()
# pattern-match in cli/init.py will silently stop coloring the output.
# This test pins the constants against the real return values so a rename in
# either direction causes an immediate red.


def test_death_msg_constants_match_generated_message(tmp_path: Path) -> None:
    """MSG_GENERATED must be a substring of the message returned when config.toml
    does not exist and is freshly created.
    """
    from secondsight.config.template import MSG_GENERATED, write_config_if_needed

    secondsight_home = tmp_path / ".secondsight"
    secondsight_home.mkdir()

    result = write_config_if_needed(secondsight_home)

    assert MSG_GENERATED.lower() in result.lower(), (
        f"MSG_GENERATED={MSG_GENERATED!r} must be a substring of the 'file created' "
        f"return message, but got: {result!r}. "
        f"If you renamed the constant or changed the message, update the other to match."
    )


def test_death_msg_constants_match_malformed_message(tmp_path: Path) -> None:
    """MSG_MALFORMED must be a substring of the message returned when config.toml
    is malformed TOML.
    """
    from secondsight.config.template import MSG_MALFORMED, write_config_if_needed

    secondsight_home = tmp_path / ".secondsight"
    secondsight_home.mkdir()
    (secondsight_home / "config.toml").write_text("not = valid toml [ broken\n", encoding="utf-8")

    result = write_config_if_needed(secondsight_home)

    assert MSG_MALFORMED.lower() in result.lower(), (
        f"MSG_MALFORMED={MSG_MALFORMED!r} must be a substring of the 'malformed TOML' "
        f"return message, but got: {result!r}. "
        f"If you renamed the constant or changed the message, update the other to match."
    )


def test_death_msg_constants_match_new_keys_message(tmp_path: Path) -> None:
    """MSG_NEW_KEYS must be a substring of the message returned when config.toml
    exists but is missing some template keys.
    """
    from secondsight.config.template import MSG_NEW_KEYS, write_config_if_needed

    secondsight_home = tmp_path / ".secondsight"
    secondsight_home.mkdir()
    # Minimal config missing most template keys
    (secondsight_home / "config.toml").write_text(
        "[retention]\nraw_traces_ttl_days = 90\n", encoding="utf-8"
    )

    result = write_config_if_needed(secondsight_home)

    assert MSG_NEW_KEYS.lower() in result.lower(), (
        f"MSG_NEW_KEYS={MSG_NEW_KEYS!r} must be a substring of the 'new keys available' "
        f"return message, but got: {result!r}. "
        f"If you renamed the constant or changed the message, update the other to match."
    )


def test_death_msg_constants_match_up_to_date_message(tmp_path: Path) -> None:
    """MSG_UP_TO_DATE must be a substring of the message returned when config.toml
    already has all template keys.
    """
    from secondsight.config.template import (
        MSG_UP_TO_DATE,
        generate_config_template,
        write_config_if_needed,
    )

    secondsight_home = tmp_path / ".secondsight"
    secondsight_home.mkdir()
    (secondsight_home / "config.toml").write_text(generate_config_template(), encoding="utf-8")

    result = write_config_if_needed(secondsight_home)

    assert MSG_UP_TO_DATE.lower() in result.lower(), (
        f"MSG_UP_TO_DATE={MSG_UP_TO_DATE!r} must be a substring of the 'up-to-date' "
        f"return message, but got: {result!r}. "
        f"If you renamed the constant or changed the message, update the other to match."
    )


def test_death_vocab_constants_are_importable() -> None:
    """All four MSG_* constants must be importable from template.py.

    This test goes RED immediately if any constant is removed or renamed in
    template.py — catching the coupling break before _render_text() silently
    starts ignoring config_status strings.
    """
    from secondsight.config import template  # noqa: F401

    for attr in ("MSG_GENERATED", "MSG_MALFORMED", "MSG_NEW_KEYS", "MSG_UP_TO_DATE"):
        assert hasattr(template, attr), (
            f"template.py must export {attr!r}. "
            f"A missing constant means _render_text() in cli/init.py has no stable "
            f"contract to import from."
        )
        value = getattr(template, attr)
        assert isinstance(value, str) and value, (
            f"template.{attr} must be a non-empty string, got: {value!r}"
        )


# ---------------------------------------------------------------------------
# No-import-from-analysis guard
# ---------------------------------------------------------------------------


def test_template_module_does_not_import_from_analysis() -> None:
    """template.py must not import from secondsight.analysis or secondsight.sdk.
    This is an architectural constraint (import rule in task spec).
    """
    import importlib
    import sys

    # Remove the module from cache if already loaded
    for key in list(sys.modules.keys()):
        if "config.template" in key:
            del sys.modules[key]

    # Track what's imported during template module load
    pre_modules = set(sys.modules.keys())
    importlib.import_module("secondsight.config.template")
    new_modules = set(sys.modules.keys()) - pre_modules

    forbidden = [m for m in new_modules if "secondsight.analysis" in m or "secondsight.sdk" in m]
    assert not forbidden, (
        f"config.template must not import from analysis or sdk, but imported: {forbidden}"
    )
