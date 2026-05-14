"""Jinja2 prompt templates for SecondSight analysis (Task 3).

Templates live in:
  src/secondsight/prompts/analysis/*.jinja2

The loader is configured with StrictUndefined — any missing context
variable raises jinja2.UndefinedError at render time rather than
producing an empty-string substitution (DC9 protection).

autoescape=False — analysis prompts contain JSON blocks and code
examples; HTML escaping would corrupt them.
"""
