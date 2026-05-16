# Overview: Executor Failure Evidence Adapters

## Goal

Move executor/provider-specific failure interpretation out of shared recovery and into adapter-owned evidence extraction.

## What Ships

- Internal `ExecutorFailureEvidence` contract.
- Evidence-aware shared classifier that can consume stable adapter evidence.
- Claude/Codex CLI evidence extraction for non-zero and file failure paths.
- SDK/router evidence extraction from typed exception chains and attempt records.
- Stable `error_details` evidence metadata: source, confidence, executor, and reason.

## Key Principle

Shared recovery owns taxonomy and retry policy. Executors own raw failure interpretation.

If shared recovery needs to know a provider phrase to classify a failure, the boundary has failed.
