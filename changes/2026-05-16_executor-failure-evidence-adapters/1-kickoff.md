# Kickoff: Executor Failure Evidence Adapters

## Problem

Analysis output recovery now has a shared taxonomy across CLI and SDK dispatchers, but some failure classification still depends on raw message markers when no typed exception is available.

That is acceptable as a fallback, but it is in the wrong layer: shared recovery should not know every CLI/provider wording. Each executor adapter should convert its own raw failure shape into stable internal evidence before the shared classifier runs.

## Why This Is Feature-Level

This is not a local bug fix. It changes the boundary between:

- CLI adapters (`claude_code`, `codex`, future CLI executors)
- SDK/router exception handling
- shared recovery classification
- observability fields in `AnalysisOutput.error_details`

Doing this inside analysis-output-recovery-phase2 iteration would expand scope beyond output recovery semantics into executor adapter architecture.

## Goal

Introduce an internal failure evidence contract:

```text
executor raw failure
  -> executor-specific adapter evidence
  -> shared classifier
  -> retry decision
  -> shared observability fields
```

## Non-Goals

- Do not add new provider integrations.
- Do not change retry policy semantics from analysis-output-recovery-phase2.
- Do not require every executor to expose real typed exceptions; CLI adapters may emit structured evidence derived from process output.

## Initial Design Direction

- Add a `RawFailureEvidence` / `ExecutorFailureEvidence` model.
- Move CLI-specific message markers out of shared classifier and into CLI adapter evidence extraction.
- Let SDK/router convert controlled framework exceptions and attempt traces into the same evidence model.
- Keep message heuristics as adapter-local fallback with explicit source/confidence metadata.

## Acceptance Sketch

- Adding a new CLI executor does not require editing shared classifier message marker lists.
- Shared classifier can classify evidence without parsing CLI-specific stdout/stderr text.
- Error details preserve raw evidence source and confidence for forensic debugging.
- Existing CLI/SDK recovery tests remain semantically unchanged.
