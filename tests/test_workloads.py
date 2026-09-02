"""Unit tests for workload generators."""

import pytest
from autoscaler.workloads.generators import (
    ConstantWorkload, StepSpikeWorkload, RampWorkload,
    PeriodicWorkload, BurstyWorkload, WORKLOAD_REGISTRY,
)


class TestConstantWorkload:
    def test_constant_rps(self):
        w = ConstantWorkload(rps=20.0)
        for t in [0, 10, 30, 59]:
            assert w.rps_at(t, 60) == 20.0

    def test_sequence_length(self):
        w = ConstantWorkload(rps=10.0)
        seq = w.rps_sequence(60, step_s=1.0)
        assert len(seq) == 60


class TestStepSpikeWorkload:
    def test_low_before_step(self):
        w = StepSpikeWorkload(rps_low=10.0, rps_high=50.0, step_fraction=0.33)
        assert w.rps_at(0, 60) == 10.0
        assert w.rps_at(15, 60) == 10.0   # 15/60 = 0.25 < 0.33

    def test_high_after_step(self):
        w = StepSpikeWorkload(rps_low=10.0, rps_high=50.0, step_fraction=0.33)
        assert w.rps_at(25, 60) == 50.0   # 25/60 = 0.42 > 0.33

    def test_peak_rps_positive(self):
        w = StepSpikeWorkload()
        seq = w.rps_sequence(90, step_s=1.0)
        assert max(seq) > min(seq)


class TestRampWorkload:
    def test_starts_at_low(self):
        w = RampWorkload(rps_start=5.0, rps_end=65.0)
        assert w.rps_at(0, 60) == pytest.approx(5.0, abs=0.1)

    def test_ends_at_high(self):
        w = RampWorkload(rps_start=5.0, rps_end=65.0)
        assert w.rps_at(60, 60) == pytest.approx(65.0, abs=0.1)

    def test_monotonically_increasing(self):
        w = RampWorkload()
        seq = w.rps_sequence(60)
        for i in range(1, len(seq)):
            assert seq[i] >= seq[i - 1]


class TestPeriodicWorkload:
    def test_within_amplitude_bounds(self):
        w = PeriodicWorkload(rps_mean=25.0, rps_amplitude=18.0)
        for t in range(60):
            rps = w.rps_at(t, 60)
            assert rps >= 1.0          # clipped at 1
            assert rps <= 43.0 + 0.1  # 25 + 18

    def test_period_matches(self):
        w = PeriodicWorkload(rps_mean=25.0, rps_amplitude=18.0, period_s=10.0)
        # Values at t=0 and t=10 should be approximately the same
        assert abs(w.rps_at(0, 60) - w.rps_at(10, 60)) < 0.01


class TestBurstyWorkload:
    def test_values_are_base_or_burst(self):
        w = BurstyWorkload(rps_base=10.0, rps_burst=60.0)
        for t in range(60):
            rps = w.rps_at(t, 60)
            assert rps in (10.0, 60.0)

    def test_reproducible_with_seed(self):
        w1 = BurstyWorkload(seed=99)
        w2 = BurstyWorkload(seed=99)
        seq1 = w1.rps_sequence(60)
        seq2 = w2.rps_sequence(60)
        assert seq1 == seq2

    def test_different_seeds_differ(self):
        w1 = BurstyWorkload(seed=1)
        w2 = BurstyWorkload(seed=2)
        seq1 = w1.rps_sequence(60)
        seq2 = w2.rps_sequence(60)
        assert seq1 != seq2


class TestRegistry:
    def test_all_classes_registered(self):
        expected = {"W1_CONSTANT", "W2_STEP_SPIKE", "W3_RAMP", "W4_PERIODIC", "W5_BURSTY"}
        assert set(WORKLOAD_REGISTRY.keys()) == expected

    def test_all_classes_instantiable(self):
        for name, cls in WORKLOAD_REGISTRY.items():
            w = cls(seed=0)
            rps = w.rps_at(10, 60)
            assert rps > 0, f"{name} returned non-positive RPS"
