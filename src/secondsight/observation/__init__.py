"""SecondSight observation layer — Phase 1.

Public surface:
    ObservationPipeline — orchestrates raw-trace + DB writes (P1-4)
"""

from __future__ import annotations

from secondsight.observation.pipeline import ObservationPipeline

__all__ = ["ObservationPipeline"]
