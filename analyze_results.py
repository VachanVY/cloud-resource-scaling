#!/usr/bin/env python3
"""
WARPS Autoscaling Platform — Results Analysis

Usage:
  python analyze_results.py
  python analyze_results.py --results-dir results/raw --figures-dir figures
"""

import sys
from pathlib import Path

import click
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from analysis.stats import load_results, compute_summary, run_statistical_tests, composite_score, to_latex_table
from analysis.plots import generate_all_figures


@click.command()
@click.option("--results-dir", default="results/raw", show_default=True)
@click.option("--figures-dir", default="figures",     show_default=True)
@click.option("--latex/--no-latex", default=True,     show_default=True)
def main(results_dir, figures_dir, latex):
    rd = Path(results_dir)
    fd = Path(figures_dir)

    print(f"\nLoading results from {rd} …")
    df = load_results(rd)
    if df.empty:
        print("No results found.  Run  python run_experiments.py  first.")
        return

    print(f"Loaded {len(df)} runs  ×  "
          f"{df['strategy_name'].nunique()} strategies  ×  "
          f"{df['workload_name'].nunique()} workloads\n")

    # ── Summary statistics ────────────────────────────────────────────────────
    summary = compute_summary(df)
    csv_path = Path("results") / "summary.csv"
    csv_path.parent.mkdir(exist_ok=True)
    summary.to_csv(csv_path, index=False)
    print(f"Summary CSV  → {csv_path}")

    # ── Console table ─────────────────────────────────────────────────────────
    print("\n── p95 Latency (ms): mean ± 95% CI ─────────────────────────────")
    try:
        pivot = summary.pivot(index="strategy", columns="workload", values="p95_latency_ms_mean")
        strat_order = ["STATIC", "CPU", "TREND", "LATENCY", "WARPS"]
        pivot = pivot.loc[[s for s in strat_order if s in pivot.index]]
        print(pivot.round(1).to_string())
    except Exception as e:
        print(f"  (table error: {e})")

    print("\n── SLA Violation Rate (%) ───────────────────────────────────────")
    try:
        pivot2 = summary.pivot(index="strategy", columns="workload", values="sla_violation_rate_mean")
        pivot2 = pivot2.loc[[s for s in strat_order if s in pivot2.index]]
        print((pivot2 * 100).round(2).to_string())
    except Exception as e:
        print(f"  (table error: {e})")

    print("\n── Mean Replicas ─────────────────────────────────────────────────")
    try:
        pivot3 = summary.pivot(index="strategy", columns="workload", values="mean_replicas_mean")
        pivot3 = pivot3.loc[[s for s in strat_order if s in pivot3.index]]
        print(pivot3.round(2).to_string())
    except Exception as e:
        print(f"  (table error: {e})")

    print("\n── Oscillation Events ────────────────────────────────────────────")
    try:
        pivot4 = summary.pivot(index="strategy", columns="workload", values="n_oscillations_mean")
        pivot4 = pivot4.loc[[s for s in strat_order if s in pivot4.index]]
        print(pivot4.round(2).to_string())
    except Exception as e:
        print(f"  (table error: {e})")

    # ── Statistical tests ─────────────────────────────────────────────────────
    tests = run_statistical_tests(df, proposed="WARPS", metric="p95_latency_ms")
    if not tests.empty:
        tests_path = Path("results") / "statistical_tests.csv"
        tests.to_csv(tests_path, index=False)
        print(f"\nStatistical tests → {tests_path}")
        sig = tests[tests["significant"]]
        print(f"  Significant results (p < 0.05): {len(sig)}/{len(tests)}")
        better = sig[sig["direction"] == "WARPS_BETTER"]
        print(f"  WARPS significantly BETTER:  {len(better)}")
        worse  = sig[sig["direction"] == "WARPS_WORSE"]
        print(f"  WARPS significantly WORSE:   {len(worse)}")
        if not tests.empty:
            print("\n  " + tests[["workload","baseline","warps_mean","baseline_mean","p_value","direction"]].to_string(index=False))

    # ── Composite score ───────────────────────────────────────────────────────
    comp = composite_score(summary)
    if not comp.empty:
        comp_path = Path("results") / "composite_score.csv"
        comp.to_csv(comp_path, index=False)
        print(f"\nComposite score → {comp_path}")
        try:
            pivot_comp = comp.pivot(index="strategy", columns="workload", values="composite_J")
            strat_order_comp = ["STATIC","CPU","TREND","LATENCY","WARPS"]
            pivot_comp = pivot_comp.loc[[s for s in strat_order_comp if s in pivot_comp.index]]
            print(pivot_comp.round(3).to_string())
        except Exception as e:
            print(f"  (composite pivot error: {e})")

    # ── LaTeX tables ──────────────────────────────────────────────────────────
    if latex:
        paper_dir = Path("paper")
        paper_dir.mkdir(exist_ok=True)
        for metric in ["p95_latency_ms", "sla_violation_rate"]:
            tex = to_latex_table(summary, metric=metric)
            tex_path = paper_dir / f"table_{metric}.tex"
            tex_path.write_text(tex)
            print(f"LaTeX table → {tex_path}")

    # ── Figures ───────────────────────────────────────────────────────────────
    print(f"\nGenerating figures → {fd}/")
    generate_all_figures(df, summary, comp, results_dir=rd, figures_dir=fd)
    print("\nDone.\n")


if __name__ == "__main__":
    main()
