"""Plot a live MPC recording (JSONL rows from /api/state + /api/sim, ~2 s cadence).

    .venv/bin/python -m floor_simulator.tools.plot_live_mpc <rows.jsonl> <out.png> [setpoint]
"""
from __future__ import annotations

import json
import sys

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from floor_simulator.tools.plot_controllers import GRID, INK, INK2, SERIES, style  # noqa: E402


def main(src: str, out: str, setpoint: float = 35.0):
    allrows = [json.loads(line) for line in open(src)]
    rows = [r for r in allrows if "fwd" in r]
    events = [r for r in allrows if "event" in r]
    m = [r["t"] / 60 for r in rows]
    fig, (a1, a2, a3) = plt.subplots(3, 1, figsize=(12, 8.5), sharex=True,
                                     gridspec_kw=dict(height_ratios=[1.6, 1.0, 0.7]))
    fig.patch.set_facecolor("white")
    a1.axhspan(setpoint - 1, setpoint + 1, color=SERIES[0], alpha=0.08, lw=0)
    a1.axhline(setpoint, color=INK2, lw=1, ls="--", label=f"Setpoint {setpoint:.0f} °C")
    a1.plot(m, [r["fwd"] for r in rows], color=SERIES[0], lw=1.6, label="T forward (controller reading)")
    # predicted trajectories at a few plan instants
    shown = 0
    for i, r in enumerate(rows):
        if i % 60 == 30 and r["mode"] == "mpc" and any(r["traj"]):
            dt = 900 / 16 / 60
            xs = [m[i] + dt * (k + 1) for k in range(16)]
            a1.plot(xs, r["traj"], color=SERIES[0], lw=1, ls=":", alpha=0.8,
                    label="MPC prediction (sampled)" if shown == 0 else None)
            shown += 1
    a1.set_ylabel("°C", color=INK2, fontsize=9)
    a1.legend(loc="lower right", fontsize=8, frameon=False)
    a1.set_title("Forward temperature and the controller's own predictions", loc="left", fontsize=10, color=INK)
    a2.plot(m, [r["phys"] for r in rows], color=SERIES[3], lw=1.6, label="Valve (emulated carrier)")
    a2.plot(m, [r["tgt"] for r in rows], color=SERIES[1], lw=1.2, ls="--", label="Target")
    a2.set_ylim(0, 100)
    a2.set_ylabel("valve %", color=INK2, fontsize=9)
    a2.legend(loc="upper right", fontsize=8, frameon=False, ncol=2)
    a2.set_title("Valve", loc="left", fontsize=10, color=INK)
    a3.plot(m, [r["boil"] for r in rows], color=SERIES[1], lw=1.4, label="T incoming (boiler role)")
    a3.plot(m, [r["ret"] for r in rows], color=SERIES[2], lw=1.4, label="T return")
    a3.set_ylabel("°C", color=INK2, fontsize=9)
    a3.legend(loc="upper right", fontsize=8, frameon=False, ncol=2)
    a3.set_xlabel("minutes", color=INK2, fontsize=9)
    for a in (a1, a2, a3):
        style(a)
        for e in events:
            a.axvline(e["t"] / 60, color=INK2, lw=0.8, ls="-.")
        # fallback periods (homing / cautious) shaded
        start = None
        for r in rows + [dict(t=rows[-1]["t"], mode="mpc")]:
            fb = r["mode"] in ("homing", "cautious")
            if fb and start is None:
                start = r["t"]
            elif not fb and start is not None:
                a.axvspan(start / 60, r["t"] / 60, color=SERIES[1], alpha=0.08, lw=0)
                start = None
    for i, e in enumerate(events):
        a1.annotate(e["event"], (e["t"] / 60, a1.get_ylim()[1]), fontsize=8, color=INK2,
                    xytext=(3, -12 - 11 * i), textcoords="offset points")
    last = rows[-1]
    travel = sum(abs(b["phys"] - a["phys"]) for a, b in zip(rows, rows[1:]))
    hours = max((rows[-1]["t"] - rows[0]["t"]) / 3600, 1e-6)
    fig.suptitle(f"Live HIL, firmware MPC — {travel:.0f} % travel ({travel / hours:.0f} %/h), "
                 f"{last['plans']} plans ({last['triggered'] if 'triggered' in last else last['trig']} triggered), "
                 f"plan compute ≤{max(r['cpu'] for r in rows):.0f} ms, end mode {last['mode']}",
                 x=0.01, ha="left", fontsize=11, color=INK, fontweight="bold")
    fig.tight_layout()
    fig.savefig(out, dpi=110)
    print(out)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2], float(sys.argv[3]) if len(sys.argv) > 3 else 35.0)
