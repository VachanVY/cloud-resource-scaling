"""
CPU / utilisation-based reactive scaling.

Decision rule:
  IF utilization_pct > scale_up_threshold   → add scale_up_step replicas
  IF utilization_pct < scale_down_threshold → remove scale_down_step replicas
  ELSE                                      → no change

This mirrors the classic Kubernetes HPA behaviour where "CPU" is used as
the primary signal.  In our platform the signal is service utilisation
(active_requests / capacity × 100) because our service model is I/O-bound;
the semantics are identical for scaling policy evaluation purposes.
"""

from __future__ import annotations

from typing import Optional

from .base import BaseStrategy, MetricSnapshot


class CpuStrategy(BaseStrategy):
    name = "CPU"

    def __init__(
        self,
        scale_up_threshold: float = 70.0,
        scale_down_threshold: float = 30.0,
        scale_up_step: int = 1,
        scale_down_step: int = 1,
        min_replicas: int = 1,
        max_replicas: int = 10,
    ):
        super().__init__(min_replicas, max_replicas)
        self.scale_up_threshold = scale_up_threshold
        self.scale_down_threshold = scale_down_threshold
        self.scale_up_step = scale_up_step
        self.scale_down_step = scale_down_step

    def decide(self, snapshot: MetricSnapshot) -> Optional[int]:
        util = snapshot.utilization_pct
        r = snapshot.replicas
        if util > self.scale_up_threshold:
            return self.clamp(r + self.scale_up_step)
        if util < self.scale_down_threshold and r > self.min_replicas:
            return self.clamp(r - self.scale_down_step)
        return None

    @property
    def config(self) -> dict:
        return {
            "scale_up_threshold": self.scale_up_threshold,
            "scale_down_threshold": self.scale_down_threshold,
            "scale_up_step": self.scale_up_step,
            "scale_down_step": self.scale_down_step,
        }
