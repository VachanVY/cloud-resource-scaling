"""Base classes shared by all scaling strategies."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MetricSnapshot:
    """All signals available to every strategy at a given decision point."""
    timestamp: float
    replicas: int
    utilization_pct: float     # active / capacity × 100
    mean_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    request_rate: float        # requests per second (recent window)
    error_rate: float          # fraction of 503 responses
    # Computed by the experiment runner from prior snapshots:
    trend_ratio: float = 1.0   # utilization[t] / utilization[t-1]
    cv_utilization: float = 0.0  # std/mean over sliding window


@dataclass
class ScalingDecision:
    strategy: str
    timestamp: float
    old_replicas: int
    new_replicas: int
    reason: str
    metric_value: float
    direction: str = field(init=False)

    def __post_init__(self):
        if self.new_replicas > self.old_replicas:
            self.direction = "UP"
        elif self.new_replicas < self.old_replicas:
            self.direction = "DOWN"
        else:
            self.direction = "NONE"


class BaseStrategy:
    """Abstract scaling strategy.  Subclasses implement ``decide()``."""

    name: str = "BASE"

    def __init__(self, min_replicas: int = 1, max_replicas: int = 10):
        self.min_replicas = min_replicas
        self.max_replicas = max_replicas

    def decide(self, snapshot: MetricSnapshot) -> Optional[int]:
        """Return desired replica count, or None if no change needed."""
        raise NotImplementedError

    def clamp(self, n: int) -> int:
        return max(self.min_replicas, min(self.max_replicas, n))


class CooldownWrapper(BaseStrategy):
    """
    Wraps any BaseStrategy with separate scale-up / scale-down cooldowns.
    
    This is applied to all strategies in the experiment, including WARPS, 
    so that cooldown behaviour is a controlled variable and not a confound.
    """

    def __init__(
        self,
        inner: BaseStrategy,
        cooldown_up_s: float = 15.0,
        cooldown_down_s: float = 30.0,
    ):
        super().__init__(inner.min_replicas, inner.max_replicas)
        self.inner = inner
        self.name = inner.name
        self.cooldown_up_s = cooldown_up_s
        self.cooldown_down_s = cooldown_down_s
        self._last_scale_up: float = 0.0
        self._last_scale_down: float = 0.0

    def decide(self, snapshot: MetricSnapshot) -> Optional[int]:
        desired = self.inner.decide(snapshot)
        if desired is None:
            return None
        now = time.monotonic()
        if desired > snapshot.replicas:
            if now - self._last_scale_up < self.cooldown_up_s:
                return None   # still in cooldown
            self._last_scale_up = now
            return self.clamp(desired)
        if desired < snapshot.replicas:
            if now - self._last_scale_down < self.cooldown_down_s:
                return None
            self._last_scale_down = now
            return self.clamp(desired)
        return None

    def reset(self):
        self._last_scale_up = 0.0
        self._last_scale_down = 0.0
        if hasattr(self.inner, "reset"):
            self.inner.reset()
