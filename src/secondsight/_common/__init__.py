"""Shared internal utilities for the SecondSight package.

Modules under `secondsight._common` are package-internal: they encode patterns
that recur across the codebase and have no business being part of the public
import surface. Cross-module duplication of a pattern that fits here is a
signal to extend or refactor this package, not to duplicate again.
"""
