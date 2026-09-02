"""
WARPS — Workload-Aware Reactive Policy Selector.

The meta-controller observes a sliding window of W metric snapshots and
extracts three lightweight statistical features to classify the current
workload regime.  It then delegates to the most appropriate base strategy.

Features (computed over window of W snapshots):
  cv_util   — coefficient of variation of utilisation
              low → stable load; high → bursty or volatile
  slope     — linear-regression slope of utilisation over the window
              large positive → monotonic ramp-up
  p95_lat   — recent mean p95 latency
              above SLO_threshold → user impact already occurring

Classification rules (priority order):
  1. p95_lat > LATENCY_BUSY_MS             → LATENCY  (SLO already violated)
  2. slope   > TREND_SLOPE_THRESH          → TREND    (load trending up fast)
  3. cv_util < CV_STABLE_THRESH            → CPU      (stable, conservative)
  4. default                               → CPU

Strategy stickiness:
  Once a strategy is chosen, it stays active for at least
  STICKY_STEPS decision intervals to prevent rapid switching.
  
Cooldown:
  Applied externally by CooldownWrapper — not duplicated here.
"""

from __future__ import annotations

from collections import deque
from typing import Optional

import numpy as np

from .base import BaseStrategy, MetricSnapshot
from .cpu import CpuStrategy
from .trend import TrendStrategy
from .latency import LatencyStrategy


class WARPSStrategy(BaseStrategy):
    name = "WARPS"

    def __init__(
        self,
        window_size: int = 8,
        cv_stable_threshold: float = 0.20,
        trend_slope_threshold: float = 1.5,   # %/step
        latency_busy_ms: float = 200.0,
        sticky_steps: int = 4,
        min_replicas: int = 1,
        max_replicas: int = 10,
    ):
        super().__init__(min_replicas, max_replicas)
        self.window_size = window_size
        self.cv_stable_threshold = cv_stable_threshold
        self.trend_slope_threshold = trend_slope_threshold
        self.latency_busy_ms = latency_busy_ms
        self.sticky_steps = sticky_steps

        # Sub-strategies (no individual cooldowns — CooldownWrapper wraps WARPS)
        self._strategies: dict[str, BaseStrategy] = {
            "CPU":     CpuStrategy(min_replicas=min_replicas, max_replicas=max_replicas),
            "TREND":   TrendStrategy(min_replicas=min_replicas, max_replicas=max_replicas),
            "LATENCY": LatencyStrategy(min_replicas=min_replicas, max_replicas=max_replicas),
        }

        self._util_history: deque[float] = deque(maxlen=window_size)
        self._lat_history: deque[float]  = deque(maxlen=window_size)
        self._current_strategy: str      = "CPU"
        self._steps_on_strategy: int     = 0
        self.selection_log: list[tuple[float, str]] = []   # (timestamp, strategy)

    # ── Feature extraction ────────────────────────────────────────────────────

    def _classify(self) -> str:
        if len(self._util_history) < max(3, self.window_size // 2):
            return "CPU"   # not enough data yet

        util = np.array(self._util_history)
        lat  = np.array(self._lat_history)

        mean_util  = float(util.mean()) + 1e-9
        cv_util    = float(util.std()) / mean_util

        # Linear slope of utilisation (units: % per step)
        x = np.arange(len(util), dtype=float)
        slope = float(np.polyfit(x, util, 1)[0]) if len(util) >= 3 else 0.0

        mean_p95 = float(lat.mean())

        # Priority 1: latency SLO already under pressure
        if mean_p95 > self.latency_busy_ms:
            return "LATENCY"

        # Priority 2: load is trending upward fast
        if slope > self.trend_slope_threshold:
            return "TREND"

        # Priority 3: stable workload → conservative CPU signal
        if cv_util < self.cv_stable_threshold:
            return "CPU"

        # Default
        return "CPU"

    # ── Decision ──────────────────────────────────────────────────────────────

    def decide(self, snapshot: MetricSnapshot) -> Optional[int]:
        self._util_history.append(snapshot.utilization_pct)
        self._lat_history.append(snapshot.p95_latency_ms)

        # Possibly switch strategy (respecting sticky window)
        if self._steps_on_strategy >= self.sticky_steps:
            new_cls = self._classify()
            if new_cls != self._current_strategy:
                self._current_strategy = new_cls
                self._steps_on_strategy = 0
        else:
            self._steps_on_strategy += 1

        self.selection_log.append((snapshot.timestamp, self._current_strategy))
        return self._strategies[self._current_strategy].decide(snapshot)

    def reset(self) -> None:
        self._util_history.clear()
        self._lat_history.clear()
        self._current_strategy = "CPU"
        self._steps_on_strategy = 0
        self.selection_log.clear()

    @property
    def config(self) -> dict:
        return {
            "window_size": self.window_size,
            "cv_stable_threshold": self.cv_stable_threshold,
            "trend_slope_threshold": self.trend_slope_threshold,
            "latency_busy_ms": self.latency_busy_ms,
            "sticky_steps": self.sticky_steps,
        }
