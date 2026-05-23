"""Internal CLI entrypoints for agent hook runtime delegation."""

from __future__ import annotations

import sys

import typer

from secondsight.cli._typer import create_typer
from secondsight.feedback.hook_runner import build_user_prompt_hook_output

app = create_typer(
    name="hook",
    help="Internal hook runtime commands.",
    hidden=True,
    add_completion=False,
    no_args_is_help=True,
)


@app.command(name="user-prompt", hidden=True)
def user_prompt() -> None:
    """Process UserPromptSubmit payload from stdin and emit hook stdout JSON."""
    output, diagnostics = build_user_prompt_hook_output(sys.stdin.read())
    for diagnostic in diagnostics:
        typer.echo(diagnostic, err=True)
    if output:
        typer.echo(output, nl=False)
