"""SecondSight unified config package.

Provides the canonical schema definitions, env-var accessors, and the
unified loader that merges TOML layers + env var overrides.

Import paths (preferred — use these in new code):
    from secondsight.config import SecondSightConfig, load_global_config, load_project_config
    from secondsight.config import SecondSightConfigError

Individual module imports also work:
    from secondsight.config.schema import SecondSightConfig, RetentionConfig, ...
    from secondsight.config.env import get_env_analysis_model, ...
    from secondsight.config.loader import load_global_config, load_project_config

Backward-compat: existing callers importing from secondsight.analysis.config
or secondsight.storage.retention continue to work unchanged (those modules
re-export from this package).
"""

from secondsight.config.loader import load_global_config, load_project_config
from secondsight.config.schema import SecondSightConfig, SecondSightConfigError

__all__ = [
    "SecondSightConfig",
    "SecondSightConfigError",
    "load_global_config",
    "load_project_config",
]
