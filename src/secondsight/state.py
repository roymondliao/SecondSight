"""SecondSight persistent init-time state.

Records the agent selected during `secondsight init` and the install timestamp.
Used by 'auto' agent resolution in CLI dispatch (mode=cli, default_agent="auto").

State file location: ~/.secondsight/state.json
Schema version: "1.0"

JSON schema:
    {
        "schema_version": "1.0",
        "init_agent": "claude_code",
        "init_at": "2026-05-14T13:42:18+08:00",
        "secondsight_version": "<version>"
    }

Design decisions:
    - load() returns None when file is absent (not an error — fresh install).
    - load() raises SecondSightStateError when file exists but is malformed JSON.
    - save() creates the parent directory (mkdir parents=True, exist_ok=True).
    - Schema validation at this layer is minimal: JSON shape only.
      Agent-name validation (rejecting unsupported agents like "opencode") is Task 6.
    - secondsight_version is read from package metadata at save() time.
      Falls back to sentinel "unknown" on PackageNotFoundError (logged as warning).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from loguru import logger

__all__ = [
    "SecondSightState",
    "SecondSightStateError",
]


class SecondSightStateError(Exception):
    """Raised when state.json exists but cannot be parsed or has invalid structure.

    NOT raised when state.json is absent — absent file is the fresh-install path.
    The error message always includes the file path so operators can locate and fix it.
    """


@dataclass
class SecondSightState:
    """Persistent init-time state for SecondSight.

    Written during `secondsight init`; read at analysis dispatch time to resolve
    `default_agent = "auto"` to the agent selected at init.

    Attributes:
        schema_version: Always "1.0" for this schema. Enables future migration.
        init_agent: The coding agent selected at init time. Values: "claude_code", "codex".
            (Task 6 rejects "opencode" at dispatch pre-check; this layer accepts it.)
        init_at: ISO8601 timestamp of when init ran. Stored as string, not datetime,
            to preserve the original timezone offset through round-trips.
        secondsight_version: The secondsight package version at init time.
            Used for upgrade detection. Falls back to "unknown" if package metadata
            is unavailable.
    """

    schema_version: str
    init_agent: str
    init_at: str
    secondsight_version: str

    @classmethod
    def load(cls, path: Path) -> "SecondSightState | None":
        """Load state from a JSON file.

        Args:
            path: Path to the state.json file.

        Returns:
            SecondSightState instance, or None if the file does not exist.
            None is the normal path for a fresh install — not an error.

        Raises:
            SecondSightStateError: The file exists but contains malformed JSON,
                or is missing required fields. The error message includes the path.
        """
        path = Path(path)
        if not path.is_file():
            return None

        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError) as exc:
            raise SecondSightStateError(f"state.json at {path} is malformed JSON: {exc}") from exc
        except OSError as exc:
            raise SecondSightStateError(f"state.json at {path} could not be read: {exc}") from exc

        if not isinstance(data, dict):
            raise SecondSightStateError(
                f"state.json at {path} must be a JSON object, got {type(data).__name__}"
            )

        required = ("schema_version", "init_agent", "init_at", "secondsight_version")
        missing = [k for k in required if k not in data]
        if missing:
            raise SecondSightStateError(
                f"state.json at {path} is missing required fields: {missing}"
            )

        return cls(
            schema_version=str(data["schema_version"]),
            init_agent=str(data["init_agent"]),
            init_at=str(data["init_at"]),
            secondsight_version=str(data["secondsight_version"]),
        )

    def save(self, path: Path) -> None:
        """Write state to a JSON file, creating the directory if needed.

        Args:
            path: Full path where state.json should be written.
                The parent directory is created if it does not exist.

        Raises:
            OSError: Disk write fails (permissions, full disk, etc.).
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": self.schema_version,
            "init_agent": self.init_agent,
            "init_at": self.init_at,
            "secondsight_version": self.secondsight_version,
        }
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        logger.debug(f"state.json written: agent={self.init_agent!r} at={path}")


def _get_secondsight_version() -> str:
    """Read the installed secondsight package version from package metadata.

    Returns:
        Version string (e.g. "0.1.0"), or "unknown" if not installed as a package
        (e.g. editable install without dist-info, or during tests).
    """
    try:
        from importlib.metadata import version

        return version("secondsight")
    except Exception:
        logger.warning("secondsight package version unavailable; using 'unknown' in state.json")
        return "unknown"


def make_state(init_agent: str) -> SecondSightState:
    """Construct a SecondSightState with the current timestamp and version.

    Convenience factory used by `secondsight init`. Reads the package version
    and captures the current local time as ISO8601.

    Args:
        init_agent: The agent selected at init time (e.g. "claude_code", "codex").

    Returns:
        SecondSightState ready to be saved.
    """
    now = datetime.now(tz=timezone.utc).astimezone()
    return SecondSightState(
        schema_version="1.0",
        init_agent=init_agent,
        init_at=now.isoformat(),
        secondsight_version=_get_secondsight_version(),
    )
