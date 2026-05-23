"""Shared Typer app factory for the SecondSight CLI."""

from __future__ import annotations

from typing import Any

import typer


def create_typer(**kwargs: Any) -> typer.Typer:
    """Create a Typer app with SecondSight's CLI-wide exception policy."""
    return typer.Typer(
        pretty_exceptions_enable=False,
        pretty_exceptions_show_locals=False,
        **kwargs,
    )
