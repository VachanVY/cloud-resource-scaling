"""
Batch experiment runner.

Iterates over the full (strategy × workload × repetition) matrix,
calls ExperimentRunner for each cell, and prints a live progress table.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from ..strategies import CpuStrategy, TrendStrategy, LatencyStrategy, WARPSStrategy, CooldownWrapper
from ..workloads.generators import WORKLOAD_REGISTRY, BaseWorkload
from .experiment import (
    ExperimentRunner, ExperimentResult,
    start_service, stop_service,
    WARMUP_S, MEASUREMENT_S, SCALING_INTERVAL_S,
    COOLDOWN_UP_S, COOLDOWN_DOWN_S,
)


# ── Strategy factory ──────────────────────────────────────────────────────────

def _make_strategy(name: str):
    """Return a fresh un-wrapped strategy instance."""
    if name == "STATIC":
        # B0: never scales (wrap with CooldownWrapper that always blocks via
        # an inner strategy that always returns None)
        class _NullStrategy:
            name = "STATIC"
            min_replicas = 3
            max_replicas = 3
            def decide(self, _snap):
                return None
            def reset(self): pass
        return _NullStrategy()
    if name == "CPU":
        return CpuStrategy()
    if name == "TREND":
        return TrendStrategy()
    if name == "LATENCY":
        return LatencyStrategy()
    if name == "WARPS":
        return WARPSStrategy()
    raise ValueError(f"Unknown strategy: {name}")


# ── Batch runner ──────────────────────────────────────────────────────────────

class BatchRunner:
    def __init__(
        self,
        strategies: Optional[list[str]] = None,
        workloads: Optional[list[str]] = None,
        repetitions: int = 3,
        seed_base: int = 42,
        results_dir: Path = Path("results/raw"),
        threads_per_replica: int = 2,
    ):
        self.strategies = strategies or ["STATIC", "CPU", "TREND", "LATENCY", "WARPS"]
        self.workloads  = workloads  or list(WORKLOAD_REGISTRY.keys())
        self.repetitions = repetitions
        self.seed_base   = seed_base
        self.results_dir = results_dir
        self.threads_per_replica = threads_per_replica

    def run(self) -> list[ExperimentResult]:
        total = len(self.strategies) * len(self.workloads) * self.repetitions
        print(f"\n{'='*70}")
        print(f"  WARPS Autoscaling Experiment — {total} runs")
        print(f"  Strategies : {self.strategies}")
        print(f"  Workloads  : {self.workloads}")
        print(f"  Reps/cell  : {self.repetitions}")
        print(f"  Warmup/Meas: {int(WARMUP_S)}s / {int(MEASUREMENT_S)}s")
        est_min = total * (WARMUP_S + MEASUREMENT_S + 3) / 60
        print(f"  Est. time  : {est_min:.0f} min")
        print(f"{'='*70}\n")

        try:
            start_service()
        except Exception as e:
            print(f"[ERROR] Could not start target service: {e}")
            raise

        all_results: list[ExperimentResult] = []
        run_n = 0
        t_start = time.monotonic()

        try:
            for strat_name in self.strategies:
                for wl_name in self.workloads:
                    WlClass = WORKLOAD_REGISTRY[wl_name]
                    for rep in range(1, self.repetitions + 1):
                        run_n += 1
                        seed = self.seed_base + rep * 1000 + hash(wl_name) % 999
                        workload = WlClass(seed=seed)
                        strategy = _make_strategy(strat_name)

                        runner = ExperimentRunner(
                            strategy=strategy,
                            workload=workload,
                            threads_per_replica=self.threads_per_replica,
                            results_dir=self.results_dir,
                        )

                        t_run = time.monotonic()
                        print(
                            f"[{run_n:>3}/{total}] "
                            f"Strategy={strat_name:<8}  Workload={wl_name:<16}  Rep={rep}  seed={seed}",
                            end="  … ", flush=True,
                        )
                        try:
                            result = runner.run(repetition=rep)
                            elapsed = time.monotonic() - t_run
                            print(
                                f"p95={result.p95_latency_ms:>6.1f}ms  "
                                f"SLA_viol={result.sla_violation_rate:.2%}  "
                                f"replicas={result.mean_replicas:.1f}  "
                                f"scaleUP={result.n_scale_up}  "
                                f"scaleDN={result.n_scale_down}  "
                                f"[{elapsed:.0f}s]"
                            )
                            all_results.append(result)
                        except Exception as exc:  # noqa: BLE001
                            print(f"FAILED: {exc}")

                        # Remaining time estimate
                        done = run_n
                        remaining = total - done
                        avg_s = (time.monotonic() - t_start) / done
                        eta_min = (remaining * avg_s) / 60
                        if remaining > 0:
                            print(f"         ↳ ETA ~{eta_min:.0f} min remaining\n")

        finally:
            stop_service()

        print(f"\n{'='*70}")
        print(f"  Completed {len(all_results)}/{total} runs in {(time.monotonic()-t_start)/60:.1f} min")
        print(f"{'='*70}\n")
        return all_results
