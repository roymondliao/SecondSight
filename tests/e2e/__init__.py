"""E2E smoke and regression tests for analysis-mode-toggle feature.

Test categories:
  - test_legacy_config_upgrade.py: NO external services, must pass in CI
  - test_mode_toggle_cli.py: gated SECONDSIGHT_TEST_REAL_CLI=1
  - test_mode_toggle_sdk.py: gated SECONDSIGHT_TEST_REAL_LLM=1

See tests/e2e/fixtures/ for config.toml fixtures used by each category.
"""
