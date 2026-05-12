"""Fixture re-exports for the GUR-99 e2e integration suite.

``real_secondsight_server`` is defined in ``tests/scripts/conftest.py``
(where it was first authored for the bash-hook tests). Pytest scopes
conftest fixtures to the conftest's directory subtree, so that fixture
is not visible to tests under ``tests/integration/`` by default.

Importing the fixture function into THIS conftest re-registers it for
the ``tests/integration/`` subtree. This is the pytest-documented
pattern for cross-subtree fixture sharing — narrower blast radius than
lifting to ``tests/conftest.py`` and avoids the
``pytest_plugins``-in-non-rootdir deprecation warning.
"""

from tests.scripts.conftest import real_secondsight_server  # noqa: F401
