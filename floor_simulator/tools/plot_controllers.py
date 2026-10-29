"""Plot controller comparisons from the offline closed loop.

    .venv/bin/python -m floor_simulator.tools.plot_controllers --out <dir>   (from repo root)
"""
from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from floor_simulator.tools.mixer_tuning_sim import run  # noqa: E402

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#6250d6"]
INK, INK2, GRID = "#0b0b0b", "#52514e", "#e8e7e3"

CONTROLLERS = [
    ("Current firmware (delay_s 120)", {}),
    ("Current firmware, delay_s 30", dict(delay_s=30.0)),
    ("MPC prototype (firmware change)", dict(ctrl="mpc", mpc=dict(lam=0.1, start_cost=1.0))),
]


def style(ax):
    ax.grid(True, color=GRID, lw=0.8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(INK2)
    ax.tick_params(colors=INK2, labelsize=8)


def scenario(out: Path, name: str, setpoint: float, preset: str, tout: float, hours: float, seed: int):
    traces = []
    for label, cfg in CONTROLLERS:
        tr = []
        m = run(dict(cfg, sensor_res=0.0625), hours=hours, seed=seed, setpoint=setpoint, preset=preset,
                tout=tout, trace=tr)
        traces.append((label, tr, m))
    n = len(traces)
    fig, axes = plt.subplots(2 * n + 1, 1, figsize=(12, 2.0 * (2 * n + 1)), sharex=True,
                             gridspec_kw=dict(height_ratios=[1.2] + [1.6, 1.0] * n))
    fig.patch.set_facecolor("white")
    t0 = traces[0][1]
    h = [r[0] / 3600 for r in t0]
    ax = axes[0]
    ax.plot(h, [r[3] for r in t0], color=SERIES[1], lw=1.4, label="T incoming (boiler-role sensor)")
    ax.plot(h, [r[4] for r in t0], color=SERIES[2], lw=1.4, label="T return")
    ax.set_title("Supply conditions (identical boiler seed for every controller)", loc="left", fontsize=10, color=INK)
    ax.set_ylabel("°C", color=INK2, fontsize=9)
    ax.legend(loc="upper right", fontsize=8, frameon=False, ncol=2)
    style(ax)
    for i, (label, tr, m) in enumerate(traces):
        hh = [r[0] / 3600 for r in tr]
        a1, a2 = axes[1 + 2 * i], axes[2 + 2 * i]
        a1.axhspan(setpoint - 1, setpoint + 1, color=SERIES[0], alpha=0.08, lw=0)
        a1.axhline(setpoint, color=INK2, lw=1, ls="--", label=f"Setpoint {setpoint:.0f} °C")
        a1.plot(hh, [r[1] for r in tr], color=SERIES[0], lw=1.6, label="T forward (sensor)")
        a1.set_ylim(setpoint - 8, setpoint + 10)
        a1.set_ylabel("°C", color=INK2, fontsize=9)
        a1.set_title(f"{label} — travel {m['travel_pct_h']:.0f} %/h · {m['starts_h']:.0f} motor starts/h · "
                     f"{m['reversals_h']:.0f} reversals/h · RMSE {m['rmse_ok']:.2f} K · max {m['fwd_max']:.1f} °C",
                     loc="left", fontsize=10, color=INK)
        a1.legend(loc="upper right", fontsize=8, frameon=False, ncol=2)
        a2.plot(hh, [r[2] for r in tr], color=SERIES[3], lw=1.4, label="Valve position")
        a2.set_ylim(0, 100)
        a2.set_ylabel("valve %", color=INK2, fontsize=9)
        style(a1)
        style(a2)
    axes[-1].set_xlabel("hours", color=INK2, fontsize=9)
    fig.suptitle(name, x=0.01, ha="left", fontsize=12, color=INK, fontweight="bold")
    fig.tight_layout()
    path = out / f"controllers_{preset}_sp{setpoint:.0f}.png"
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return path


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--hours", type=float, default=4.0)
    ap.add_argument("--seed", type=int, default=1)
    a = ap.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    for args in [("Setpoint 35 °C, steady winter, outdoor 2 °C", 35.0, "steady_winter", 2.0),
                 ("Setpoint 40 °C, steady winter, outdoor −5 °C", 40.0, "steady_winter", -5.0)]:
        print(scenario(a.out, *args, hours=a.hours, seed=a.seed))
