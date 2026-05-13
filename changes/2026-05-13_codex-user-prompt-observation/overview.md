# Overview: codex-user-prompt-observation

## Goal
Make Codex observation trust real hook stdin again, especially for `UserPromptSubmit`, while proving the full hook -> ingress -> persistence path against verified local captures.

## Architecture
Codex hook payloads remain the single source of truth. `CodexAdapter` normalizes verified hook stdin into `PartialEvent.data`, the thin ingress route persists that normalized event, and the Codex hook installer writes a registration shape that matches the verified local working setup.

## Tech Stack
Python, FastAPI ingress, SecondSight adapter/observation pipeline, pytest fixture contracts, Codex `hooks.json` patching.

## Key Decisions
- `hook payload > rollout JSONL`: rollout files are out of scope for this feature and must not be used to "repair" prompt storage.
- `prompt_text stored completely`: Codex `UserPromptSubmit.prompt` normalizes to `data.action_metadata.prompt_text`.
- `raw tool/assistant output dropped`: `tool_response` and `last_assistant_message` stay outside `Event.data`.
- `verified local capture drives contract`: the 2026-05-13 hook captures override older inferred fixture shapes.
- `tool-hook registration must match working local shape`: Pre/Post tool hooks are part of the supported Codex observation surface and must be installed accordingly.

## Death Cases Summary
1. Codex appears to ingest user prompts, but `prompt_text` is silently reduced to cwd-only or length-only data.
2. Fixtures drift back to invented lower-case or nested payload shapes, so tests validate the wrong contract.
3. Raw `tool_response` or `last_assistant_message` leaks into `Event.data` even though the adapter still appears to function.

## File Map
- `src/secondsight/adapters/codex.py` — normalize verified Codex hook payloads and enforce drop rules.
- `src/secondsight/installer/codex_hooks.py` — register the complete Codex observation hook surface in verified shape.
- `tests/fixtures/codex/*.json` — verified hook payload truth set.
- `tests/fixtures/codex/_README.md` — fixture provenance and drift policy.
- `tests/adapters/test_codex.py` — adapter death tests and round-trip assertions.
- `tests/adapters/test_codex_fixtures.py` — fixture governance checks.
- `tests/api/test_ingress_codex.py` — Codex hook -> ingress -> persistence coverage.
- `tests/installer/test_codex_hooks.py` — installer death tests for Codex hook registration.

