# Overview: Analysis Output Recovery Phase 1

## Goal

Stabilize the CLI output-parse path without trying to solve the full cross-mode recovery problem in one shot.

## What Ships

Phase 1 ships four things together:

1. Local normalization for common JSON wrapper noise
2. A small shared output failure taxonomy
3. Structured, bounded retry feedback
4. Config-driven output-repair retry policy

## What Does Not Ship

- SDK output-repair retry
- Transport retry
- Shared cross-mode recovery orchestrator
- Metrics/dashboard work

## Why The Split Matters

This phase closes the highest-signal current failure mode with the smallest safe blast radius. It also defines the contract Phase 2 will build on, instead of forcing CLI and SDK convergence prematurely.
