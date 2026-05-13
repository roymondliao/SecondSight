"""Death + unit tests for `secondsight config` CLI (config-unification task-5).

Death test inventory (written FIRST — must be RED before implementation):
- DT-show-1: env var wins over TOML; output contains env var value + [env_var] label
- DT-show-2: no config.toml, no env var → all fields show [builtin_default], exit 0
- DT-validate-1: claude model name missing date suffix → validate exits 1, model format error
- DT-validate-2: config.toml has ${MISSING_VAR} → validate exits 1, missing var reported
- DT-validate-3: all config valid → validate exits 0, prints "N config file(s) validated, 0 errors"

Unit test inventory:
- UT-show-1: per-project config model → [per_project_config] label
- UT-show-2: global config model → [global_config] label
- UT-show-3: env var for default_agent → [env_var] label
- UT-validate-4: empty model string → no error (warn only = exit 0)
- UT-validate-5: gpt-4o model → valid, no error
- UT-validate-6: gemini model → valid, no error

Execution order (Samsara framework):
  Death tests written FIRST — expected RED before implementation.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from secondsight.cli.app import app

runner = CliRunner()

# =====================================================================
# Helpers
# =====================================================================


def _write_toml(path: Path, content: str) -> None:
    """Write a TOML file, creating parent dirs as needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# =====================================================================
# DEATH TESTS
# =====================================================================


class TestDTShow1EnvVarWinsOverTOML:
    """DT-show-1: env var overrides TOML; output shows env var value + [env_var] label.

    Silent failure target: source detection says [global_config] when env var is set.
    If source detection silently misreports, DC-1 is not solved.
    """

    def test_env_var_shown_with_env_var_label(self, tmp_path: Path) -> None:
        ss_home = tmp_path / ".secondsight"
        global_toml = ss_home / "config.toml"
        _write_toml(
            global_toml,
            '[analysis.models]\nclaude_code = "claude-haiku-4-5-20251001"\n',
        )

        env = {**os.environ, "SECONDSIGHT_ANALYSIS_MODEL": "claude-opus-4-7"}
        result = runner.invoke(
            app,
            ["config", "show", "--secondsight-home", str(ss_home)],
            env=env,
        )

        assert result.exit_code == 0, f"expected exit 0, got {result.exit_code}\n{result.output}"
        # Must show the env var value (not the TOML value)
        assert "claude-opus-4-7" in result.output, f"env var value not in output:\n{result.output}"
        # Must label it [env_var]
        assert "[env_var]" in result.output, f"[env_var] label missing:\n{result.output}"
        # Must NOT show [global_config] for the overridden field
        lines = result.output.splitlines()
        model_line = next(
            (ln for ln in lines if "claude-opus-4-7" in ln),
            None,
        )
        assert model_line is not None
        assert "[global_config]" not in model_line, (
            f"global_config label shown for env-var-sourced field: {model_line}"
        )


class TestDTShow2NoConfigNoEnvVar:
    """DT-show-2: no config.toml, no env var → all fields show [builtin_default], exit 0.

    Silent failure target: if any field silently resolves with wrong source, DC-1 is undermined.
    """

    def test_all_builtin_default_labels(self, tmp_path: Path) -> None:
        ss_home = tmp_path / ".secondsight"
        # Do NOT create any config files

        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("SECONDSIGHT_ANALYSIS_MODEL", "SECONDSIGHT_DEFAULT_AGENT")
        }
        result = runner.invoke(
            app,
            ["config", "show", "--secondsight-home", str(ss_home)],
            env=env,
        )

        assert result.exit_code == 0, f"expected exit 0, got {result.exit_code}\n{result.output}"
        assert "[builtin_default]" in result.output, (
            f"[builtin_default] label missing when no config exists:\n{result.output}"
        )
        # Must NOT show global_config or per_project_config when no files exist
        assert "[global_config]" not in result.output, (
            f"[global_config] wrongly present:\n{result.output}"
        )
        assert "[per_project_config]" not in result.output, (
            f"[per_project_config] wrongly present:\n{result.output}"
        )


