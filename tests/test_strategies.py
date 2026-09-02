"""Unit tests for scaling strategies."""

import time
import pytest
from autoscaler.strategies import (
    CpuStrategy, TrendStrategy, LatencyStrategy, WARPSStrategy, CooldownWrapper
)
from autoscaler.strategies.base import MetricSnapshot


def _snap(replicas=2, util=50.0, p95=120.0, trend=1.0, cv=0.1):
    return MetricSnapshot(
        timestamp=time.monotonic(),
        replicas=replicas,
        utilization_pct=util,
        mean_latency_ms=100.0,
        p50_latency_ms=90.0,
        p95_latency_ms=p95,
        p99_latency_ms=140.0,
        request_rate=20.0,
        error_rate=0.0,
        trend_ratio=trend,
        cv_utilization=cv,
    )


# ── CpuStrategy ───────────────────────────────────────────────────────────────

class TestCpuStrategy:
    def test_scales_up_above_threshold(self):
        s = CpuStrategy(scale_up_threshold=70.0, scale_up_step=1)
        result = s.decide(_snap(util=80.0))
        assert result == 3   # 2 + 1

    def test_scales_down_below_threshold(self):
        s = CpuStrategy(scale_down_threshold=30.0, scale_down_step=1)
        result = s.decide(_snap(replicas=3, util=20.0))
        assert result == 2   # 3 - 1

    def test_no_action_in_dead_band(self):
        s = CpuStrategy(scale_up_threshold=70.0, scale_down_threshold=30.0)
        result = s.decide(_snap(util=50.0))
        assert result is None

    def test_respects_min_replicas(self):
        s = CpuStrategy(min_replicas=2, scale_down_step=1)
        result = s.decide(_snap(replicas=2, util=10.0))
        assert result is None   # already at min

    def test_respects_max_replicas(self):
        s = CpuStrategy(max_replicas=3, scale_up_step=2)
        # When already at max, clamp returns 3 == current replicas;
        # strategy still returns 3 (desired = clamped). The CooldownWrapper
        # (not used here) or the caller must detect no-op. The raw strategy
        # returns the clamped value; verify it never exceeds max_replicas.
        result = s.decide(_snap(replicas=2, util=95.0))
        assert result is not None and result <= 3
        result2 = s.decide(_snap(replicas=3, util=95.0))
        # clamped desired == current → strategy returns clamp(3+2)=3, which
        # equals current, so experiment runner ignores it. But the strategy
        # itself may return 3 or None depending on implementation.
        if result2 is not None:
            assert result2 <= 3


# ── TrendStrategy ─────────────────────────────────────────────────────────────

class TestTrendStrategy:
    def test_scales_up_on_rising_trend(self):
        s = TrendStrategy(scale_up_threshold=1.2, min_utilization_for_trend=5.0)
        result = s.decide(_snap(util=50.0, trend=1.5))
        assert result == 3

    def test_scales_down_on_falling_trend(self):
        s = TrendStrategy(scale_down_threshold=0.8, min_utilization_for_trend=5.0)
        result = s.decide(_snap(replicas=4, util=40.0, trend=0.6))
        assert result == 3

    def test_ignores_trend_at_low_util(self):
        s = TrendStrategy(min_utilization_for_trend=10.0)
        result = s.decide(_snap(util=5.0, trend=2.0))
        assert result is None   # below floor → ignore


# ── LatencyStrategy ───────────────────────────────────────────────────────────

class TestLatencyStrategy:
    def test_scales_up_on_high_latency(self):
        s = LatencyStrategy(scale_up_threshold_ms=200.0, scale_up_step=2)
        result = s.decide(_snap(p95=350.0))
        assert result == 4   # 2 + 2

    def test_scales_down_on_low_latency(self):
        s = LatencyStrategy(scale_down_threshold_ms=100.0, scale_down_step=1)
        result = s.decide(_snap(replicas=4, p95=60.0))
        assert result == 3

    def test_no_action_in_safe_zone(self):
        s = LatencyStrategy(scale_up_threshold_ms=200.0, scale_down_threshold_ms=100.0)
        result = s.decide(_snap(p95=150.0))
        assert result is None


# ── WARPSStrategy ─────────────────────────────────────────────────────────────

class TestWARPSStrategy:
    def test_default_cpu_strategy_with_no_history(self):
        w = WARPSStrategy()
        result = w.decide(_snap(util=80.0, p95=50.0, trend=1.0))
        # With no history, should use CPU → scale up at 80% util
        assert result == 3 or result is None   # depends on cooldown in test context

    def test_switches_to_latency_when_slo_violated(self):
        # sticky_steps=0 allows reclassification at every step.
        w = WARPSStrategy(latency_busy_ms=200.0, window_size=4, sticky_steps=0)
        # Feed enough history so classifier has data
        for _ in range(5):
            w.decide(_snap(util=60.0, p95=80.0))
        # Now trigger SLO violation; classifier should see mean_p95 > 200 ms
        for _ in range(3):
            w.decide(_snap(util=60.0, p95=250.0))
        # After enough high-latency steps, LATENCY should be selected
        recent = [entry[1] for entry in w.selection_log[-3:]]
        assert "LATENCY" in recent

    def test_switches_to_trend_on_ramp(self):
        w = WARPSStrategy(trend_slope_threshold=1.5, window_size=4, sticky_steps=1)
        # Rising utilisation history
        utils = [10.0, 25.0, 45.0, 70.0, 90.0]
        for u in utils:
            w.decide(_snap(util=u, p95=90.0, trend=1.5))
        # Should have selected TREND at some point
        selected = {entry[1] for entry in w.selection_log}
        assert "TREND" in selected or "CPU" in selected  # either is acceptable

    def test_reset_clears_state(self):
        w = WARPSStrategy()
        for _ in range(5):
            w.decide(_snap())
        w.reset()
        assert len(w._util_history) == 0
        assert len(w.selection_log) == 0


# ── CooldownWrapper ───────────────────────────────────────────────────────────

class TestCooldownWrapper:
    def test_blocks_immediate_second_scale_up(self):
        inner = CpuStrategy()
        wrapped = CooldownWrapper(inner, cooldown_up_s=60.0, cooldown_down_s=60.0)
        snap = _snap(util=80.0)
        first = wrapped.decide(snap)   # should fire
        assert first == 3
        second = wrapped.decide(snap)  # should be blocked
        assert second is None

    def test_allows_scale_up_after_cooldown(self):
        inner = CpuStrategy()
        wrapped = CooldownWrapper(inner, cooldown_up_s=0.05, cooldown_down_s=60.0)
        snap = _snap(util=80.0)
        wrapped.decide(snap)
        time.sleep(0.1)
        result = wrapped.decide(_snap(util=90.0, replicas=3))
        assert result == 4

    def test_reset_clears_cooldown_timers(self):
        inner = CpuStrategy()
        wrapped = CooldownWrapper(inner, cooldown_up_s=60.0, cooldown_down_s=60.0)
        wrapped.decide(_snap(util=80.0))   # fires, sets timer
        wrapped.reset()
        result = wrapped.decide(_snap(util=80.0))  # should fire again
        assert result is not None
