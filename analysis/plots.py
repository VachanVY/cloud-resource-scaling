"""
Experiment visualisation.

Generates publication-quality figures saved to ``figures/``.
All figures use a colour-blind-friendly palette.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import matplotlib
matplotlib.use("Agg")   # headless
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd
import seaborn as sns

# ── Style ─────────────────────────────────────────────────────────────────────

PALETTE = {
    "STATIC":  "#999999",
    "CPU":     "#4477AA",
    "TREND":   "#EE6677",
    "LATENCY": "#228833",
    "WARPS":   "#CC3311",
}
STRATEGY_ORDER = ["STATIC", "CPU", "TREND", "LATENCY", "WARPS"]
sns.set_theme(style="whitegrid", font_scale=1.1)


def _fig_path(figures_dir: Path, name: str) -> Path:
    figures_dir.mkdir(parents=True, exist_ok=True)
    return figures_dir / name


def _strategy_color(s: str) -> str:
    return PALETTE.get(s, "#888888")


# ── Figure 1: Box-plots — p95 latency per strategy × workload ─────────────────

def fig_latency_boxplot(df: pd.DataFrame, figures_dir: Path) -> Path:
    """Grid of box-plots: rows=workloads, cols not split (one panel per workload)."""
    workloads = sorted(df["workload_name"].unique())
    n_wl = len(workloads)
    fig, axes = plt.subplots(1, n_wl, figsize=(4 * n_wl, 5), sharey=False)
    if n_wl == 1:
        axes = [axes]

    for ax, wl in zip(axes, workloads):
        sub = df[df["workload_name"] == wl]
        order = [s for s in STRATEGY_ORDER if s in sub["strategy_name"].unique()]
        colors = [_strategy_color(s) for s in order]
        bp = ax.boxplot(
            [sub[sub["strategy_name"] == s]["p95_latency_ms"].dropna().values for s in order],
            labels=order, patch_artist=True, notch=False,
            medianprops=dict(color="black", linewidth=2),
        )
        for patch, col in zip(bp["boxes"], colors):
            patch.set_facecolor(col)
            patch.set_alpha(0.75)
        ax.axhline(200, color="red", linestyle="--", linewidth=1.0, label="SLA 200 ms")
        ax.set_title(wl.replace("_", " "), fontsize=10)
        ax.set_ylabel("p95 Latency (ms)")
        ax.tick_params(axis="x", rotation=30)
    fig.suptitle("p95 Latency Distribution by Strategy and Workload", fontsize=12, y=1.01)
    fig.tight_layout()
    path = _fig_path(figures_dir, "fig1_latency_boxplot.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


# ── Figure 2: Bar chart — SLA violation rates ────────────────────────────────

def fig_sla_violations(df: pd.DataFrame, figures_dir: Path) -> Path:
    workloads = sorted(df["workload_name"].unique())
    strategies = [s for s in STRATEGY_ORDER if s in df["strategy_name"].unique()]

    summary = df.groupby(["strategy_name", "workload_name"])["sla_violation_rate"].mean().reset_index()

    n_wl = len(workloads)
    fig, axes = plt.subplots(1, n_wl, figsize=(4.2 * n_wl, 4.5), sharey=True)
    if n_wl == 1:
        axes = [axes]

    for ax, wl in zip(axes, workloads):
        sub = summary[summary["workload_name"] == wl]
        vals   = [sub[sub["strategy_name"] == s]["sla_violation_rate"].values[0]
                  if s in sub["strategy_name"].values else 0.0
                  for s in strategies]
        colors = [_strategy_color(s) for s in strategies]
        bars = ax.bar(strategies, [v * 100 for v in vals], color=colors, alpha=0.85)
        ax.set_title(wl.replace("_", " "), fontsize=10)
        ax.set_ylabel("SLA Violation Rate (%)")
        ax.tick_params(axis="x", rotation=30)
        ax.yaxis.set_major_formatter(mticker.FormatStrFormatter("%.1f%%"))
        for bar, val in zip(bars, vals):
            if val > 0:
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                        f"{val*100:.1f}%", ha="center", va="bottom", fontsize=8)

    fig.suptitle("SLA Violation Rate (p95 > 200 ms)", fontsize=12, y=1.02)
    fig.tight_layout()
    path = _fig_path(figures_dir, "fig2_sla_violations.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


# ── Figure 3: Bar chart — replica-hours (simulated cost) ─────────────────────

def fig_cost(df: pd.DataFrame, figures_dir: Path) -> Path:
    strategies = [s for s in STRATEGY_ORDER if s in df["strategy_name"].unique()]
    summary = df.groupby(["strategy_name", "workload_name"])["replica_hours"].mean().reset_index()
    workloads = sorted(df["workload_name"].unique())

    pivot = summary.pivot(index="strategy_name", columns="workload_name", values="replica_hours").fillna(0)
    pivot = pivot.loc[[s for s in strategies if s in pivot.index]]

    x = np.arange(len(workloads))
    width = 0.8 / len(strategies)
    fig, ax = plt.subplots(figsize=(max(8, 2 * len(workloads)), 5))
    for i, strat in enumerate(strategies):
        if strat not in pivot.index:
            continue
        offsets = x + (i - len(strategies) / 2) * width + width / 2
        vals = [pivot.loc[strat, wl] if wl in pivot.columns else 0.0 for wl in workloads]
        ax.bar(offsets, vals, width=width * 0.9, label=strat,
               color=_strategy_color(strat), alpha=0.85)

    ax.set_xticks(x)
    ax.set_xticklabels([wl.replace("_", "\n") for wl in workloads], fontsize=9)
    ax.set_ylabel("Avg Replica-Hours (simulated)")
    ax.set_title("Resource Cost by Strategy and Workload")
    ax.legend(title="Strategy", fontsize=9)
    fig.tight_layout()
    path = _fig_path(figures_dir, "fig3_cost_replica_hours.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


# ── Figure 4: Scaling stability — oscillation count ──────────────────────────

def fig_oscillations(df: pd.DataFrame, figures_dir: Path) -> Path:
    strategies = [s for s in STRATEGY_ORDER if s in df["strategy_name"].unique()]
    grp = df.groupby(["strategy_name", "workload_name"])["n_oscillations"].mean().reset_index()
    workloads = sorted(df["workload_name"].unique())

    pivot = grp.pivot(index="strategy_name", columns="workload_name", values="n_oscillations").fillna(0)
    pivot = pivot.loc[[s for s in strategies if s in pivot.index]]

    fig, ax = plt.subplots(figsize=(max(8, 1.8 * len(workloads)), 5))
    x = np.arange(len(workloads))
    width = 0.8 / len(strategies)
    for i, strat in enumerate(strategies):
        if strat not in pivot.index:
            continue
        offsets = x + (i - len(strategies) / 2) * width + width / 2
        vals = [pivot.loc[strat, wl] if wl in pivot.columns else 0.0 for wl in workloads]
        ax.bar(offsets, vals, width=width * 0.9, label=strat,
               color=_strategy_color(strat), alpha=0.85)

    ax.set_xticks(x)
    ax.set_xticklabels([wl.replace("_", "\n") for wl in workloads], fontsize=9)
    ax.set_ylabel("Avg Oscillation Events")
    ax.set_title("Scaling Oscillation Count (direction reversals)")
    ax.legend(title="Strategy", fontsize=9)
    fig.tight_layout()
    path = _fig_path(figures_dir, "fig4_oscillations.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


# ── Figure 5: Time series — representative run ──────────────────────────────

def fig_time_series(results_dir: Path, figures_dir: Path, workload: str = "W2_STEP_SPIKE") -> Path:
    """Load one representative run per strategy for a given workload and plot."""
    strategies = STRATEGY_ORDER
    timelines: dict[str, list[dict]] = {}

    for f in sorted(results_dir.glob(f"*_{workload}_rep01_*.json")):
        with open(f) as fh:
            d = json.load(fh)
        strat = d.get("strategy_name", "")
        if strat not in timelines:
            timelines[strat] = d.get("timeline", [])

    if not timelines:
        fig, ax = plt.subplots()
        ax.text(0.5, 0.5, "No data", transform=ax.transAxes, ha="center")
        path = _fig_path(figures_dir, f"fig5_timeseries_{workload}.png")
        fig.savefig(path, dpi=150)
        plt.close(fig)
        return path

    present = [s for s in strategies if s in timelines]
    fig, axes = plt.subplots(2, 1, figsize=(10, 7), sharex=True)
    ax_lat, ax_rep = axes

    for strat in present:
        tl = timelines[strat]
        t  = [s["elapsed_s"] for s in tl]
        p95 = [s["p95_latency_ms"] for s in tl]
        rep = [s["replicas"] for s in tl]
        c   = _strategy_color(strat)
        ax_lat.plot(t, p95, label=strat, color=c, linewidth=1.8)
        ax_rep.step(t, rep, label=strat, color=c, linewidth=1.8, where="post")

    ax_lat.axhline(200, color="red", linestyle="--", linewidth=1.2, label="SLA 200 ms")
    ax_lat.set_ylabel("p95 Latency (ms)")
    ax_lat.set_title(f"Time Series — {workload.replace('_', ' ')} (rep 1)")
    ax_lat.legend(fontsize=9, loc="upper left")

    ax_rep.set_xlabel("Elapsed time (s)")
    ax_rep.set_ylabel("Replica count")
    ax_rep.legend(fontsize=9, loc="upper left")

    fig.tight_layout()
    path = _fig_path(figures_dir, f"fig5_timeseries_{workload}.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


# ── Figure 6: WARPS strategy selection over time ─────────────────────────────

def fig_warps_selection(results_dir: Path, figures_dir: Path, workload: str = "W5_BURSTY") -> Path:
    """Show which sub-strategy WARPS selected at each step for a bursty workload."""
    target_file = None
    for f in sorted(results_dir.glob(f"WARPS_{workload}_rep01_*.json")):
        target_file = f
        break

    if not target_file:
        fig, ax = plt.subplots()
        ax.text(0.5, 0.5, "No WARPS data", transform=ax.transAxes, ha="center")
        path = _fig_path(figures_dir, f"fig6_warps_{workload}.png")
        fig.savefig(path, dpi=150)
        plt.close(fig)
        return path

    with open(target_file) as fh:
        d = json.load(fh)

    tl = d.get("timeline", [])
    t  = [s["elapsed_s"] for s in tl]
    lat = [s["p95_latency_ms"] for s in tl]
    sel = [s.get("warps_selected_strategy") or "CPU" for s in tl]
    rps = [s.get("target_rps", 0) for s in tl]

    sub_colors = {"CPU": "#4477AA", "TREND": "#EE6677", "LATENCY": "#228833"}

    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(10, 8), sharex=True)

    ax1.plot(t, rps, color="#555555", linewidth=1.5)
    ax1.set_ylabel("Target RPS")
    ax1.set_title(f"WARPS Adaptive Strategy Selection — {workload.replace('_', ' ')} (rep 1)")

    ax2.plot(t, lat, color="steelblue", linewidth=1.5)
    ax2.axhline(200, color="red", linestyle="--", linewidth=1.0, label="SLA")
    ax2.set_ylabel("p95 Latency (ms)")
    ax2.legend(fontsize=8)

    # Color bands for selected strategy
    for i, (ti, s) in enumerate(zip(t, sel)):
        next_t = t[i + 1] if i + 1 < len(t) else ti + 5
        ax3.axvspan(ti, next_t, alpha=0.55,
                    color=sub_colors.get(s, "#AAAAAA"), label=None)

    # Legend
    from matplotlib.patches import Patch
    handles = [Patch(facecolor=sub_colors[k], alpha=0.7, label=k) for k in sub_colors]
    ax3.legend(handles=handles, fontsize=9, loc="upper left")
    ax3.set_ylabel("Selected Strategy")
    ax3.set_xlabel("Elapsed time (s)")
    ax3.set_yticks([])

    fig.tight_layout()
    path = _fig_path(figures_dir, f"fig6_warps_{workload}.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


# ── Figure 7: Heatmap — composite score ───────────────────────────────────────

def fig_composite_heatmap(composite: pd.DataFrame, figures_dir: Path) -> Path:
    if composite.empty:
        fig, ax = plt.subplots()
        ax.text(0.5, 0.5, "No data", transform=ax.transAxes, ha="center")
        path = _fig_path(figures_dir, "fig7_composite_heatmap.png")
        fig.savefig(path, dpi=150)
        plt.close(fig)
        return path

    pivot = composite.pivot(index="strategy", columns="workload", values="composite_J")
    pivot = pivot.loc[[s for s in STRATEGY_ORDER if s in pivot.index]]

    fig, ax = plt.subplots(figsize=(max(7, len(pivot.columns) * 1.5), 4))
    sns.heatmap(
        pivot, ax=ax, annot=True, fmt=".2f", cmap="RdYlGn_r",
        cbar_kws={"label": "Composite J (lower = better)"},
        linewidths=0.5,
    )
    ax.set_title("Composite Score J = 0.4×SLA + 0.3×Cost + 0.3×Oscillation\n(normalised within workload; lower is better)")
    fig.tight_layout()
    path = _fig_path(figures_dir, "fig7_composite_heatmap.png")
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return path


# ── Master function ───────────────────────────────────────────────────────────

def generate_all_figures(
    df: pd.DataFrame,
    summary: pd.DataFrame,
    composite: pd.DataFrame,
    results_dir: Path = Path("results/raw"),
    figures_dir: Path = Path("figures"),
) -> list[Path]:
    paths = []
    if df.empty:
        print("[plots] No data — skipping figures.")
        return paths

    try: paths.append(fig_latency_boxplot(df, figures_dir))
    except Exception as e: print(f"[plots] fig1 failed: {e}")

    try: paths.append(fig_sla_violations(df, figures_dir))
    except Exception as e: print(f"[plots] fig2 failed: {e}")

    try: paths.append(fig_cost(df, figures_dir))
    except Exception as e: print(f"[plots] fig3 failed: {e}")

    try: paths.append(fig_oscillations(df, figures_dir))
    except Exception as e: print(f"[plots] fig4 failed: {e}")

    for wl in ["W2_STEP_SPIKE", "W5_BURSTY", "W3_RAMP"]:
        try: paths.append(fig_time_series(results_dir, figures_dir, workload=wl))
        except Exception as e: print(f"[plots] fig5/{wl} failed: {e}")

    try: paths.append(fig_warps_selection(results_dir, figures_dir, workload="W5_BURSTY"))
    except Exception as e: print(f"[plots] fig6 failed: {e}")

    try: paths.append(fig_composite_heatmap(composite, figures_dir))
    except Exception as e: print(f"[plots] fig7 failed: {e}")

    print(f"[plots] Generated {len(paths)} figures in {figures_dir}/")
    for p in paths:
        print(f"  {p}")
    return paths
