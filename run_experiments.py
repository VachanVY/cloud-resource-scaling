#!/usr/bin/env python3
"""
WARPS Autoscaling Platform — Experiment Runner

Usage
-----
  # Quick smoke-test (1 rep × 3 workloads × 3 strategies, ~15 min):
  python run_experiments.py --mode quick

  # Full experiment matrix (3 reps × 5 workloads × 5 strategies, ~75 min):
  python run_experiments.py --mode full

  # Custom:
  python run_experiments.py --reps 5 --strategies CPU LATENCY WARPS --workloads W2_STEP_SPIKE W5_BURSTY

Environment variables (override defaults):
  WARMUP_S=15          MEASUREMENT_S=60
  SCALING_INTERVAL_S=5 COOLDOWN_UP_S=15  COOLDOWN_DOWN_S=30
  SLA_THRESHOLD_MS=200 SERVICE_PORT=8100
  THREADS_PER_REPLICA=2
"""

import os
import sys
from pathlib import Path

import click

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from autoscaler.runner.batch_runner import BatchRunner


QUICK_STRATEGIES = ["STATIC", "CPU", "WARPS"]
QUICK_WORKLOADS  = ["W2_STEP_SPIKE", "W3_RAMP", "W5_BURSTY"]
FULL_STRATEGIES  = ["STATIC", "CPU", "TREND", "LATENCY", "WARPS"]
FULL_WORKLOADS   = ["W1_CONSTANT", "W2_STEP_SPIKE", "W3_RAMP", "W4_PERIODIC", "W5_BURSTY"]


@click.command()
@click.option("--mode", type=click.Choice(["quick", "full", "custom"]), default="quick",
              show_default=True, help="Experiment matrix size.")
@click.option("--reps", default=3, show_default=True, help="Repetitions per cell.")
@click.option("--strategies", multiple=True, default=None, help="Override strategy list.")
@click.option("--workloads",  multiple=True, default=None, help="Override workload list.")
@click.option("--results-dir", default="results/raw", show_default=True,
              help="Directory for raw JSON results.")
@click.option("--seed-base", default=42, show_default=True, help="Base random seed.")
def main(mode, reps, strategies, workloads, results_dir, seed_base):
    if mode == "quick":
        strats = list(strategies) or QUICK_STRATEGIES
        wls    = list(workloads)  or QUICK_WORKLOADS
        reps   = reps or 2
    elif mode == "full":
        strats = list(strategies) or FULL_STRATEGIES
        wls    = list(workloads)  or FULL_WORKLOADS
    else:   # custom
        strats = list(strategies) or QUICK_STRATEGIES
        wls    = list(workloads)  or QUICK_WORKLOADS

    threads_per_replica = int(os.environ.get("THREADS_PER_REPLICA", "2"))

    runner = BatchRunner(
        strategies=strats,
        workloads=wls,
        repetitions=reps,
        seed_base=seed_base,
        results_dir=Path(results_dir),
        threads_per_replica=threads_per_replica,
    )
    runner.run()
    print(f"\nResults saved to: {results_dir}/")
    print("Run  python analyze_results.py  to generate figures and tables.\n")


if __name__ == "__main__":
    main()
