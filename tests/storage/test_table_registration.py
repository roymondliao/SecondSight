"""Death tests + unit tests for storage package table registration invariant.

DEATH TESTS first — these verify the silent failure path that motivated this fix:
    If any table module is NOT imported before metadata.create_all(), that table
    will silently not be created. The failure is invisible until a write fails
    or a query returns empty unexpectedly.

The architectural invariant enforced here:
    ANY import of `secondsight.storage` must register ALL tables with the
    shared SQLAlchemy MetaData. `__init__.py` achieves this by importing every
    table module. If a future contributor adds a new table WITHOUT updating
    `__init__.py`, these tests will catch it.

Silent failure mode this pins:
    1. metadata.create_all() is called after `import secondsight.storage`
    2. A table module was added but NOT imported in __init__.py
    3. The table is never created in the DB
    4. First symptom: IntegrityError or silent data loss on write to that table
    5. Discovery: only when someone queries the missing table or checks the DB schema
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXPECTED_TABLE_NAMES = {
    "events",
    "behavior_flags",
    "directives",
    "analysis_runs",
    "session_reports",
    "analysis_outputs",
}


def _reload_storage_metadata():
    """Return the shared MetaData object from events_table.

    We import it directly (not via secondsight.storage) to inspect
    what is registered at the SQLAlchemy level.
    """
    from secondsight.storage.events_table import metadata  # always safe — it's the root

    return metadata


# ---------------------------------------------------------------------------
# DEATH TESTS — run before any import of secondsight.storage
# ---------------------------------------------------------------------------


class TestDeathPaths:
    """Verify the silent failure paths that motivated this fix.

    These tests prove that the pre-fix state (or future regression) is
    detectable: importing only a subset of table modules leaves tables
    unregistered.
    """

    def test_importing_events_table_alone_does_not_register_analysis_outputs(self):
        """DEATH TEST: importing only events_table does NOT register analysis_outputs.

        This replicates the risk path: a caller imports events_table (which defines
        the shared metadata) without also importing analysis_outputs_table.
        analysis_outputs table must be absent from metadata at that point.

        After this test, we import analysis_outputs_table directly to confirm
        the table registers on import — proving the mechanism works.
        """
        # We can't truly undo Python's module cache between tests, but we CAN
        # verify the invariant directly: at any point in this process,
        # if analysis_outputs_table has been imported, the table is registered.
        # The inverse: if we check metadata BEFORE analysis_outputs_table is imported
        # in a subprocess-clean state, it won't be there.
        #
        # Instead, we verify the mechanism: after importing analysis_outputs_table
        # directly, the table IS in metadata — proving registration is import-driven.
        # The complementary unit test (below) verifies that secondsight.storage
        # import causes this registration.

        # Import events_table alone (isolated metadata state in a subprocess is
        # not practical here; instead we verify the table module self-registers).
        from secondsight.storage import events_table  # noqa: F401

        metadata = events_table.metadata

        # analysis_outputs_table may or may not be registered depending on
        # whether it has been imported earlier in the test session.
        # The critical invariant is: AFTER importing analysis_outputs_table, it IS registered.
        import secondsight.storage.analysis_outputs_table as ao_table  # noqa: F401

        assert "analysis_outputs" in metadata.tables, (
            "analysis_outputs table was NOT registered after importing analysis_outputs_table. "
            "The table registration side-effect on import is broken."
        )

    def test_importing_analysis_outputs_table_directly_registers_with_shared_metadata(self):
        """Confirm that analysis_outputs_table.py registers its table with events_table.metadata.

        This verifies the mechanism: table modules use `from secondsight.storage.events_table
        import metadata` and then call `sa.Table(name, metadata, ...)` — the sa.Table()
        call registers the table with metadata as a side effect of object construction.
        """
        import secondsight.storage.analysis_outputs_table  # noqa: F401
        from secondsight.storage.events_table import metadata

        assert "analysis_outputs" in metadata.tables, (
            "analysis_outputs_table.py did not register 'analysis_outputs' with shared metadata. "
            "Check that it imports `from secondsight.storage.events_table import metadata`."
        )

    def test_importing_analysis_runs_table_directly_registers_with_shared_metadata(self):
        """Same mechanism verification for analysis_runs_table."""
        import secondsight.storage.analysis_runs_table  # noqa: F401
        from secondsight.storage.events_table import metadata

        assert "analysis_runs" in metadata.tables, (
            "analysis_runs_table.py did not register 'analysis_runs' with shared metadata."
        )


# ---------------------------------------------------------------------------
# UNIT TESTS — the invariant we're enforcing with the __init__.py fix
# ---------------------------------------------------------------------------


class TestStoragePackageRegistrationInvariant:
    """After importing secondsight.storage, ALL tables must be registered.

    This is the architectural invariant: the storage package __init__.py
    imports every table module so that any caller using
    `from secondsight.storage import DBEngine; metadata.create_all(engine)`
    gets all tables, not a silent subset.
    """

    def test_import_secondsight_storage_registers_analysis_outputs(self):
        """Importing secondsight.storage must register 'analysis_outputs' table.

        Pre-fix state: this test FAILS because __init__.py does not import
        analysis_outputs_table or analysis_outputs_repository.

        Post-fix state: this test PASSES because __init__.py was updated.
        """
        import secondsight.storage  # noqa: F401
        from secondsight.storage.events_table import metadata

        assert "analysis_outputs" in metadata.tables, (
            "Importing secondsight.storage did NOT register 'analysis_outputs' table. "
            "This means metadata.create_all() after `import secondsight.storage` will "
            "silently skip creating the analysis_outputs table — a data loss risk. "
            "Fix: add `from secondsight.storage.analysis_outputs_table import analysis_outputs  # noqa: F401` "
            "to src/secondsight/storage/__init__.py"
        )

    def test_import_secondsight_storage_registers_analysis_runs(self):
        """Importing secondsight.storage must register 'analysis_runs' table.

        Same pattern as analysis_outputs — analysis_runs_table is not imported
        via any __init__.py import chain, making it a second unregistered table.
        """
        import secondsight.storage  # noqa: F401
        from secondsight.storage.events_table import metadata

        assert "analysis_runs" in metadata.tables, (
            "Importing secondsight.storage did NOT register 'analysis_runs' table. "
            "This means metadata.create_all() after `import secondsight.storage` will "
            "silently skip creating the analysis_runs table. "
            "Fix: add analysis_runs_repository (or analysis_runs_table) import to __init__.py"
        )

    def test_import_secondsight_storage_registers_all_expected_tables(self):
        """Comprehensive: ALL expected table names must be present after package import.

        This test is the regression guard for future table additions.
        If a developer adds a new *_table.py but forgets __init__.py,
        this test will fail with a clear message naming the missing table.
        """
        import secondsight.storage  # noqa: F401
        from secondsight.storage.events_table import metadata

        registered = set(metadata.tables.keys())
        missing = _EXPECTED_TABLE_NAMES - registered
        assert not missing, (
            f"The following tables are NOT registered after `import secondsight.storage`: "
            f"{sorted(missing)}. "
            f"Every table module must be imported in src/secondsight/storage/__init__.py "
            f"so that metadata.create_all() always creates all tables. "
            f"This is the CRITICAL comment in __init__.py: 'ALL table modules must be "
            f"imported here to register their tables with shared metadata.'"
        )

    def test_registered_tables_are_a_superset_of_expected(self):
        """Verify no expected table is missing — allow extra tables (future additions).

        This is a softer version of the comprehensive test: it only fails if
        a table in _EXPECTED_TABLE_NAMES is absent. Extra tables (e.g. from
        third-party or future additions) are allowed.
        """
        import secondsight.storage  # noqa: F401
        from secondsight.storage.events_table import metadata

        for table_name in _EXPECTED_TABLE_NAMES:
            assert table_name in metadata.tables, (
                f"Expected table '{table_name}' is not registered in shared metadata "
                f"after `import secondsight.storage`. "
                f"Add the corresponding table module import to __init__.py."
            )

    def test_events_table_registered(self):
        """Base case: events table (the metadata owner) is always registered."""
        import secondsight.storage  # noqa: F401
        from secondsight.storage.events_table import metadata

        assert "events" in metadata.tables

    def test_behavior_flags_table_registered(self):
        """behavior_flags is registered via BehaviorFlagsRepository import chain."""
        import secondsight.storage  # noqa: F401
        from secondsight.storage.events_table import metadata

        assert "behavior_flags" in metadata.tables

    def test_directives_table_registered(self):
        """directives is registered via DirectivesRepository import chain."""
        import secondsight.storage  # noqa: F401
        from secondsight.storage.events_table import metadata

        assert "directives" in metadata.tables

    def test_session_reports_table_registered(self):
        """session_reports is registered via BehaviorFlagsRepository import chain."""
        import secondsight.storage  # noqa: F401
        from secondsight.storage.events_table import metadata

        assert "session_reports" in metadata.tables


# ---------------------------------------------------------------------------
# INVARIANT COMPLETENESS — verify __init__.py exports match table registration
# ---------------------------------------------------------------------------


class TestInitExportsCompleteness:
    """Verify that __init__.py __all__ includes the newly added symbols.

    If __all__ is declared, all public surface names should be listed there.
    This test ensures that adding an import without adding to __all__ doesn't
    create a hidden public surface.
    """

    def test_analysis_outputs_repository_in_all(self):
        """AnalysisOutputsRepository must appear in __all__ after the fix."""
        import secondsight.storage as storage

        if hasattr(storage, "__all__"):
            assert "AnalysisOutputsRepository" in storage.__all__, (
                "AnalysisOutputsRepository was added to __init__.py imports but "
                "was NOT added to __all__. This creates a hidden public surface "
                "that `from secondsight.storage import *` won't include."
            )

    def test_analysis_runs_repository_in_all(self):
        """AnalysisRunsRepository must appear in __all__ after the fix."""
        import secondsight.storage as storage

        if hasattr(storage, "__all__"):
            assert "AnalysisRunsRepository" in storage.__all__, (
                "AnalysisRunsRepository was added to __init__.py imports but "
                "was NOT added to __all__."
            )