class TestDTValidate1ClaudeModelMissingDateSuffix:
    """DT-validate-1: claude_code model missing YYYYMMDD suffix → exit 1, format error.

    Silent failure target: validate exits 0 with a typo model name → operator believes
    config is valid, but runtime will use wrong model or fail model resolution.
    """

    def test_missing_date_suffix_exits_one(self, tmp_path: Path) -> None:
        ss_home = tmp_path / ".secondsight"
        _write_toml(
            ss_home / "config.toml",
            '[analysis.models]\nclaude_code = "claude-haiku-4-5"\n',
        )

        result = runner.invoke(
            app,
            ["config", "validate", "--secondsight-home", str(ss_home)],
        )

        assert result.exit_code == 1, (
            f"expected exit 1 for malformed claude model, got {result.exit_code}\n{result.output}"
        )
        # Must mention model format error
        output_lower = result.output.lower()
        assert any(kw in output_lower for kw in ("format", "date", "yyyymmdd", "claude")), (
            f"no model format error in output:\n{result.output}"
        )


class TestDTValidate2MissingEnvVar:
    """DT-validate-2: config.toml has ${MISSING_VAR} → validate exits 1, names missing var.

    Silent failure target: validate silently ignores interpolation errors → operator
    deploys config that crashes at runtime when the env var is missing.
    """

    def test_missing_env_var_exits_one(self, tmp_path: Path) -> None:
        ss_home = tmp_path / ".secondsight"
        _write_toml(
            ss_home / "config.toml",
            '[analysis.models]\nclaude_code = "${MISSING_SECRET_MODEL_VAR}"\n',
        )

        # Ensure the var is not set
        env = {k: v for k, v in os.environ.items() if k != "MISSING_SECRET_MODEL_VAR"}
        result = runner.invoke(
            app,
            ["config", "validate", "--secondsight-home", str(ss_home)],
            env=env,
        )

        assert result.exit_code == 1, (
            f"expected exit 1 for missing env var ref, got {result.exit_code}\n{result.output}"
        )
        assert "MISSING_SECRET_MODEL_VAR" in result.output, (
            f"missing var name not reported:\n{result.output}"
        )


class TestDTValidate3ValidConfigExitsZero:
    """DT-validate-3: all config valid → exit 0, prints N config file(s) validated, 0 errors.

    Silent failure target: validate exits non-zero on valid config → operator
    wastes time debugging a false positive.
    """

    def test_valid_config_exits_zero(self, tmp_path: Path) -> None:
        ss_home = tmp_path / ".secondsight"
        _write_toml(
            ss_home / "config.toml",
            '[analysis.models]\nclaude_code = "claude-haiku-4-5-20251001"\n',
        )

        result = runner.invoke(
            app,
            ["config", "validate", "--secondsight-home", str(ss_home)],
        )

        assert result.exit_code == 0, (
            f"expected exit 0 for valid config, got {result.exit_code}\n{result.output}"
        )
        # Must print summary line
        output_lower = result.output.lower()
        assert "validated" in output_lower, f"no 'validated' in output:\n{result.output}"
        assert "0 error" in output_lower, f"no '0 errors' in output:\n{result.output}"


# =====================================================================
# UNIT TESTS
# =====================================================================


class TestUTShow1PerProjectConfigLabel:
    """UT-show-1: per-project config model → [per_project_config] label."""

    def test_per_project_label(self, tmp_path: Path) -> None:
        ss_home = tmp_path / ".secondsight"
        project_id = "proj-test-show"
        _write_toml(
            ss_home / "projects" / project_id / "config.toml",
            '[analysis]\nmodel = "claude-opus-4-5-20250301"\n',
        )

        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("SECONDSIGHT_ANALYSIS_MODEL", "SECONDSIGHT_DEFAULT_AGENT")
        }
        result = runner.invoke(
            app,
            ["config", "show", "--secondsight-home", str(ss_home), "--project", project_id],
            env=env,
        )

        assert result.exit_code == 0, f"exit {result.exit_code}\n{result.output}"
        assert "[per_project_config]" in result.output, (
            f"[per_project_config] label missing:\n{result.output}"
        )


