# Problem Autopsy: Executor Failure Evidence Adapters

## Observed Failure Mode

Typed exceptions classify cleanly:

- Pydantic validation errors map to output repair classes.
- SDK/router transport traces map to transport classes.
- Auth/config exceptions map to fatal classes.

But some executor/provider failures arrive as untyped text. In those cases, shared recovery currently falls back to message markers such as `api key`, `authentication`, `unauthorized`, or provider-specific wording.

## The Lie

The shared classifier appears executor-agnostic because it returns shared taxonomy values.

## The Truth

The classifier still knows too much about executor/provider raw failure text. As more CLI executors are added, the classifier will accumulate brittle marker lists and become the place where adapter-specific quirks leak into shared policy.

## Correct Boundary

Executor-specific code should own executor-specific evidence extraction:

- Claude CLI adapter understands Claude result envelopes and stderr/status conventions.
- Codex CLI adapter understands Codex output-file conventions.
- SDK/router understands provider exceptions and attempt records.

Shared recovery should own only stable classification and retry semantics.

## Risk If Deferred Forever

- Adding new CLI executors requires shared recovery changes.
- Message drift in provider/CLI output can silently misclassify failures.
- Observability fields remain consistent in shape but not necessarily in classification quality.

## Why Not Fix Inside Phase 2

Phase 2's contract is shared output recovery semantics. The adapter evidence contract is a broader executor architecture change and should get its own plan, death cases, and acceptance criteria.
