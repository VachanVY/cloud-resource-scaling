"""
Trend-based proactive scaling.

Decision rule:
  trend_ratio = utilization[t] / utilization[t-1]

  IF trend_ratio > scale_up_threshold  (e.g. 1.20 → load grew 20 %)
      → add scale_up_step replicas          (early scale-up)
  IF trend_ratio < scale_down_threshold (e.g. 0.80 → load fell 20 %)
      → remove scale_down_step replicas     (early scale-down)

Intent: detect *direction of change* before the absolute threshold is
crossed, allowing the system to provision capacity ahead of saturation.
"""

from __future__ import annotations

from typing import Optional

from .base import BaseStrategy, MetricSnapshot


class TrendStrategy(BaseStrategy):
    name = "TREND"

    def __init__(
        self,
        scale_up_threshold: float = 1.20,
        scale_down_threshold: float = 0.80,
        scale_up_step: int = 1,
        scale_down_step: int = 1,
        min_replicas: int = 1,
        max_replicas: int = 10,
        min_utilization_for_trend: float = 10.0,
    ):
        super().__init__(min_replicas, max_replicas)
        self.scale_up_threshold = scale_up_threshold
        self.scale_down_threshold = scale_down_threshold
        self.scale_up_step = scale_up_step
        self.scale_down_step = scale_down_step
        # Only use trend signal when load is above this floor to avoid
        # oscillation when the service is essentially idle.
        self.min_utilization_for_trend = min_utilization_for_trend

    def decide(self, snapshot: MetricSnapshot) -> Optional[int]:
        r = snapshot.replicas
        if snapshot.utilization_pct < self.min_utilization_for_trend:
            return None   # ignore tiny fluctuations at idle

        tr = snapshot.trend_ratio
        if tr > self.scale_up_threshold:
            return self.clamp(r + self.scale_up_step)
        if tr < self.scale_down_threshold and r > self.min_replicas:
            return self.clamp(r - self.scale_down_step)
        return None

    @property
    def config(self) -> dict:
        return {
            "scale_up_threshold": self.scale_up_threshold,
            "scale_down_threshold": self.scale_down_threshold,
            "scale_up_step": self.scale_up_step,
            "scale_down_step": self.scale_down_step,
            "min_utilization_for_trend": self.min_utilization_for_trend,
        }