class TestUTShow2GlobalConfigLabel:
    """UT-show-2: global config model → [global_config] label."""

    def test_global_config_label(self, tmp_path: Path) -> None:
        ss_home = tmp_path / ".secondsight"
        _write_toml(
            ss_home / "config.toml",
            '[analysis.models]\nclaude_code = "claude-haiku-4-5-20251001"\n',
        )

        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("SECONDSIGHT_ANALYSIS_MODEL", "SECONDSIGHT_DEFAULT_AGENT")
        }
        result = runner.invoke(
            app,
            ["config", "show", "--secondsight-home", str(ss_home)],
            env=env,
        )

        assert result.exit_code == 0, f"exit {result.exit_code}\n{result.output}"
        assert "[global_config]" in result.output, (
            f"[global_config] label missing:\n{result.output}"
        )
        assert "claude-haiku-4-5-20251001" in result.output


class TestUTShow3EnvVarDefaultAgentLabel:
    """UT-show-3: env var for default_agent → [env_var] label."""

    def test_default_agent_env_var_label(self, tmp_path: Path) -> None:
        ss_home = tmp_path / ".secondsight"

        env = {
            **{
                k: v
                for k, v in os.environ.items()
                if k not in ("SECONDSIGHT_ANALYSIS_MODEL", "SECONDSIGHT_DEFAULT_AGENT")
            },
            "SECONDSIGHT_DEFAULT_AGENT": "codex",
        }
        result = runner.invoke(
            app,
            ["config", "show", "--secondsight-home", str(ss_home)],
            env=env,
        )

        assert result.exit_code == 0, f"exit {result.exit_code}\n{result.output}"
        output = result.output
        # Find the line containing default_agent
        lines = output.splitlines()
        agent_line = next((ln for ln in lines if "default_agent" in ln), None)
        assert agent_line is not None, f"default_agent not in output:\n{output}"
        assert "[env_var]" in agent_line, f"[env_var] not on default_agent line: {agent_line}"
        assert "codex" in agent_line, f"'codex' not in default_agent line: {agent_line}"


class TestUTValidate4EmptyModelNoError:
    """UT-validate-4: empty model string → no error (warn only or nothing), exit 0."""

    def test_empty_model_exits_zero(self, tmp_path: Path) -> None:
        ss_home = tmp_path / ".secondsight"
        _write_toml(
            ss_home / "config.toml",
            '[analysis.models]\nclaude_code = ""\n',
        )

        result = runner.invoke(
            app,
            ["config", "validate", "--secondsight-home", str(ss_home)],
        )

        assert result.exit_code == 0, (
            f"expected exit 0 for empty model (not set), got {result.exit_code}\n{result.output}"
        )


class TestUTValidate5GptModelValid:
    """UT-validate-5: gpt-4o model → valid format, exit 0."""

    def test_gpt_model_valid(self, tmp_path: Path) -> None:
        ss_home = tmp_path / ".secondsight"
        _write_toml(
            ss_home / "config.toml",
            '[analysis.models]\ncodex = "gpt-4o"\n',
        )

        result = runner.invoke(
            app,
            ["config", "validate", "--secondsight-home", str(ss_home)],
        )

        assert result.exit_code == 0, (
            f"expected exit 0 for gpt model, got {result.exit_code}\n{result.output}"
        )


class TestUTValidate6GeminiModelValid:
    """UT-validate-6: gemini model → valid format, exit 0."""

    def test_gemini_model_valid(self, tmp_path: Path) -> None:
        ss_home = tmp_path / ".secondsight"
        _write_toml(
            ss_home / "config.toml",
            '[analysis.models]\nopencode = "gemini-2.0-flash"\n',
        )

        result = runner.invoke(
            app,
            ["config", "validate", "--secondsight-home", str(ss_home)],
        )

        assert result.exit_code == 0, (
            f"expected exit 0 for gemini model, got {result.exit_code}\n{result.output}"
        )


class TestUTShowTimestamp:
    """UT-show-timestamp: last loaded timestamp is present in output."""

    def test_timestamp_in_output(self, tmp_path: Path) -> None:
        ss_home = tmp_path / ".secondsight"

        result = runner.invoke(
            app,
            ["config", "show", "--secondsight-home", str(ss_home)],
        )

        assert result.exit_code == 0, f"exit {result.exit_code}\n{result.output}"
        assert "Config last loaded at:" in result.output, (
            f"timestamp line missing:\n{result.output}"
        )


