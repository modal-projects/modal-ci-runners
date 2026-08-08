"""Render README performance charts (matplotlib → docs/).

Covers the metrics CI runner users care about:

- time to ready (vs GitHub-hosted, cold vs warm)
- where warm queue time goes
- concurrency / burst
- idle cost shape

Run from repo root:

  uv run --group dev python scripts/charts.py
"""

from __future__ import annotations

from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs"


def style() -> None:
    plt.rcParams.update(
        {
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "axes.edgecolor": "#333333",
            "axes.labelcolor": "#222222",
            "text.color": "#222222",
            "xtick.color": "#333333",
            "ytick.color": "#333333",
            "font.size": 11,
            "axes.titlesize": 13,
            "axes.titleweight": "bold",
            "figure.dpi": 160,
        }
    )


def chart_time_to_ready() -> Path:
    """Queue / time-to-ready + cold vs warm + vs GitHub-hosted."""
    labels = ["GitHub-hosted", "runner-modal\n(warm)", "runner-modal\n(cold image)"]
    seconds = [3, 4, 30]
    colors = ["#2a6f4e", "#3b6ea5", "#a33b3b"]

    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    bars = ax.bar(labels, seconds, color=colors, width=0.62)
    ax.set_ylabel("Seconds to runner ready")
    ax.set_title("Time to runner ready")
    ax.set_ylim(0, 36)
    for bar, value in zip(bars, seconds, strict=True):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            value + 0.6,
            f"~{value}s",
            ha="center",
            va="bottom",
            fontsize=10,
        )
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    path = OUT / "time-to-ready.png"
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def chart_queue_budget() -> Path:
    """Breakdown of warm self-hosted queue (time to online)."""
    # Shape after Named Image + in-job JIT mint (not an SLA).
    stages = ["Admit / claim", "Sandbox create", "JIT mint (in job)", "Runner online"]
    shares = [5, 10, 15, 70]
    colors = ["#8a8a8a", "#5b8fc7", "#3b6ea5", "#c47a2c"]
    y = list(range(len(stages)))[::-1]

    fig, ax = plt.subplots(figsize=(7.2, 3.6))
    bars = ax.barh(y, shares, color=colors, height=0.65)
    ax.set_yticks(y)
    ax.set_yticklabels(stages)
    ax.set_xlim(0, 100)
    ax.set_xlabel("Share of warm time-to-online (%)")
    ax.set_title("Warm queue budget (self-hosted Sandbox runner)")
    for bar, share in zip(bars, shares, strict=True):
        ax.text(
            share + 1.5,
            bar.get_y() + bar.get_height() / 2,
            f"{share}%",
            ha="left",
            va="center",
            fontsize=10,
        )
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    path = OUT / "queue-budget.png"
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def chart_concurrency() -> Path:
    """Can N jobs start together, or do they wait?"""
    n_jobs = np.array([1, 5, 10, 20])
    # Illustrative: with headroom, each job starts ~independently (~4s).
    # With a hard pool of 1, later jobs wait in series.
    parallel_ready = np.full_like(n_jobs, 4, dtype=float)
    serial_ready = 4 * n_jobs

    fig, ax = plt.subplots(figsize=(7.2, 3.8))
    x = np.arange(len(n_jobs))
    width = 0.36
    ax.bar(
        x - width / 2,
        parallel_ready,
        width,
        label="runner-modal (headroom under max_concurrent)",
        color="#3b6ea5",
    )
    ax.bar(
        x + width / 2,
        serial_ready,
        width,
        label="Serialized (effective concurrency = 1)",
        color="#a33b3b",
    )
    ax.set_xticks(x)
    ax.set_xticklabels([str(n) for n in n_jobs])
    ax.set_xlabel("Jobs queued together")
    ax.set_ylabel("Seconds until last runner is ready")
    ax.set_title("Concurrency / burst")
    ax.set_ylim(0, 90)
    ax.legend(frameon=False, fontsize=9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    fig.tight_layout()
    path = OUT / "concurrency.png"
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def chart_idle_cost() -> Path:
    """What you pay when nothing is running vs while jobs run."""
    categories = ["Idle\n(no jobs)", "One job\nrunning", "N jobs\nrunning"]
    # Relative cost units — shape, not dollars.
    github = [0, 1, 1]  # hosted: no idle bill; jobs billed by GH minutes
    modal = [1, 2, 2 + 3]  # min_containers + per-Sandbox jobs (N=3 illustrative)

    fig, ax = plt.subplots(figsize=(7.2, 3.8))
    x = np.arange(len(categories))
    width = 0.36
    ax.bar(x - width / 2, github, width, label="GitHub-hosted", color="#2a6f4e")
    ax.bar(x + width / 2, modal, width, label="runner-modal", color="#3b6ea5")
    ax.set_xticks(x)
    ax.set_xticklabels(categories)
    ax.set_ylabel("Relative cost (illustrative units)")
    ax.set_title("Idle vs active cost shape")
    ax.set_ylim(0, 7)
    ax.legend(frameon=False, fontsize=9)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.text(
        0.02,
        0.95,
        "Modal idle ≈ control plane (min_containers).\n"
        "Each job ≈ one Sandbox; goes away when the run ends.",
        transform=ax.transAxes,
        va="top",
        fontsize=9,
        color="#444444",
    )
    fig.tight_layout()
    path = OUT / "idle-cost.png"
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)
    return path


def main() -> None:
    style()
    OUT.mkdir(parents=True, exist_ok=True)
    for path in (
        chart_time_to_ready(),
        chart_queue_budget(),
        chart_concurrency(),
        chart_idle_cost(),
    ):
        print(path.relative_to(ROOT))


if __name__ == "__main__":
    main()
