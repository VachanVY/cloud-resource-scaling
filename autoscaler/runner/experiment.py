"""
Single-experiment runner.

Orchestrates one (strategy, workload, repetition) trial:

  1. Reset target service to initial_replicas.
  2. Start load generator at initial RPS.
  3. Every SCALING_INTERVAL seconds:
       a. Drain request records from load generator.
       b. Compute MetricSnapshot (latency percentiles, utilisation, trend).
       c. Run scaling strategy → decide new replica count.
       d. If changed: call PUT /control/replicas/<n> on target service.
       e. Append step record to timeline.
  4. After WARMUP + MEASUREMENT seconds: stop load generator, collect results.
  5. Return ExperimentResult with full timeline and summary statistics.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

import numpy as np
import requests as http_lib

from ..strategies.base import BaseStrategy, CooldownWrapper, MetricSnapshot, ScalingDecision
from ..workloads.generators import BaseWorkload
from .load_gen import LoadGenerator, RequestRecord

# ── Config ────────────────────────────────────────────────────────────────────

SERVICE_PORT = int(os.environ.get("SERVICE_PORT", "8100"))
SERVICE_URL  = f"http://127.0.0.1:{SERVICE_PORT}"
SCALING_INTERVAL_S   = float(os.environ.get("SCALING_INTERVAL_S",  "5.0"))
WARMUP_S             = float(os.environ.get("WARMUP_S",             "15.0"))
MEASUREMENT_S        = float(os.environ.get("MEASUREMENT_S",        "60.0"))
INITIAL_REPLICAS     = int(os.environ.get("INITIAL_REPLICAS",       "2"))
SLA_THRESHOLD_MS     = float(os.environ.get("SLA_THRESHOLD_MS",     "200.0"))
COOLDOWN_UP_S        = float(os.environ.get("COOLDOWN_UP_S",        "15.0"))
COOLDOWN_DOWN_S      = float(os.environ.get("COOLDOWN_DOWN_S",      "30.0"))
COST_PER_REPLICA_HOUR = 0.023   # USD, t3.small equivalent (labeled as simulated)


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class StepRecord:
    """Metrics + decision at one scaling-interval timestep."""
    step: int
    timestamp: float
    elapsed_s: float
    target_rps: float
    actual_rps: float
    replicas: int
    utilization_pct: float
    mean_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    error_rate: float
    n_requests: int
    trend_ratio: float
    cv_utilization: float
    scaling_decision: Optional[str] = None   # "UP" / "DOWN" / None
    warps_selected_strategy: Optional[str] = None


@dataclass
class ExperimentResult:
    strategy_name: str
    workload_name: str
    repetition: int
    seed: int
    # Summary statistics (measurement window only)
    n_requests: int = 0
    n_failed: int = 0
    sla_violation_rate: float = 0.0
    mean_latency_ms: float = 0.0
    p50_latency_ms: float = 0.0
    p95_latency_ms: float = 0.0
    p99_latency_ms: float = 0.0
    mean_utilization_pct: float = 0.0
    peak_replicas: int = 0
    mean_replicas: float = 0.0
    replica_hours: float = 0.0     # simulated cost basis
    cost_usd: float = 0.0          # labeled simulated
    n_scale_up: int = 0
    n_scale_down: int = 0
    n_oscillations: int = 0        # scale-up immediately after scale-down or vice versa
    mean_p95_latency_ms: float = 0.0
    # Full timeline (all steps including warmup)
    timeline: list[StepRecord] = field(default_factory=list)
    # Metadata
    warmup_s: float = WARMUP_S
    measurement_s: float = MEASUREMENT_S
    scaling_interval_s: float = SCALING_INTERVAL_S
    sla_threshold_ms: float = SLA_THRESHOLD_MS
    initial_replicas: int = INITIAL_REPLICAS

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timeline"] = [asdict(s) for s in self.timeline]
        return d


# ── Helpers ───────────────────────────────────────────────────────────────────

def _percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    return float(np.percentile(values, p))


def _compute_snapshot(
    records: list[RequestRecord],
    replicas: int,
    threads_per_replica: int,
    prev_util: float,
    util_history: deque,
    timestamp: float,
    target_rps: float,
    elapsed: float,
) -> MetricSnapshot:
    capacity = replicas * threads_per_replica
    latencies = [r.latency_ms for r in records if r.status_code == 200]
    errors    = [r for r in records if r.status_code != 200]
    n = len(records)

    if latencies:
        mean_lat = float(np.mean(latencies))
        p50      = float(np.percentile(latencies, 50))
        p95      = float(np.percentile(latencies, 95))
        p99      = float(np.percentile(latencies, 99))
    else:
        mean_lat = p50 = p95 = p99 = 0.0

    # Utilisation proxy: use active worker fraction from service metrics if possible
    # Fall back to derived estimate from request rate and capacity
    interval = SCALING_INTERVAL_S
    actual_rps = n / interval if interval > 0 else 0.0
    # avg concurrent = arrival_rate × service_time
    # service_time ≈ mean_lat (ms → sec)
    service_time_s = (mean_lat / 1000.0) if mean_lat > 0 else 0.08
    estimated_active = actual_rps * service_time_s
    utilization = min(100.0, estimated_active / max(capacity, 1) * 100)

    util_history.append(utilization)
    trend_ratio = utilization / max(prev_util, 1.0) if prev_util > 0 else 1.0

    if len(util_history) >= 2:
        arr = np.array(util_history)
        cv = float(arr.std()) / (float(arr.mean()) + 1e-9)
    else:
        cv = 0.0

    error_rate = len(errors) / max(n, 1)

    return MetricSnapshot(
        timestamp=timestamp,
        replicas=replicas,
        utilization_pct=utilization,
        mean_latency_ms=mean_lat,
        p50_latency_ms=p50,
        p95_latency_ms=p95,
        p99_latency_ms=p99,
        request_rate=actual_rps,
        error_rate=error_rate,
        trend_ratio=trend_ratio,
        cv_utilization=cv,
    )


def _count_oscillations(decisions: list[str]) -> int:
    """Count direction reversals (UP→DOWN or DOWN→UP back-to-back)."""
    count = 0
    for i in range(1, len(decisions)):
        a, b = decisions[i - 1], decisions[i]
        if (a == "UP" and b == "DOWN") or (a == "DOWN" and b == "UP"):
            count += 1
    return count


# ── Service management ────────────────────────────────────────────────────────

_service_proc: Optional[subprocess.Popen] = None

def start_service() -> None:
    global _service_proc
    if _service_proc is not None and _service_proc.poll() is None:
        return   # already running
    env = os.environ.copy()
    _service_proc = subprocess.Popen(
        [sys.executable, "target_app/app.py", str(SERVICE_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    # Wait until healthy
    for _ in range(30):
        time.sleep(0.5)
        try:
            r = http_lib.get(f"{SERVICE_URL}/health", timeout=1.0)
            if r.status_code == 200:
                return
        except Exception:  # noqa: BLE001
            pass
    raise RuntimeError("Target service did not become healthy within 15 s")


def stop_service() -> None:
    global _service_proc
    if _service_proc is not None:
        _service_proc.terminate()
        try:
            _service_proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _service_proc.kill()
        _service_proc = None


def reset_service(initial_replicas: int = INITIAL_REPLICAS) -> None:
    http_lib.post(
        f"{SERVICE_URL}/control/reset",
        json={"initial_replicas": initial_replicas},
        timeout=3.0,
    )


def _set_replicas(n: int) -> None:
    try:
        http_lib.put(f"{SERVICE_URL}/control/replicas/{n}", timeout=2.0)
    except Exception:  # noqa: BLE001
        pass


# ── Main runner ───────────────────────────────────────────────────────────────

class ExperimentRunner:
    """Runs a single (strategy, workload, seed) experiment trial."""

    def __init__(
        self,
        strategy: BaseStrategy,
        workload: BaseWorkload,
        threads_per_replica: int = 2,
        results_dir: Optional[Path] = None,
    ):
        self.threads_per_replica = threads_per_replica
        self.results_dir = results_dir or Path("results/raw")
        self.results_dir.mkdir(parents=True, exist_ok=True)

        # Wrap strategy with cooldown
        if not isinstance(strategy, CooldownWrapper):
            self.strategy = CooldownWrapper(
                strategy,
                cooldown_up_s=COOLDOWN_UP_S,
                cooldown_down_s=COOLDOWN_DOWN_S,
            )
        else:
            self.strategy = strategy
        self.workload = workload

    def run(self, repetition: int = 1) -> ExperimentResult:
        seed = self.workload.seed
        total_duration = WARMUP_S + MEASUREMENT_S

        result = ExperimentResult(
            strategy_name=self.strategy.name,
            workload_name=self.workload.name,
            repetition=repetition,
            seed=seed,
        )

        # Reset service and strategy state
        reset_service(INITIAL_REPLICAS)
        if hasattr(self.strategy, "reset"):
            self.strategy.reset()
        if hasattr(self.strategy.inner, "reset"):
            self.strategy.inner.reset()

        load_gen = LoadGenerator(base_url=SERVICE_URL)
        # Get initial RPS
        initial_rps = self.workload.rps_at(0, total_duration)
        load_gen.start(initial_rps)

        experiment_start = time.monotonic()
        prev_util: float = 1.0
        util_history: deque = deque(maxlen=10)
        replicas: int = INITIAL_REPLICAS
        step: int = 0
        decisions_dir: list[str] = []
        n_scale_up = n_scale_down = 0

        # Measurement-window accumulators
        meas_latencies: list[float] = []
        meas_failed: int = 0
        meas_total: int = 0
        meas_replicas: list[int] = []
        meas_p95s: list[float] = []
        sla_violations: int = 0
        in_measurement = False

        try:
            while True:
                elapsed = time.monotonic() - experiment_start
                if elapsed >= total_duration:
                    break

                # Update target RPS for this step
                target_rps = self.workload.rps_at(elapsed, total_duration)
                load_gen.set_rps(target_rps)

                # Sleep one scaling interval
                time.sleep(SCALING_INTERVAL_S)
                elapsed = time.monotonic() - experiment_start

                # Collect metrics
                records = load_gen.drain_records()
                snapshot = _compute_snapshot(
                    records, replicas, self.threads_per_replica,
                    prev_util, util_history, elapsed, target_rps, elapsed,
                )
                prev_util = snapshot.utilization_pct

                # Scaling decision
                desired = self.strategy.decide(snapshot)
                decision_dir: Optional[str] = None
                if desired is not None and desired != replicas:
                    old_r = replicas
                    replicas = desired
                    _set_replicas(replicas)
                    decision_dir = "UP" if replicas > old_r else "DOWN"
                    if decision_dir == "UP":
                        n_scale_up += 1
                    else:
                        n_scale_down += 1
                    decisions_dir.append(decision_dir)

                # WARPS strategy selection tag
                warps_tag: Optional[str] = None
                if self.strategy.name == "WARPS":
                    inner = self.strategy.inner
                    if hasattr(inner, "selection_log") and inner.selection_log:
                        warps_tag = inner.selection_log[-1][1]

                step_rec = StepRecord(
                    step=step,
                    timestamp=elapsed,
                    elapsed_s=elapsed,
                    target_rps=target_rps,
                    actual_rps=snapshot.request_rate,
                    replicas=replicas,
                    utilization_pct=snapshot.utilization_pct,
                    mean_latency_ms=snapshot.mean_latency_ms,
                    p50_latency_ms=snapshot.p50_latency_ms,
                    p95_latency_ms=snapshot.p95_latency_ms,
                    p99_latency_ms=snapshot.p99_latency_ms,
                    error_rate=snapshot.error_rate,
                    n_requests=len(records),
                    trend_ratio=snapshot.trend_ratio,
                    cv_utilization=snapshot.cv_utilization,
                    scaling_decision=decision_dir,
                    warps_selected_strategy=warps_tag,
                )
                result.timeline.append(step_rec)
                step += 1

                # Accumulate measurement-window stats
                if elapsed >= WARMUP_S:
                    in_measurement = True
                    ok_lats = [r.latency_ms for r in records if r.status_code == 200]
                    fail_ct = sum(1 for r in records if r.status_code != 200)
                    meas_latencies.extend(ok_lats)
                    meas_failed += fail_ct
                    meas_total += len(records)
                    meas_replicas.append(replicas)
                    meas_p95s.append(snapshot.p95_latency_ms)
                    sla_violations += sum(1 for l in ok_lats if l > SLA_THRESHOLD_MS)

        finally:
            load_gen.stop()

        # ── Compute summary statistics ────────────────────────────────────────
        result.n_requests = meas_total
        result.n_failed   = meas_failed
        result.sla_violation_rate = sla_violations / max(meas_total, 1)
        result.n_scale_up    = n_scale_up
        result.n_scale_down  = n_scale_down
        result.n_oscillations = _count_oscillations(decisions_dir)

        if meas_latencies:
            arr = np.array(meas_latencies)
            result.mean_latency_ms = float(arr.mean())
            result.p50_latency_ms  = float(np.percentile(arr, 50))
            result.p95_latency_ms  = float(np.percentile(arr, 95))
            result.p99_latency_ms  = float(np.percentile(arr, 99))

        result.mean_p95_latency_ms = float(np.mean(meas_p95s)) if meas_p95s else 0.0

        if meas_replicas:
            result.peak_replicas       = max(meas_replicas)
            result.mean_replicas       = float(np.mean(meas_replicas))
            result.mean_utilization_pct = (
                sum(s.utilization_pct for s in result.timeline if s.elapsed_s >= WARMUP_S)
                / max(1, sum(1 for s in result.timeline if s.elapsed_s >= WARMUP_S))
            )
            # Replica-hours (simulated cost basis, clearly labeled)
            result.replica_hours = result.mean_replicas * (MEASUREMENT_S / 3600.0)
            result.cost_usd      = result.replica_hours * COST_PER_REPLICA_HOUR

        # Persist result
        fname = (
            f"{result.strategy_name}_{result.workload_name}"
            f"_rep{result.repetition:02d}_seed{result.seed}.json"
        )
        out_path = self.results_dir / fname
        with open(out_path, "w") as fh:
            json.dump(result.to_dict(), fh, indent=2)

        return result