class TestDTCleanupInterpolationLabeledCorrectly:
    """DT-cleanup-interpolation: cleanup_after_analysis = "${VAR}" must show [env_var_interpolation].

    Death test for DC-1 regression:
    The hand-rolled inline source detection for cleanup_after_analysis bypasses
    _determine_source(), which means a ${VAR} pattern in a boolean field gets mislabeled
    as [per_project_config] or [global_config] instead of [env_var_interpolation].
    Silent failure: operator can't tell the env var is controlling this field.
    """

    def test_cleanup_interpolation_labeled_correctly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ss_home = tmp_path / ".secondsight"
        # TOML uses string interpolation for a boolean field
        _write_toml(
            ss_home / "config.toml",
            '[retention]\ncleanup_after_analysis = "${CLEANUP_FLAG}"\n',
        )
        monkeypatch.setenv("CLEANUP_FLAG", "true")

        result = runner.invoke(
            app,
            ["config", "show", "--secondsight-home", str(ss_home)],
        )

        assert result.exit_code == 0, f"exit {result.exit_code}\n{result.output}"
        lines = result.output.splitlines()
        cleanup_line = next((ln for ln in lines if "cleanup_after_analysis" in ln), None)
        assert cleanup_line is not None, f"cleanup_after_analysis not in output:\n{result.output}"
        assert "[env_var_interpolation]" in cleanup_line, (
            f"cleanup_after_analysis must be labeled [env_var_interpolation] when "
            f"value is a ${{VAR}} reference, got: {cleanup_line!r}"
        )
        assert "[per_project_config]" not in cleanup_line, (
            f"cleanup_after_analysis must NOT be [per_project_config] for interpolated "
            f"value, got: {cleanup_line!r}"
        )
        assert "[global_config]" not in cleanup_line, (
            f"cleanup_after_analysis must NOT be [global_config] for interpolated "
            f"value, got: {cleanup_line!r}"
        )


class TestDTZeroTtlNotSilentlyIgnored:
    """DT-zero-ttl: per-project raw_traces_ttl_days = 0 must show 0, labeled [per_project_config].

    Death test for 'or' chain falsy bug:
    `project_ret.get("raw_traces_ttl_days") or global_ret.get(...)` treats 0 as falsy,
    falling through to the global/builtin default. Wrong value AND wrong label displayed.
    Silent failure: operator sets 0-day TTL to disable retention, but config show reports
    the global default, giving false confidence the config is not in effect.
    """

    def test_zero_ttl_not_silently_ignored(self, tmp_path: Path) -> None:
        ss_home = tmp_path / ".secondsight"
        project_id = "proj-zero-ttl"
        # Global config has a non-zero TTL
        _write_toml(
            ss_home / "config.toml",
            "[retention]\nraw_traces_ttl_days = 30\n",
        )
        # Per-project config explicitly sets 0
        _write_toml(
            ss_home / "projects" / project_id / "config.toml",
            "[retention]\nraw_traces_ttl_days = 0\n",
        )

        env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("SECONDSIGHT_ANALYSIS_MODEL", "SECONDSIGHT_DEFAULT_AGENT")
        }
        result = runner.invoke(
            app,
            ["config", "show", "--secondsight-home", str(ss_home), "--project", project_id],
            env=env,
        )

        assert result.exit_code == 0, f"exit {result.exit_code}\n{result.output}"
        lines = result.output.splitlines()
        ttl_line = next((ln for ln in lines if "raw_traces_ttl_days" in ln), None)
        assert ttl_line is not None, f"raw_traces_ttl_days not in output:\n{result.output}"
        # Must show 0, not 30 or the builtin default
        assert "= 0 " in ttl_line or ttl_line.strip().endswith("= 0"), (
            f"raw_traces_ttl_days must show value 0 from project config, got: {ttl_line!r}"
        )
        # Must label it [per_project_config]
        assert "[per_project_config]" in ttl_line, (
            f"raw_traces_ttl_days = 0 must be labeled [per_project_config], got: {ttl_line!r}"
        )


