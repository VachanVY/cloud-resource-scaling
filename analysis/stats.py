"""
Statistical analysis of experiment results.

Loads all JSON result files from results/raw/, computes per-cell
(strategy × workload) summary statistics with 95% confidence intervals,
and runs Mann-Whitney U tests comparing WARPS against each baseline.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from scipy import stats as sp_stats


# ── Loading ───────────────────────────────────────────────────────────────────

def load_results(results_dir: str | Path = "results/raw") -> pd.DataFrame:
    """Load all JSON result files into a flat DataFrame."""
    p = Path(results_dir)
    rows = []
    for f in sorted(p.glob("*.json")):
        try:
            with open(f) as fh:
                d = json.load(fh)
            # Flatten — exclude heavy timeline list
            row = {k: v for k, v in d.items() if k != "timeline"}
            rows.append(row)
        except Exception as e:  # noqa: BLE001
            print(f"[warn] Could not load {f}: {e}")
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows)


# ── Summary statistics ────────────────────────────────────────────────────────

def _ci95(values: np.ndarray) -> float:
    """Half-width of 95% CI (t-distribution)."""
    n = len(values)
    if n < 2:
        return 0.0
    return float(sp_stats.t.ppf(0.975, df=n - 1) * values.std(ddof=1) / np.sqrt(n))


METRICS = [
    "p95_latency_ms",
    "mean_latency_ms",
    "p99_latency_ms",
    "sla_violation_rate",
    "mean_replicas",
    "replica_hours",
    "cost_usd",
    "n_scale_up",
    "n_scale_down",
    "n_oscillations",
    "mean_utilization_pct",
]


def compute_summary(df: pd.DataFrame) -> pd.DataFrame:
    """Return per-(strategy, workload) mean ± CI for all metrics."""
    if df.empty:
        return pd.DataFrame()

    rows = []
    for (strategy, workload), grp in df.groupby(["strategy_name", "workload_name"]):
        row: dict = {"strategy": strategy, "workload": workload, "n_reps": len(grp)}
        for m in METRICS:
            if m not in grp.columns:
                continue
            vals = grp[m].dropna().values.astype(float)
            if len(vals) == 0:
                row[f"{m}_mean"] = np.nan
                row[f"{m}_ci95"] = np.nan
                continue
            row[f"{m}_mean"] = float(vals.mean())
            row[f"{m}_ci95"] = _ci95(vals)
        rows.append(row)
    return pd.DataFrame(rows)


# ── Statistical tests ─────────────────────────────────────────────────────────

def run_statistical_tests(
    df: pd.DataFrame,
    proposed: str = "WARPS",
    baselines: Optional[list[str]] = None,
    metric: str = "p95_latency_ms",
    alpha: float = 0.05,
) -> pd.DataFrame:
    """
    Mann-Whitney U test: WARPS vs each baseline, per workload.
    Returns DataFrame with columns: workload, baseline, U, p_value, effect_size (r),
    significant, direction (WARPS better / worse / tie).
    """
    if df.empty or proposed not in df["strategy_name"].values:
        return pd.DataFrame()

    if baselines is None:
        baselines = [s for s in df["strategy_name"].unique() if s != proposed]

    rows = []
    for workload in df["workload_name"].unique():
        proposed_vals = df[
            (df["strategy_name"] == proposed) & (df["workload_name"] == workload)
        ][metric].dropna().values.astype(float)

        for bl in baselines:
            bl_vals = df[
                (df["strategy_name"] == bl) & (df["workload_name"] == workload)
            ][metric].dropna().values.astype(float)

            if len(proposed_vals) < 2 or len(bl_vals) < 2:
                continue

            stat, pval = sp_stats.mannwhitneyu(
                proposed_vals, bl_vals, alternative="two-sided"
            )
            n1, n2 = len(proposed_vals), len(bl_vals)
            # Effect size r = Z / sqrt(N)
            z = (stat - n1 * n2 / 2.0) / np.sqrt(n1 * n2 * (n1 + n2 + 1) / 12.0)
            r = abs(z) / np.sqrt(n1 + n2)

            sig = pval < alpha
            if sig:
                direction = "WARPS_BETTER" if proposed_vals.mean() < bl_vals.mean() else "WARPS_WORSE"
            else:
                direction = "NO_DIFF"

            rows.append({
                "workload":     workload,
                "baseline":     bl,
                "metric":       metric,
                "warps_mean":   round(float(proposed_vals.mean()), 2),
                "baseline_mean": round(float(bl_vals.mean()), 2),
                "U":            float(stat),
                "p_value":      round(float(pval), 4),
                "effect_r":     round(float(r), 3),
                "significant":  sig,
                "direction":    direction,
            })
    return pd.DataFrame(rows)


# ── Composite score ───────────────────────────────────────────────────────────

def composite_score(
    summary: pd.DataFrame,
    w_sla: float = 0.40,
    w_cost: float = 0.30,
    w_osc: float = 0.30,
) -> pd.DataFrame:
    """
    J = w_sla × SLA_violation_rate + w_cost × replica_hours + w_osc × n_oscillations

    All metrics are min-max normalised within the workload group so they
    contribute on the same scale.
    """
    if summary.empty:
        return pd.DataFrame()

    rows = []
    for workload, grp in summary.groupby("workload"):
        for col, weight in [
            ("sla_violation_rate_mean", w_sla),
            ("replica_hours_mean",      w_cost),
            ("n_oscillations_mean",     w_osc),
        ]:
            if col not in grp.columns:
                grp = grp.copy()
                grp[col] = 0.0
            mn, mx = grp[col].min(), grp[col].max()
            rng = mx - mn if mx != mn else 1.0
            grp[f"_norm_{col}"] = (grp[col] - mn) / rng

        grp = grp.copy()
        grp["composite_J"] = (
            w_sla  * grp["_norm_sla_violation_rate_mean"]
            + w_cost * grp["_norm_replica_hours_mean"]
            + w_osc  * grp["_norm_n_oscillations_mean"]
        )
        rows.append(grp[["strategy", "workload", "composite_J"]])

    return pd.concat(rows, ignore_index=True)


# ── LaTeX table helper ────────────────────────────────────────────────────────

def to_latex_table(summary: pd.DataFrame, metric: str = "p95_latency_ms") -> str:
    """
    Produce a LaTeX table of mean ± CI for the chosen metric,
    with strategies as rows and workloads as columns.
    """
    mean_col = f"{metric}_mean"
    ci_col   = f"{metric}_ci95"
    if mean_col not in summary.columns:
        return "% No data"

    pivot_mean = summary.pivot(index="strategy", columns="workload", values=mean_col)
    pivot_ci   = summary.pivot(index="strategy", columns="workload", values=ci_col)

    wl_order = sorted(pivot_mean.columns)
    pivot_mean = pivot_mean[wl_order]
    pivot_ci   = pivot_ci[wl_order]

    lines = [
        r"\begin{tabular}{l" + "c" * len(wl_order) + r"}",
        r"\toprule",
        "Strategy & " + " & ".join(c.replace("_", r"\_") for c in wl_order) + r" \\",
        r"\midrule",
    ]
    for strat in pivot_mean.index:
        cells = []
        for wl in wl_order:
            m = pivot_mean.loc[strat, wl]
            c = pivot_ci.loc[strat, wl]
            cells.append(f"${m:.1f} \\pm {c:.1f}$" if not np.isnan(m) else "--")
        lines.append(strat + " & " + " & ".join(cells) + r" \\")
    lines += [r"\bottomrule", r"\end{tabular}"]
    return "\n".join(lines)
