"""
Latency / SLO-aware reactive scaling.

Decision rule:
  IF p95_latency_ms > scale_up_threshold_ms   → add scale_up_step replicas
  IF p95_latency_ms < scale_down_threshold_ms → remove scale_down_step replicas

Intent: use the *user-visible* metric (response time) rather than an
internal signal.  Scales more aggressively (larger step) to recover fast
from SLA violations.
"""

from __future__ import annotations

from typing import Optional

from .base import BaseStrategy, MetricSnapshot


class LatencyStrategy(BaseStrategy):
    name = "LATENCY"

    def __init__(
        self,
        scale_up_threshold_ms: float = 200.0,
        scale_down_threshold_ms: float = 100.0,
        scale_up_step: int = 2,
        scale_down_step: int = 1,
        min_replicas: int = 1,
        max_replicas: int = 10,
    ):
        super().__init__(min_replicas, max_replicas)
        self.scale_up_threshold_ms = scale_up_threshold_ms
        self.scale_down_threshold_ms = scale_down_threshold_ms
        self.scale_up_step = scale_up_step
        self.scale_down_step = scale_down_step

    def decide(self, snapshot: MetricSnapshot) -> Optional[int]:
        p95 = snapshot.p95_latency_ms
        r = snapshot.replicas

        # If we have no latency data yet, fall back to utilisation guard
        if p95 == 0.0:
            if snapshot.utilization_pct > 85.0:
                return self.clamp(r + self.scale_up_step)
            return None

        if p95 > self.scale_up_threshold_ms:
            return self.clamp(r + self.scale_up_step)
        if p95 < self.scale_down_threshold_ms and r > self.min_replicas:
            return self.clamp(r - self.scale_down_step)
        return None

    @property
    def config(self) -> dict:
        return {
            "scale_up_threshold_ms": self.scale_up_threshold_ms,
            "scale_down_threshold_ms": self.scale_down_threshold_ms,
            "scale_up_step": self.scale_up_step,
            "scale_down_step": self.scale_down_step,
        }
