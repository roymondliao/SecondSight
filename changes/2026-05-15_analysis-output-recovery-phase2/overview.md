# Overview: Analysis Output Recovery Phase 2

## Goal

Turn Phase 1's CLI-focused recovery helpers into a real shared recovery layer for both CLI and SDK dispatchers.

## What Ships

- Shared failure taxonomy across modes
- Shared retry policy and feedback semantics
- SDK adoption of output-repair retry
- Unified attempt accounting and `error_details` language

## Key Principle

Shared policy does not mean shared executor. CLI keeps subprocess control; SDK keeps provider/library control. The shared layer owns only classification, retry semantics, feedback, and observability language.