class TestDTTtlBoolRejectedByValidate:
    """DT-ttl-bool: raw_traces_ttl_days = true in TOML → validate exits 1.

    Death test for bool-as-int gap:
    isinstance(True, int) is True in Python, so `not isinstance(sv.value, int)` passes
    for a boolean, letting `raw_traces_ttl_days = true` exit validate with 0 errors.
    The runtime loader would raise SecondSightConfigError on the same input.
    Silent failure: operator sees validate pass, deploys config, runtime crashes.
    """

    def test_bool_ttl_rejected(self, tmp_path: Path) -> None:
        ss_home = tmp_path / ".secondsight"
        _write_toml(
            ss_home / "config.toml",
            "[retention]\nraw_traces_ttl_days = true\n",
        )

        result = runner.invoke(
            app,
            ["config", "validate", "--secondsight-home", str(ss_home)],
        )

        assert result.exit_code == 1, (
            f"expected exit 1 for bool TTL (bool is a subclass of int — must reject explicitly), "
            f"got {result.exit_code}\n{result.output}"
        )
        assert "raw_traces_ttl_days" in result.output, (
            f"field name not in error output:\n{result.output}"
        )


class TestDTDotEnvLoadedBeforeValidate:
    """DT-dotenv: ${VAR} ref resolved via ~/.secondsight/.env → validate exits 0.

    Death test for .env loading gap:
    _collect_sourced_values() calls _parse_toml() directly without first loading .env.
    If a var is defined only in ~/.secondsight/.env (not in shell env), validate raises
    SecondSightConfigError (unresolvable var) while the runtime loader succeeds.
    Silent failure: operator using .env for secrets sees validate report false positives.
    """

    def test_dotenv_loaded_before_validate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ss_home = tmp_path / ".secondsight"
        ss_home.mkdir(parents=True)
        # Config references a var that exists only in the .env file, NOT in the shell env
        _write_toml(
            ss_home / "config.toml",
            '[analysis.models]\nclaude_code = "${DOTENV_SECRET_MODEL}"\n',
        )
        # Write .env file (python-dotenv format)
        (ss_home / ".env").write_text(
            "DOTENV_SECRET_MODEL=claude-haiku-4-5-20251001\n", encoding="utf-8"
        )
        # Ensure the var is NOT in the shell environment
        monkeypatch.delenv("DOTENV_SECRET_MODEL", raising=False)

        result = runner.invoke(
            app,
            ["config", "validate", "--secondsight-home", str(ss_home)],
        )

        assert result.exit_code == 0, (
            f"expected exit 0 when var is defined in .env, got {result.exit_code}\n{result.output}"
        )


class TestDTD6NoDuplicateTomlReader:
    """DT-d6: after D-6 fix, config_cmd.py must not define its own TOML reader.

    Death test for D-6 (TOML re-reads DRY violation):
    Before fix: config_cmd.py defined _read_raw_toml() as a local TOML reader
    parallel to loader.py's _parse_toml(). Any change to the reading path (error
    messages, exception types) needed tracking to both sites independently.
    After fix: _read_raw_toml() is removed; config_cmd imports _parse_toml_both()
    from loader.py — both raw and interpolated reads co-locate in one function.
    """

    def test_no_local_toml_reader_in_config_cmd(self) -> None:
        import secondsight.cli.config_cmd as config_cmd

        assert not hasattr(config_cmd, "_read_raw_toml"), (
            "config_cmd.py must not define its own _read_raw_toml() — "
            "use _parse_toml_both() from loader.py instead (D-6 fix)"
        )

    def test_parse_toml_both_importable_from_loader(self) -> None:
        from secondsight.config import loader

        assert hasattr(loader, "_parse_toml_both"), (
            "loader.py must expose _parse_toml_both() after D-6 fix"
        )


