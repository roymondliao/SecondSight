"""Jinja2 template loader for SecondSight analysis prompts (Task 3).

Configuration choices documented here so Task 4 reviewers can adjust
with full context:

- PackageLoader("secondsight", "prompts"): templates are co-located
  with the Python package in src/secondsight/prompts/. This works from
  both source checkouts (via src/ layout) and installed wheels (hatchling
  includes the directory if it contains __init__.py files at every level).

- StrictUndefined: any template variable not present in the context dict
  raises jinja2.UndefinedError at render time. This is DC9 protection —
  a missing variable in a prompt would silently produce an empty
  substitution, delivering a malformed prompt to the coding agent.

- autoescape=False: analysis prompts are NOT HTML. Autoescape would
  encode < > & " ' characters in JSON schema blocks and code examples,
  corrupting the output the coding agent reads.

- trim_blocks=True, lstrip_blocks=True: block tags ({% ... %}) do not
  leave extra whitespace/blank lines in the rendered output. This makes
  template source readable without spurious blank lines in rendered
  prompts. Choice is documented here so the whitespace behavior is
  intentional and verifiable.
"""

from __future__ import annotations

import jinja2

_env = jinja2.Environment(
    loader=jinja2.PackageLoader("secondsight", "prompts"),
    undefined=jinja2.StrictUndefined,
    autoescape=False,
    trim_blocks=True,
    lstrip_blocks=True,
)


def render(template_name: str, context: dict) -> str:
    """Render a prompt template by name with the given context dict.

    Args:
        template_name: Relative path within the prompts package, without
            the .jinja2 extension. Example: "analysis/behavior".
        context: Dict of variables available to the template. With
            StrictUndefined, any variable referenced in the template
            but absent from context raises jinja2.UndefinedError.

    Returns:
        Rendered string.

    Raises:
        jinja2.TemplateNotFound: If the template file does not exist.
        jinja2.UndefinedError: If a context variable referenced in the
            template is absent from `context` (DC9 protection).
    """
    template = _env.get_template(f"{template_name}.jinja2")
    return template.render(**context)
