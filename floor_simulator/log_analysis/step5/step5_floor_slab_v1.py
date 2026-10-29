#!/usr/bin/env python3
"""Step 5 v1 - underfloor loop, slab and room dynamics (T_forward -> T_return, T_room).

Discrete model, DT = 10 s.  States: Tr (return water at the manifold sensor),
Ts (slab / screed node), Tq (deep slab node, two-node candidates only),
Tro (room air).

  pump on :  Tr  -> Ts + beta * (Tfwd(t - d) - Ts)                (tau_r)
             Ts  += DT * [a_in * (Tfwd(t - d) - Tr) - a_out * (Ts - Tamb)]
  pump off:  Tr  -> Ts + kappa * (Tamb - Ts)                      (tau_off)
             Ts  += DT * [- a_out * (Ts - Tamb)]
  two-node:  Ts  += DT * a_q * (Tq - Ts),  Tq += DT * a_q * r_q * (Ts - Tq)
  room    :  Tro += DT * [b_s * (Ts - Tro) - b_o * (Tro - Tout) + c0]
  Tamb = measured room temperature (slab model) during identification of
  the slab; the room model is driven by the simulated slab temperature.

Run: .venv/bin/python simulation/step5/step5_floor_slab_v1.py [--fit-only]
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIM = HERE.parent
os.environ.setdefault("MPLCONFIGDIR", str(SIM / ".cache" / "mpl"))
os.environ.setdefault("NUMBA_CACHE_DIR", str(SIM / ".cache" / "numba"))
sys.path.insert(0, str(SIM / "common"))
sys.path.insert(0, str(SIM / "step3"))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.dates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from numba import njit  # noqa: E402
from scipy.optimize import differential_evolution, minimize  # noqa: E402

import plantdata as pdx  # noqa: E402
import step3_mixer_static_v1 as S3  # noqa: E402

VERSION = "1.0"
DT = 10.0
WARMUP_S = 7200.0
MAX_D = 128
SPLIT_PATH = SIM / "step2" / "holdout_v7.json"
GATES_PATH = HERE / "gates_step5.json"
PARAMS_PATH = HERE / "step5_params_v1.json"
REPORT_PATH = HERE / "step5_floor_slab_report_v1.md"
RESULT_PATH = HERE / "step5_tune_result_v1.json"
PLOT_OV = HERE / "step5_tune_overlay_v1.png"
SELECT_TOL_C = 0.02
DE_SEED = 5

# slab parameters: name -> (lo, hi, log)
SLAB_P = {
    "tau_r": (10, 3000, True),        # s, return sensor / loop mixing
    "beta": (0.0, 1.0, False),        # -, return approach (exp(-NTU))
    "tau_in": (300, 2e5, True),       # s, 1/a_in: slab charging by loop heat
    "tau_out": (1e3, 5e5, True),      # s, 1/a_out: slab loss to the room
    "tau_off": (60, 1e5, True),       # s, return sensor relaxation, pump off
    "kappa": (-0.5, 1.0, False),      # -, pump-off blend toward room
    "d_s": (0, 1270, False),          # s, forward -> slab transport delay
    "tau_q": (600, 5e5, True),        # s, 1/a_q: screed <-> deep node
    "r_q": (0.01, 1.0, True),         # -, deep node capacity ratio (inverse)
}
ROOM_P = {
    "tau_bs": (1e3, 1e6, True),       # s, 1/b_s room <- slab
    "tau_bo": (1e3, 1e6, True),       # s, 1/b_o room -> outdoors
    "c0": (-2e-4, 2e-4, False),       # K/s, other gains/losses
}
CANDIDATES = {
    "S1 one node, no delay": dict(two=False, delay=False),
    "S2 one node, transport delay": dict(two=False, delay=True),
    "S3 two nodes, no delay": dict(two=True, delay=False),
    "S4 two nodes, transport delay": dict(two=True, delay=True),
}


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=HERE, text=True).strip()
    except Exception:
        return "unknown"


def fmt(x, nd=3):
    return "n/a" if x is None or not np.isfinite(x) else f"{x:.{nd}f}"


def table(h, rows):
    out = ["| " + " | ".join(h) + " |", "|" + "|".join("---" for _ in h) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


@njit(cache=True)
def sim_slab(p, two, tfwd, p3, tamb, tret0):
    tau_r, beta, tau_in, tau_out, tau_off, kappa, d_s, tau_q, r_q = (
        p[0], p[1], p[2], p[3], p[4], p[5], p[6], p[7], p[8])
    n = tfwd.shape[0]
    Tr = np.empty(n)
    Ts = np.empty(n)
    tr = tret0
    ts = tret0
    tq = tret0
    dn = int(round(d_s / DT))
    if dn > MAX_D:
        dn = MAX_D
    ring = np.empty(MAX_D)
    for i in range(MAX_D):
        ring[i] = tret0
    rp = 0
    a_r = DT / (tau_r + DT)
    a_off = DT / (tau_off + DT)
    for k in range(n):
        if p3[k]:
            if dn > 0:
                tf = ring[rp]
                ring[rp] = tfwd[k]
                rp += 1
                if rp >= dn:
                    rp = 0
            else:
                tf = tfwd[k]
            target = ts + beta * (tf - ts)
            q = (tf - tr) / tau_in
            tr = tr + a_r * (target - tr)
            ts = ts + DT * (q - (ts - tamb[k]) / tau_out)
        else:
            for i in range(dn):
                ring[i] = tr
            tr = tr + a_off * (ts + kappa * (tamb[k] - ts) - tr)
            ts = ts + DT * (-(ts - tamb[k]) / tau_out)
        if two:
            dq = DT * (tq - ts) / tau_q
            ts = ts + dq
            tq = tq - dq * r_q
        Tr[k] = tr
        Ts[k] = ts
    return Tr, Ts


@njit(cache=True)
def sim_room(q, ts, tout, troom0):
    tau_bs, tau_bo, c0 = q[0], q[1], q[2]
    n = ts.shape[0]
    R = np.empty(n)
    r = troom0
    for k in range(n):
        r = r + DT * ((ts[k] - r) / tau_bs - (r - tout[k]) / tau_bo + c0)
        R[k] = r
    return R


class Episodes:
    def __init__(self, S, blocks, fault_days):
        self.eps = []
        tv = S["t"].values
        for a, b in blocks:
            i0 = int(np.searchsorted(tv, a.to_datetime64()))
            i1 = int(np.searchsorted(tv, b.to_datetime64()))
            x = S.iloc[i0:i1]
            if len(x) < int(WARMUP_S / DT) + 60:
                continue
            troom = x["troom"].ffill().bfill().values.astype(float)
            tout = x["tout"].ffill().bfill().values.astype(float)
            if np.isnan(troom).any() or np.isnan(tout).any() or np.isnan(x["tret"].values[0]):
                continue
            day = x["t"].dt.strftime("%Y-%m-%d").values
            hour = x["t"].dt.hour.values
            fresh = ((x["tret_age"].values <= 900) | True)  # T_return de-duplicated: see report
            ok = ((x["tfwd_age"].values <= 900) & (x["tb_age"].values <= 900)
                  & ~np.isin(day, list(fault_days)))
            sc = ok.copy()
            sc[: int(WARMUP_S / DT)] = False
            room_sc = sc & ((hour >= 22) | (hour < 6)) & (x["troom_age"].values <= 3600)
            self.eps.append(dict(t=x["t"].values, tfwd=x["tfwd"].ffill().values.astype(float),
                                 p3=x["p3"].values.astype(bool), troom=troom, tout=tout,
                                 tret=x["tret"].values.astype(float), score=sc, room_score=room_sc,
                                 week=int(a.isocalendar()[1]), start=a))
        self.n = int(sum(e["score"].sum() for e in self.eps))
        self.n_room = int(sum(e["room_score"].sum() for e in self.eps))


def full_p(z, spec):
    names = list(SLAB_P)
    p = np.array([SLAB_P[n][0] for n in names], float)
    defaults = dict(d_s=0.0, tau_q=1e9, r_q=0.0)
    free = free_names(spec)
    for i, n in enumerate(names):
        if n in free:
            v = z[free.index(n)]
            p[i] = np.exp(v) if SLAB_P[n][2] else v
        else:
            p[i] = defaults[n]
    return p


def free_names(spec):
    f = ["tau_r", "beta", "tau_in", "tau_out", "tau_off", "kappa"]
    if spec["delay"]:
        f.append("d_s")
    if spec["two"]:
        f += ["tau_q", "r_q"]
    return f


def bounds(names, P):
    return [(np.log(P[n][0]), np.log(P[n][1])) if P[n][2] else (P[n][0], P[n][1]) for n in names]


def slab_mse(z, eps, spec):
    p = full_p(z, spec)
    tot, n = 0.0, 0
    for e in eps:
        tr, _ = sim_slab(p, spec["two"], e["tfwd"], e["p3"], e["troom"], e["tret"][0])
        d = (tr - e["tret"])[e["score"]]
        tot += float((d * d).sum()); n += len(d)
    return tot / max(n, 1)


def fit_slab(eps, spec, quick=False):
    names = free_names(spec)
    r = differential_evolution(slab_mse, bounds(names, SLAB_P), args=(eps, spec), seed=DE_SEED,
                               popsize=10, maxiter=40 if quick else 120, tol=1e-8, polish=True)
    return full_p(r.x, spec), float(r.fun)


def fit_room(eps, p, spec):
    names = list(ROOM_P)
    sims = [sim_slab(p, spec["two"], e["tfwd"], e["p3"], e["troom"], e["tret"][0])[1] for e in eps]

    def f(z):
        q = np.array([np.exp(z[0]), np.exp(z[1]), z[2]])
        tot, n = 0.0, 0
        for e, ts in zip(eps, sims):
            r = sim_room(q, ts, e["tout"], e["troom"][0])
            d = (r - e["troom"])[e["room_score"]]
            tot += float((d * d).sum()); n += len(d)
        return tot / max(n, 1)
    r = differential_evolution(f, bounds(names, ROOM_P), seed=DE_SEED, popsize=10, maxiter=80,
                               tol=1e-9, polish=True)
    return np.array([np.exp(r.x[0]), np.exp(r.x[1]), r.x[2]]), float(np.sqrt(r.fun))


def evaluate(eps, p, q, spec):
    E, R, B = [], [], []
    per = []
    for e in eps:
        tr, ts = sim_slab(p, spec["two"], e["tfwd"], e["p3"], e["troom"], e["tret"][0])
        d = (tr - e["tret"])[e["score"]]
        E.append(d)
        if q is not None:
            r = sim_room(q, ts, e["tout"], e["troom"][0])
            R.append((r - e["troom"])[e["room_score"]])
        if len(d):
            per.append(float(np.sqrt(np.mean(d ** 2))))
    e = np.concatenate(E)
    out = dict(n=int(len(e)), rmse=float(np.sqrt(np.mean(e ** 2))), mae=float(np.mean(np.abs(e))),
               bias=float(np.mean(e)), p95=float(np.percentile(np.abs(e), 95)),
               median_block_rmse=float(np.median(per)) if per else np.nan)
    if q is not None:
        r = np.concatenate(R)
        out.update(room_n=int(len(r)), room_rmse=float(np.sqrt(np.mean(r ** 2))),
                   room_bias=float(np.mean(r)))
    return out


def main():
    ctx = dict(generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), head=git_head(),
               gates_sha=sha256(GATES_PATH))
    split = json.loads(SPLIT_PATH.read_text())
    S = S3.load()
    blocks = pdx.split_blocks(split)
    fitm = pdx.blocks_mask(S["t"], blocks["FIT"])
    tunem = pdx.blocks_mask(S["t"], blocks["TUNE"])
    fd_fit = set(S3.fault_days(S3.quasi_steady(S, fitm)))
    fd_tune = set(S3.fault_days(S3.quasi_steady(S, tunem)))
    ctx["fault_days"] = sorted(fd_fit | fd_tune)
    FIT = Episodes(S, blocks["FIT"], fd_fit)
    TUNE = Episodes(S, blocks["TUNE"], fd_tune)
    ctx.update(n_fit=FIT.n, n_tune=TUNE.n, n_fit_room=FIT.n_room, n_tune_room=TUNE.n_room,
               n_fit_blocks=len(FIT.eps), n_tune_blocks=len(TUNE.eps))
    print("scored FIT", FIT.n, "TUNE", TUNE.n, "room", FIT.n_room, TUNE.n_room)

    ev = [e for e in FIT.eps if e["week"] % 2 == 0]
    od = [e for e in FIT.eps if e["week"] % 2 == 1]
    cv = {}
    for name, spec in CANDIDATES.items():
        se = []
        for tr, te in ((ev, od), (od, ev)):
            p, _ = fit_slab(tr, spec, quick=True)
            for e in te:
                x, _ = sim_slab(p, spec["two"], e["tfwd"], e["p3"], e["troom"], e["tret"][0])
                se.append((x - e["tret"])[e["score"]])
        se = np.concatenate(se)
        cv[name] = dict(rmse=float(np.sqrt(np.mean(se ** 2))), mae=float(np.mean(np.abs(se))))
        print("CV", name, cv[name], flush=True)
    best = min(v["rmse"] for v in cv.values())
    chosen = next(n for n in CANDIDATES if cv[n]["rmse"] <= best + SELECT_TOL_C)
    spec = CANDIDATES[chosen]
    p, mse = fit_slab(FIT.eps, spec)
    q, room_rmse_fit = fit_room(FIT.eps, p, spec)
    ctx.update(cv=cv, chosen=chosen, p=p, q=q)
    ctx["fit_metrics"] = evaluate(FIT.eps, p, q, spec)
    print("chosen", chosen, dict(zip(SLAB_P, np.round(p, 4))), "room", q, ctx["fit_metrics"], flush=True)
    if "--fit-only" in sys.argv:
        return

    gates = json.loads(GATES_PATH.read_text())
    tm = evaluate(TUNE.eps, p, q, spec)
    ctx["tune_metrics"] = tm
    rows = []
    for key, actual in (("tune_tret_rmse_c", tm["rmse"]), ("tune_tret_mae_c", tm["mae"]),
                        ("tune_tret_abs_bias_c", abs(tm["bias"])), ("tune_room_night_rmse_c", tm["room_rmse"])):
        g = gates["gates"][key]
        ok = actual < g["threshold"]
        mf = (g["threshold"] - actual) / g["threshold"]
        rows.append(dict(gate=key, threshold=g["threshold"], actual=actual,
                         result="PASS" if ok else "FAIL", margin_frac=mf,
                         marginal=abs(mf) < gates["marginal_margin_fraction"]))
    ctx["gate_rows"] = rows
    ctx["overall"] = all(r["result"] == "PASS" for r in rows)
    RESULT_PATH.write_text(json.dumps(dict(generated=ctx["generated"], gates_sha256=ctx["gates_sha"],
                                           chosen=chosen, tune=tm, gates=rows,
                                           overall="PASS" if ctx["overall"] else "FAIL"),
                                      indent=2, default=float))
    plot(TUNE, p, q, spec)
    export(ctx)
    report(ctx)
    print("TUNE", tm, "->", "PASS" if ctx["overall"] else "FAIL")


def plot(TUNE, p, q, spec):
    eps = sorted(TUNE.eps, key=lambda e: -e["p3"].mean())
    pick = [eps[len(eps) // 4], eps[len(eps) // 2]]
    fig, axs = plt.subplots(2, 1, figsize=(16, 9))
    for ax, e in zip(axs, pick):
        tr, ts = sim_slab(p, spec["two"], e["tfwd"], e["p3"], e["troom"], e["tret"][0])
        ro = sim_room(q, ts, e["tout"], e["troom"][0])
        t = pd.to_datetime(e["t"]).tz_localize("UTC").tz_convert("Europe/Budapest")
        ax.plot(t, e["tfwd"], color="#1f77b4", lw=0.7, label="T_forward (input)")
        ax.plot(t, e["tret"], "k", lw=1.3, label="T_return measured")
        ax.plot(t, tr, color="#2ca02c", ls="--", lw=1.3, label="T_return simulated")
        ax.plot(t, ts, color="#8c564b", lw=0.9, label="slab node (simulated)")
        ax.plot(t, e["troom"], "m", lw=1.0, label="room measured")
        ax.plot(t, ro, "m", ls=":", lw=1.3, label="room simulated")
        ax.fill_between(t, 0, 1, where=e["p3"], transform=ax.get_xaxis_transform(), color="grey",
                        alpha=0.12, label="floor pump on")
        ax.axvline(t[int(WARMUP_S / DT)], color="grey", ls="-.")
        ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%m-%d %H:%M", tz=t.tz))
        ax.set_ylabel("degC"); ax.grid(alpha=0.3); ax.legend(ncol=4, fontsize=8)
        ax.set_title(f"TUNE block {pd.Timestamp(e['start']).strftime('%Y-%m-%d %H:%M')}")
    fig.tight_layout(); fig.savefig(PLOT_OV, dpi=80); plt.close(fig)


def export(ctx):
    p, q = ctx["p"], ctx["q"]
    out = dict(version=VERSION, generated=ctx["generated"],
               model="see step5_floor_slab_v1.py docstring",
               parameters={**{n: float(v) for n, v in zip(SLAB_P, p)},
                           **{n: float(v) for n, v in zip(ROOM_P, q)}, "two_node": CANDIDATES[ctx["chosen"]]["two"]},
               parameter_units=dict(tau_r="s", beta="-", tau_in="s", tau_out="s", tau_off="s", kappa="-",
                                    d_s="s", tau_q="s", r_q="-", tau_bs="s", tau_bo="s", c0="K/s"),
               chosen_structure=ctx["chosen"], cv=ctx["cv"], fit=ctx["fit_metrics"],
               acceptance=dict(gates=ctx["gate_rows"], overall="PASS" if ctx["overall"] else "FAIL",
                               gates_sha256=ctx["gates_sha"], test_set="TUNE blocks", tune=ctx["tune_metrics"]),
               report=str(REPORT_PATH.relative_to(SIM)))
    PARAMS_PATH.write_text(json.dumps(out, indent=2, default=float))


def report(ctx):
    p = dict(zip(SLAB_P, ctx["p"]))
    q = dict(zip(ROOM_P, ctx["q"]))
    fm, tm = ctx["fit_metrics"], ctx["tune_metrics"]
    spec = CANDIDATES[ctx["chosen"]]
    L = []
    A = L.append
    A("# Step 5 Report v1.0: Underfloor Loop, Slab and Room Dynamics")
    A("")
    A("**Project:** ESP32 floor-heating HIL simulator - parameter extraction  ")
    A(f"**Generated:** {ctx['generated']} by `simulation/step5/step5_floor_slab_v1.py`  ")
    A(f"**Git HEAD:** `{ctx['head']}`  ")
    A(f"**Gates:** `step5/gates_step5.json` (SHA-256 `{ctx['gates_sha']}`, frozen before any step-5 analysis)")
    A("")
    A("## 0. Summary")
    A("")
    A(f"**TUNE (untouched) result: {'PASS' if ctx['overall'] else 'FAIL'}**")
    A("")
    A(table(["Gate", "Threshold", "Actual", "Result", "Margin"],
            [[r["gate"], f"< {r['threshold']}", fmt(r["actual"]), r["result"],
              f"{100 * r['margin_frac']:+.1f}%" + (" (marginal)" if r["marginal"] else "")] for r in ctx["gate_rows"]]))
    A("")
    A(f"Selected structure: **{ctx['chosen']}**.  Key identified quantities (FIT):")
    A("")
    A(f"* slab charging time constant tau_in = {p['tau_in']:.0f} s ({p['tau_in'] / 3600:.2f} h), driven by the "
      f"loop temperature drop T_fwd - T_ret; slab-to-room loss time constant tau_out = {p['tau_out']:.0f} s "
      f"({p['tau_out'] / 3600:.1f} h).  These hour-scale constants describe the fast screed/pipe layer "
      "that the return water sees, not the whole building mass; a second, slower node did not "
      "improve the cross-validated error (Section 3).")
    A(f"* return approach beta = {p['beta']:.3f} (return = slab + beta x (forward - slab)), return-sensor/loop "
      f"mixing tau_r = {p['tau_r']:.0f} s")
    A(f"* pump off: return sensor relaxes with tau_off = {p['tau_off']:.0f} s toward slab + "
      f"{p['kappa']:.3f} x (room - slab)")
    if spec["delay"]:
        A(f"* forward -> slab transport delay d_s = {p['d_s']:.0f} s")
    if spec["two"]:
        A(f"* deep slab node: exchange tau_q = {p['tau_q']:.0f} s, capacity ratio r_q = {p['r_q']:.3f}")
    A(f"* room: coupling to slab tau_bs = {q['tau_bs']:.0f} s ({q['tau_bs'] / 3600:.1f} h), to outdoors "
      f"tau_bo = {q['tau_bo']:.0f} s ({q['tau_bo'] / 3600:.1f} h), other net gain c0 = {q['c0'] * 3600:+.3f} K/h")
    A("")
    A("## 1. Model")
    A("")
    A("```")
    A("pump on : Tr -> Ts + beta*(Tfwd(t-d_s) - Ts)              (tau_r)")
    A("          Ts += DT*[(Tfwd(t-d_s) - Tr)/tau_in - (Ts - Troom)/tau_out]")
    A("pump off: Tr -> Ts + kappa*(Troom - Ts)                   (tau_off)")
    A("          Ts += DT*[-(Ts - Troom)/tau_out]")
    A("two-node: Ts += DT*(Tq - Ts)/tau_q ; Tq -= r_q * same")
    A("room    : Troom += DT*[(Ts - Troom)/tau_bs - (Troom - Tout)/tau_bo + c0]")
    A("```")
    A("")
    A("The slab is identified with the measured room temperature as boundary; the room model is "
      "then identified with the simulated slab temperature as input.  In the HIL both run coupled.")
    A("")
    A("## 2. Data and scoring")
    A("")
    A("* FIT/TUNE 12-hour blocks of `step2/holdout_v7.json`; Step 1 pump debounce.  All states start "
      "at the measured T_return (slab nodes) and room temperature at the block start; the first "
      "2 h are a warm-up and are not scored.")
    A("* Scored T_return samples: fresh T_forward and T_boiler (age <= 900 s), outside Step 3 fault "
      f"days ({', '.join(ctx['fault_days'])}).  T_return age is not required: Home Assistant "
      "de-duplicates unchanged DS18B20 values, so an old T_return report means an unchanged value.")
    A("* Room samples: 22:00-06:00 local only (no solar gain), room sensor age <= 3600 s.")
    A(f"* Blocks / samples: FIT {ctx['n_fit_blocks']} / {ctx['n_fit']:,} (room {ctx['n_fit_room']:,}); "
      f"TUNE {ctx['n_tune_blocks']} / {ctx['n_tune']:,} (room {ctx['n_tune_room']:,}).")
    A("")
    A("## 3. Identification and structure selection (FIT)")
    A("")
    A("Differential evolution (seed 5) on the free-run T_return mean squared error; 2-fold "
      f"week-parity CV on FIT (reduced search), simplest candidate within {SELECT_TOL_C} degC of the best.")
    A("")
    A(table(["Candidate", "CV RMSE", "CV MAE", "Chosen"],
            [[n, fmt(v["rmse"]), fmt(v["mae"]), "yes" if n == ctx["chosen"] else ""] for n, v in ctx["cv"].items()]))
    A("")
    A(table(["Parameter", "Value", "Search range"],
            [[f"`{n}`", f"{v:.6g}", f"[{SLAB_P[n][0]:g}, {SLAB_P[n][1]:g}]" if n in free_names(spec) else "fixed (not in structure)"]
             for n, v in p.items()]
            + [[f"`{n}`", f"{v:.6g}", f"[{ROOM_P[n][0]:g}, {ROOM_P[n][1]:g}]"] for n, v in q.items()]))
    A("")
    bnd = [n for n in free_names(spec) if np.isclose(p[n], SLAB_P[n][0], rtol=1e-3, atol=1e-6)
           or np.isclose(p[n], SLAB_P[n][1], rtol=1e-3, atol=1e-6)]
    bnd += [n for n in ROOM_P if np.isclose(q[n], ROOM_P[n][0], rtol=1e-3) or np.isclose(q[n], ROOM_P[n][1], rtol=1e-3)]
    A("Boundary-limited parameters: " + (", ".join(f"`{n}`" for n in bnd) if bnd else "none") + ".  "
      + ("`kappa` = 1 means that with the pump off the return manifold water relaxes completely to the "
         "room temperature (it is a stagnant pipe in a heated room), not to the slab.  " if "kappa" in bnd else "")
      + "The room parameters are weakly identifiable: the room temperature moves by only about one "
      "degree per day, so tau_bs, tau_bo and c0 trade off against each other; the combination "
      "reproduces the night-time room temperature but the individual values should not be "
      "interpreted as building UA values.")
    A("")
    A(f"FIT: T_return RMSE {fmt(fm['rmse'])}, MAE {fmt(fm['mae'])}, bias {fmt(fm['bias'])}, P95 "
      f"{fmt(fm['p95'])} degC; room (night) RMSE {fmt(fm['room_rmse'])}, bias {fmt(fm['room_bias'])} degC.")
    A("")
    A("## 4. Test on TUNE (untouched)")
    A("")
    A(f"T_return: RMSE {fmt(tm['rmse'])}, MAE {fmt(tm['mae'])}, bias {fmt(tm['bias'])}, P95 {fmt(tm['p95'])} "
      f"degC, median per-block RMSE {fmt(tm['median_block_rmse'])} degC (n = {tm['n']:,}).  Room (night): "
      f"RMSE {fmt(tm['room_rmse'])}, bias {fmt(tm['room_bias'])} degC (n = {tm['room_n']:,}).")
    A("")
    A("![overlay](step5_tune_overlay_v1.png)")
    A("")
    A("## 5. Use in the HIL simulator and limitations")
    A("")
    A("* Inputs in closed loop: simulated T_forward (Step 4), floor-pump relay, outdoor temperature "
      "(replayed or synthetic); the room temperature is simulated, so the slab model uses the "
      "simulated room in the HIL.")
    A("* The room sensor sits near a window; day-time solar gains are not modelled (night-only "
      "identification), so day-time room temperatures will be underestimated on sunny days.")
    A("* The slab state is latent; it is initialised from T_return.  The 12-hour blocks resolve "
      "time constants up to about a day; slower seasonal storage is not identified.")
    A("")
    A("## 6. Reproduction")
    A("")
    A("```bash")
    A("cd /home/gume/claude/HeatpumpsN && .venv/bin/python simulation/step5/step5_floor_slab_v1.py")
    A("```")
    REPORT_PATH.write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