class TestDTShowBoolTtlWarned:
    """DT-show-bool-ttl: config show with bool TTL must print WARNING, NOT exit 1.

    Death test for sfc-4-partial (iteration fix U-2):
    Before fix: config show renders `True` for raw_traces_ttl_days silently.
    config validate exits 1 for the same input but show has no warning.
    Silent failure: operator runs show, sees nothing wrong, skips validate,
    deploys config that crashes at runtime with SecondSightConfigError.
    """

    def test_bool_ttl_warns_in_show(self, tmp_path: Path) -> None:
        ss_home = tmp_path / ".secondsight"
        _write_toml(
            ss_home / "config.toml",
            "[retention]\nraw_traces_ttl_days = true\n",
        )

        result = runner.invoke(
            app,
            ["config", "show", "--secondsight-home", str(ss_home)],
        )

        # show must still exit 0 — type validation is validate's job, not show's
        assert result.exit_code == 0, (
            f"config show must exit 0 for bool TTL (informational only), "
            f"got {result.exit_code}\n{result.output}"
        )
        # BUT must print a WARNING so the operator knows something is wrong
        assert "WARNING" in result.output, (
            f"config show must print a WARNING for bool TTL (True is not a valid int TTL), "
            f"got:\n{result.output}"
        )
        assert "raw_traces_ttl_days" in result.output.split("WARNING", 1)[-1], (
            f"WARNING must mention the offending field name:\n{result.output}"
        )


class TestUTShowEnvVarInterpolation:
    """UT-show-interpolation: ${VAR} TOML value that resolves → [env_var_interpolation] label."""

    def test_interpolation_label(self, tmp_path: Path) -> None:
        ss_home = tmp_path / ".secondsight"
        _write_toml(
            ss_home / "config.toml",
            '[analysis.models]\nclaude_code = "${MY_MODEL_VAR}"\n',
        )

        env = {
            **{
                k: v
                for k, v in os.environ.items()
                if k not in ("SECONDSIGHT_ANALYSIS_MODEL", "SECONDSIGHT_DEFAULT_AGENT")
            },
            "MY_MODEL_VAR": "claude-haiku-4-5-20251001",
        }
        result = runner.invoke(
            app,
            ["config", "show", "--secondsight-home", str(ss_home)],
            env=env,
        )

        assert result.exit_code == 0, f"exit {result.exit_code}\n{result.output}"
        assert "[env_var_interpolation]" in result.output, (
            f"[env_var_interpolation] label missing:\n{result.output}"
        )


# ---------------------------------------------------------------------------
# DT-sec-1: path traversal rejection for --project in config show + validate
# ---------------------------------------------------------------------------


class TestDTSec1ConfigProjectTraversal:
    """DT-sec-1: config show and config validate must reject unsafe project IDs.

    sync.py and analyze.py both call is_safe_id() before constructing the
    project directory path. config_cmd.py must apply the same guard — without
    it, `--project ../../etc` would read TOML from outside ~/.secondsight/projects/.
    (Security review finding, 2026-05-13.)
    """

    @pytest.mark.parametrize(
        "bad_id",
        ["../../etc", "../passwd", "foo/bar", "..", "foo\x00inject"],
        ids=["dotdot-etc", "parent-passwd", "slash", "bare-dotdot", "null-byte"],
    )
    def test_show_rejects_unsafe_project_id(self, tmp_path: Path, bad_id: str) -> None:
        ss_home = tmp_path / ".secondsight"
        result = runner.invoke(
            app,
            ["config", "show", "--secondsight-home", str(ss_home), "--project", bad_id],
        )
        assert result.exit_code != 0, (
            f"config show must reject unsafe project id {bad_id!r}, "
            f"got exit_code={result.exit_code}; output={result.output!r}"
        )

    @pytest.mark.parametrize(
        "bad_id",
        ["../../etc", "../passwd", "foo/bar", "..", "foo\x00inject"],
        ids=["dotdot-etc", "parent-passwd", "slash", "bare-dotdot", "null-byte"],
    )
    def test_validate_rejects_unsafe_project_id(self, tmp_path: Path, bad_id: str) -> None:
        ss_home = tmp_path / ".secondsight"
        result = runner.invoke(
            app,
            ["config", "validate", "--secondsight-home", str(ss_home), "--project", bad_id],
        )
        assert result.exit_code != 0, (
            f"config validate must reject unsafe project id {bad_id!r}, "
            f"got exit_code={result.exit_code}; output={result.output!r}"
        )
