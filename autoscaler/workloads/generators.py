"""
Reproducible synthetic workload generators.

Each generator produces a *target RPS* (requests per second) for each
second of the experiment.  The load generator then tries to send exactly
that many requests during that second.

All generators accept a ``seed`` for reproducibility and expose a
``rps_at(t, total_duration)`` method returning a float RPS value.

Workload classes
----------------
W1  CONSTANT    — steady background load; tests scale-down behaviour.
W2  STEP_SPIKE  — sudden step from low to high RPS; tests reactive speed.
W3  RAMP        — linear increase over full duration; tests proactive scaling.
W4  PERIODIC    — sinusoidal oscillation; tests oscillation control.
W5  BURSTY      — Pareto-distributed on/off bursts; tests burst handling.
"""

from __future__ import annotations

import math
import random
from typing import ClassVar


class BaseWorkload:
    name: ClassVar[str] = "BASE"
    description: ClassVar[str] = ""

    def __init__(self, seed: int = 42):
        self.seed = seed
        self._rng = random.Random(seed)

    def rps_at(self, t: float, total_duration: float) -> float:
        """Return target RPS at time t (seconds into experiment)."""
        raise NotImplementedError

    def rps_sequence(self, total_duration: float, step_s: float = 1.0) -> list[float]:
        """Return list of RPS values, one per step_s interval."""
        n = int(total_duration / step_s)
        return [self.rps_at(i * step_s, total_duration) for i in range(n)]

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(seed={self.seed})"


# ── W1: Constant ──────────────────────────────────────────────────────────────

class ConstantWorkload(BaseWorkload):
    """
    Fixed RPS throughout.  Low enough for 2 replicas to handle comfortably.
    Tests that strategies do NOT unnecessarily scale up.
    """
    name = "W1_CONSTANT"
    description = "Constant load at 15 RPS — tests scale-stability"

    def __init__(self, rps: float = 15.0, seed: int = 42):
        super().__init__(seed)
        self.rps = rps

    def rps_at(self, t: float, total_duration: float) -> float:
        return self.rps


# ── W2: Step spike ────────────────────────────────────────────────────────────

class StepSpikeWorkload(BaseWorkload):
    """
    Sudden step from low to high RPS at fraction ``step_fraction`` of total
    duration.  Tests how quickly each strategy responds to an abrupt spike.
    """
    name = "W2_STEP_SPIKE"
    description = "10→55 RPS step at 33% of duration — tests reactive speed"

    def __init__(
        self,
        rps_low: float = 10.0,
        rps_high: float = 55.0,
        step_fraction: float = 0.33,
        seed: int = 42,
    ):
        super().__init__(seed)
        self.rps_low = rps_low
        self.rps_high = rps_high
        self.step_fraction = step_fraction

    def rps_at(self, t: float, total_duration: float) -> float:
        return self.rps_high if t >= total_duration * self.step_fraction else self.rps_low


# ── W3: Ramp ──────────────────────────────────────────────────────────────────

class RampWorkload(BaseWorkload):
    """
    Linear ramp from rps_start to rps_end.  Tests proactive (TREND) scaling:
    a trend-aware strategy should scale ahead of saturation.
    """
    name = "W3_RAMP"
    description = "5→65 RPS linear ramp — tests proactive trend scaling"

    def __init__(self, rps_start: float = 5.0, rps_end: float = 65.0, seed: int = 42):
        super().__init__(seed)
        self.rps_start = rps_start
        self.rps_end = rps_end

    def rps_at(self, t: float, total_duration: float) -> float:
        if total_duration <= 0:
            return self.rps_start
        frac = min(1.0, t / total_duration)
        return self.rps_start + frac * (self.rps_end - self.rps_start)


# ── W4: Periodic ─────────────────────────────────────────────────────────────

class PeriodicWorkload(BaseWorkload):
    """
    Sinusoidal oscillation.  Tests oscillation / scaling stability:
    strategies that over-react will rack up unnecessary scaling events.
    """
    name = "W4_PERIODIC"
    description = "25±18 RPS sinusoidal (T=20 s) — tests oscillation control"

    def __init__(
        self,
        rps_mean: float = 25.0,
        rps_amplitude: float = 18.0,
        period_s: float = 20.0,
        seed: int = 42,
    ):
        super().__init__(seed)
        self.rps_mean = rps_mean
        self.rps_amplitude = rps_amplitude
        self.period_s = period_s

    def rps_at(self, t: float, total_duration: float) -> float:
        return max(1.0, self.rps_mean + self.rps_amplitude * math.sin(2 * math.pi * t / self.period_s))


# ── W5: Bursty ────────────────────────────────────────────────────────────────

class BurstyWorkload(BaseWorkload):
    """
    Alternates between a low base rate and high-intensity bursts.  Burst
    timing is drawn from a Pareto distribution (heavy-tailed inter-arrivals).
    Tests latency-aware and WARPS strategies under unpredictable spikes.
    """
    name = "W5_BURSTY"
    description = "10/60 RPS Pareto-burst (p=0.25) — tests burst handling"

    def __init__(
        self,
        rps_base: float = 10.0,
        rps_burst: float = 60.0,
        burst_probability: float = 0.25,
        seed: int = 42,
    ):
        super().__init__(seed)
        self.rps_base = rps_base
        self.rps_burst = rps_burst
        self.burst_probability = burst_probability
        # Pre-generate burst schedule so rps_at() is deterministic
        self._schedule: list[bool] = []

    def _ensure_schedule(self, n: int) -> None:
        while len(self._schedule) < n:
            # Pareto-like: burst runs last Pareto(1, 3) steps
            if not self._schedule or not self._schedule[-1]:
                in_burst = self._rng.random() < self.burst_probability
            else:
                # If currently in a burst, end it with probability 0.4/step
                in_burst = self._rng.random() > 0.40
            self._schedule.append(in_burst)

    def rps_at(self, t: float, total_duration: float) -> float:
        idx = int(t)
        self._ensure_schedule(idx + 1)
        return self.rps_burst if self._schedule[idx] else self.rps_base

    def reset(self, seed: int | None = None) -> None:
        if seed is not None:
            self.seed = seed
        self._rng = random.Random(self.seed)
        self._schedule.clear()


# ── Registry ──────────────────────────────────────────────────────────────────

WORKLOAD_REGISTRY: dict[str, type[BaseWorkload]] = {
    "W1_CONSTANT":   ConstantWorkload,
    "W2_STEP_SPIKE": StepSpikeWorkload,
    "W3_RAMP":       RampWorkload,
    "W4_PERIODIC":   PeriodicWorkload,
    "W5_BURSTY":     BurstyWorkload,
}
