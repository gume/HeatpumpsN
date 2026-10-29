#!/usr/bin/env python3
"""Step 6 v1 - integrated HIL plant: consistency, coupled open-loop validation,
closed-loop check with a legacy-controller replica, C++ parity, boiler
emulator statistics.

Run: .venv/bin/python simulation/step6/step6_plant_validation_v1.py
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
for sub in ("common", "step2", "step3", "step4", "step5", "step6"):
    sys.path.insert(0, str(SIM / sub))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.dates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import plantdata as pdx  # noqa: E402
import hx_model_v71 as HX  # noqa: E402
import step3_mixer_static_v1 as S3  # noqa: E402
import step4_mixer_dynamics_v2 as S4  # noqa: E402
import step5_floor_slab_v1 as S5  # noqa: E402
import plant_v1 as PL  # noqa: E402

VERSION = "1.0"
DT = 10.0
W2 = 180            # Step 2 anchor index (30 min)
WARM = 720          # 2 h warm-up for coupled scoring
GATES_PATH = HERE / "gates_step6.json"
SPLIT_PATH = SIM / "step2" / "holdout_v7.json"
PARAMS_OUT = HERE / "step6_params_v1.json"
REPORT = HERE / "step6_plant_validation_report_v1.md"
PLOT_COUPLED = HERE / "step6_coupled_v1.png"
PLOT_CLOSED = HERE / "step6_closed_loop_v1.png"
HARNESS_SRC = HERE / "plant_v1_harness.cpp"
HARNESS_BIN = SIM / ".cache" / "plant_v1_harness"
N_CONSIST = 6
N_PARITY = 3


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=HERE, text=True).strip()
    except Exception:
        return "unknown"


def fmt(x, nd=3):
    return "n/a" if x is None or not np.isfinite(x) else f"{x:.{nd}g}" if abs(x) < 1e-3 and x != 0 else f"{x:.{nd}f}"


def table(h, rows):
    out = ["| " + " | ".join(h) + " |", "|" + "|".join("---" for _ in h) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


# ---------------------------------------------------------------------------
def load_params():
    p2 = json.loads((SIM / "step2" / "step2_params_v71.json").read_text())
    p4 = json.loads((SIM / "step4" / "step4_params_v2.json").read_text())
    p5 = json.loads((SIM / "step5" / "step5_params_v1.json").read_text())
    hx = np.array([p2["parameters"][n] for n in HX.PARAM_NAMES], float)
    q4 = p4["parameters"]
    knots = np.array(q4["knots_pos_pct"], float)
    xk = np.array(q4["x_at_knots"], float)
    mix = np.array([q4["transport_delay_s"] / DT, q4["tau_fwd_sensor_s"], q4["radiator_x_multiplier"]])
    q5 = p5["parameters"]
    slab = np.array([q5[n] for n in S5.SLAB_P] + [1.0 if q5["two_node"] else 0.0], float)
    room = np.array([q5[n] for n in S5.ROOM_P], float)
    tau_s = p2["parameters"]["tau_s"]
    return dict(hx=hx, knots=knots, xk=xk, mix=mix, slab=slab, room=room, tau_s=tau_s,
                sources=dict(step2=sha256(SIM / "step2" / "step2_params_v71.json"),
                             step4=sha256(SIM / "step4" / "step4_params_v2.json"),
                             step5=sha256(SIM / "step5" / "step5_params_v1.json")))


def block(S, a, b, tau_s):
    tv = S["t"].values
    i0 = int(np.searchsorted(tv, a.to_datetime64()))
    i1 = int(np.searchsorted(tv, b.to_datetime64()))
    x = S.iloc[i0:i1]
    tinc = x["tinc"].values.astype(float)
    sm = int(S4.DERIV_SMOOTH_S / DT) | 1
    tsm = pd.Series(tinc).rolling(sm, center=True, min_periods=1).mean().values
    p3 = x["p3"].values.astype(bool)
    on_for = np.zeros(len(x))
    for k in range(1, len(x)):
        on_for[k] = on_for[k - 1] + DT if p3[k] else 0.0
    bp = x["bp"].fillna(0).values.astype(float)
    recent_fire = pd.Series(bp > 0).rolling(60, min_periods=1).max().values.astype(bool)
    return dict(
        t=x["t"].values, day=x["t"].dt.strftime("%Y-%m-%d").values, hour=x["t"].dt.hour.values,
        tb=x["tb"].values.astype(float), bp=bp, pmod=x["pmod"].fillna(0).values.astype(float),
        ww=x["ww"].fillna(0).values.astype(float),
        p1=x["p1"].values.astype(bool), p2=x["p2"].values.astype(bool), p3=p3,
        pos=np.clip(x["pos"].fillna(0).values.astype(float), 0, 100),
        tout=x["tout"].ffill().bfill().values.astype(float),
        tinc=tinc, tinc_w=tinc + tau_s * np.gradient(tsm, DT),
        tret=x["tret"].ffill().bfill().values.astype(float),
        tfwd=x["tfwd"].ffill().bfill().values.astype(float),
        troom=x["troom"].ffill().bfill().values.astype(float),
        tinc_fresh=x["tinc_age"].values <= 900, tfwd_fresh=x["tfwd_age"].values <= 900,
        tb_fresh=x["tb_age"].values <= 900, troom_fresh=x["troom_age"].values <= 3600,
        on_for=on_for, recent_fire=recent_fire, start=a)


def run_plant(P, e, coupled, act_mode, hx_anchor=-1, valid=None, anchor_ok=None, pos_in=None):
    init = np.array([e["tinc"][0], e["tfwd"][0], e["tret"][0], e["troom"][0], e["pos"][0]])
    n = len(e["tb"])
    return PL.plant_run(P["hx"], P["knots"], P["xk"], P["mix"], P["slab"], P["room"],
                        np.array([1 if coupled else 0, act_mode], np.int64),
                        e["tb"], e["bp"], e["pmod"], e["ww"], e["p1"], e["p2"], e["p3"],
                        e["pos"] if pos_in is None else pos_in, e["tout"],
                        e["tinc"], e["tinc_w"], e["tret"], e["tfwd"], e["troom"],
                        valid if valid is not None else np.ones(n, bool),
                        anchor_ok if anchor_ok is not None else np.ones(n, bool),
                        hx_anchor, init)


# ---------------------------------------------------------------------------
def consistency(P, eps):
    worst = dict(hx=0.0, mixer=0.0, tret=0.0, slab=0.0, room=0.0)
    for e in eps:
        valid = e["tb_fresh"].copy(); valid[0] = True
        anchor_ok = e["tinc_fresh"] & e["tfwd_fresh"] & e["tb_fresh"]
        out = run_plant(P, e, False, 0, hx_anchor=W2, valid=valid, anchor_ok=anchor_ok)
        ts, *_ = HX.simulate(P["hx"], e["tb"], e["tret"], e["troom"], e["pos"], e["bp"], e["pmod"],
                             e["ww"], e["p1"], e["p2"], e["p3"], e["tinc"][0], e["tinc"][0],
                             e["tinc"][0], W2, e["tinc"], valid)
        mx = S4.simulate(P["knots"], P["xk"], int(P["mix"][0]), float(P["mix"][1]), e["pos"],
                         e["tinc_w"], e["tret"], e["p3"], e["tfwd"], anchor_ok, e["p1"] | e["p2"],
                         float(P["mix"][2]))
        two = P["slab"][9] > 0.5
        tr, tsl = S5.sim_slab(P["slab"][:9], two, e["tfwd"], e["p3"], e["troom"], e["tret"][0])
        ro = S5.sim_room(P["room"], tsl, e["tout"], e["troom"][0])
        for key, a, b in (("hx", out[:, 0], ts), ("mixer", out[:, 2], mx), ("tret", out[:, 3], tr),
                          ("slab", out[:, 4], tsl), ("room", out[:, 5], ro)):
            worst[key] = max(worst[key], float(np.max(np.abs(a - b))))
    return worst


def coupled_scores(P, eps, fault):
    acc = {k: [] for k in ("tinc", "tfwd", "tret", "room")}
    per = []
    for e in eps:
        out = run_plant(P, e, True, 0)
        k = np.arange(len(e["tb"]))
        base = (k >= WARM) & e["tb_fresh"] & ~np.isin(e["day"], list(fault))
        m_inc = base & e["tinc_fresh"]
        m_fwd = base & e["p3"] & (e["on_for"] >= 300) & (e["ww"] < 0.5) & e["tfwd_fresh"] & e["tinc_fresh"]
        m_ret = base & e["tfwd_fresh"]
        m_room = base & ((e["hour"] >= 22) | (e["hour"] < 6)) & e["troom_fresh"]
        acc["tinc"].append(out[m_inc, 0] - e["tinc"][m_inc])
        acc["tfwd"].append(out[m_fwd, 2] - e["tfwd"][m_fwd])
        acc["tret"].append(out[m_ret, 3] - e["tret"][m_ret])
        acc["room"].append(out[m_room, 5] - e["troom"][m_room])
        per.append((e, out))
    res = {}
    for kk, v in acc.items():
        x = np.concatenate(v)
        res[kk] = dict(n=int(len(x)), rmse=float(np.sqrt(np.mean(x ** 2))), mae=float(np.mean(np.abs(x))),
                       bias=float(np.mean(x)))
    return res, per


def closed_loop(P, eps, fault):
    SF, MF, SP, MP = [], [], [], []
    keep = []
    for e in eps:
        out = run_plant(P, e, True, 2)
        k = np.arange(len(e["tb"]))
        m = ((k >= WARM) & e["p3"] & (e["on_for"] >= 1800) & (e["ww"] < 0.5) & e["recent_fire"]
             & e["tfwd_fresh"] & e["tb_fresh"] & ~np.isin(e["day"], list(fault)))
        SF.append(out[m, 2]); MF.append(e["tfwd"][m]); SP.append(out[m, 6]); MP.append(e["pos"][m])
        keep.append((e, out, m))
    sf, mf, sp, mp = map(np.concatenate, (SF, MF, SP, MP))
    return dict(n=int(len(sf)), tfwd_sim_mean=float(sf.mean()), tfwd_meas_mean=float(mf.mean()),
                tfwd_sim_std=float(sf.std()), tfwd_meas_std=float(mf.std()),
                pos_sim_mean=float(sp.mean()), pos_meas_mean=float(mp.mean()),
                tfwd_mean_abs_diff=float(abs(sf.mean() - mf.mean())),
                pos_mean_abs_diff=float(abs(sp.mean() - mp.mean()))), keep


def parity(P, eps):
    if not HARNESS_BIN.exists() or HARNESS_BIN.stat().st_mtime < HARNESS_SRC.stat().st_mtime:
        subprocess.check_call(["g++", "-O2", "-std=c++17", "-o", str(HARNESS_BIN), str(HARNESS_SRC)],
                              cwd=HERE)
    tmp = SIM / ".cache" / "parity"
    tmp.mkdir(parents=True, exist_ok=True)
    worst = 0.0
    rows = 0
    for i, e in enumerate(eps):
        for mode in (0, 1):
            pos_in = e["pos"]
            out = run_plant(P, e, True, mode, pos_in=pos_in)
            init = [e["tinc"][0], e["tfwd"][0], e["tret"][0], e["troom"][0], e["pos"][0]]
            vals = ([mode] + init + list(P["hx"]) + [len(P["knots"])] + list(P["knots"]) + list(P["xk"])
                    + list(P["mix"]) + list(P["slab"]) + list(P["room"]))
            pf = tmp / f"p{i}_{mode}.txt"
            pf.write_text(" ".join(repr(float(v)) if not isinstance(v, int) else str(v) for v in vals))
            df = pd.DataFrame(dict(tb=e["tb"], bp=e["bp"], pmod=e["pmod"], ww=e["ww"], p1=e["p1"].astype(int),
                                   p2=e["p2"].astype(int), p3=e["p3"].astype(int), pos=pos_in, tout=e["tout"]))
            fi, fo = tmp / f"in{i}_{mode}.csv", tmp / f"out{i}_{mode}.csv"
            df.to_csv(fi, index=False, float_format="%.17g")
            subprocess.check_call([str(HARNESS_BIN), str(pf), str(fi), str(fo)])
            c = pd.read_csv(fo).values
            worst = max(worst, float(np.max(np.abs(c[:, :7] - out[:, :7]))))
            worst = max(worst, float(np.max(np.abs(c[:, 7] - out[:, 7]))))
            rows += len(c)
    return dict(max_abs=worst, rows=rows)


def boiler_stats(S, fitm):
    x = S[fitm]
    bp = x["bp"].fillna(0).values > 0
    pm = x["pmod"].fillna(0).values > 0
    ww = x["ww"].fillna(0).values >= 0.5
    ch = bp & pm & ~ww
    tb = x["tb"].values
    ch_st = pd.Series(ch).rolling(60, min_periods=60).min().fillna(0).values.astype(bool)
    out = dict(tb_ch_steady_p10=float(np.nanpercentile(tb[ch_st], 10)),
               tb_ch_steady_median=float(np.nanmedian(tb[ch_st])),
               tb_ch_steady_p90=float(np.nanpercentile(tb[ch_st], 90)),
               bp_ch_steady_median=float(np.nanmedian(x["bp"].values[ch_st])))

    def runs(mask):
        r, c = [], 0
        for v in mask:
            if v:
                c += 1
            elif c:
                r.append(c * DT); c = 0
        return np.array(r)
    on = runs(bp & ~ww)
    out.update(burner_on_median_s=float(np.median(on)), burner_on_p90_s=float(np.percentile(on, 90)))
    dw = runs(ww)
    days = len(np.unique(x["t"].dt.strftime("%Y-%m-%d")))
    out.update(dhw_events=int(len(dw)), dhw_days=int(days), dhw_per_day=float(len(dw) / max(days, 1)),
               dhw_duration_median_s=float(np.median(dw)), dhw_duration_p90_s=float(np.percentile(dw, 90)),
               tb_dhw_max_median=float(np.nanmedian(pd.Series(tb).where(ww).groupby((~pd.Series(ww)).cumsum()).max())))
    return out


# ---------------------------------------------------------------------------
def main():
    ctx = dict(generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), head=git_head(),
               gates_sha=sha256(GATES_PATH))
    gates = json.loads(GATES_PATH.read_text())["gates"]
    P = load_params()
    ctx["P"] = P
    split = json.loads(SPLIT_PATH.read_text())
    S = S3.load()
    bl = pdx.split_blocks(split)
    fitm = pdx.blocks_mask(S["t"], bl["FIT"])
    tunem = pdx.blocks_mask(S["t"], bl["TUNE"])
    fault = set(S3.fault_days(S3.quasi_steady(S, fitm))) | set(S3.fault_days(S3.quasi_steady(S, tunem)))
    ctx["fault_days"] = sorted(fault)
    FIT = [block(S, a, b, P["tau_s"]) for a, b in bl["FIT"]]
    TUNE_all = [block(S, a, b, P["tau_s"]) for a, b in bl["TUNE"]]
    TUNE = [e for e in TUNE_all if e["tb_fresh"].all() and len(e["tb"]) > WARM + 60]
    ctx["n_tune_all"], ctx["n_tune"] = len(TUNE_all), len(TUNE)

    # 1. component consistency (FIT blocks with floor heating)
    pick = sorted(FIT, key=lambda e: -e["p3"].mean())[:N_CONSIST]
    ctx["consistency"] = consistency(P, pick)
    print("consistency", ctx["consistency"], flush=True)
    # 2. coupled open loop (TUNE)
    ctx["coupled"], per = coupled_scores(P, TUNE, fault)
    print("coupled", ctx["coupled"], flush=True)
    # 3. closed loop with the legacy replica
    ctx["closed"], keep = closed_loop(P, TUNE, fault)
    print("closed", ctx["closed"], flush=True)
    # 4. C++ parity
    ppick = sorted(TUNE, key=lambda e: -e["p3"].mean())[:N_PARITY]
    ctx["parity"] = parity(P, ppick)
    print("parity", ctx["parity"], flush=True)
    # 5. boiler emulator statistics (FIT)
    ctx["boiler"] = boiler_stats(S, fitm)
    print("boiler", ctx["boiler"], flush=True)

    rows = []
    actual = {
        "component_consistency_max_abs_c": max(ctx["consistency"].values()),
        "cpp_parity_max_abs_c": ctx["parity"]["max_abs"],
        "coupled_tinc_rmse_c": ctx["coupled"]["tinc"]["rmse"],
        "coupled_tfwd_rmse_c": ctx["coupled"]["tfwd"]["rmse"],
        "coupled_tret_rmse_c": ctx["coupled"]["tret"]["rmse"],
        "coupled_room_night_rmse_c": ctx["coupled"]["room"]["rmse"],
        "closed_loop_tfwd_mean_abs_diff_c": ctx["closed"]["tfwd_mean_abs_diff"],
        "closed_loop_pos_mean_abs_diff_pct": ctx["closed"]["pos_mean_abs_diff"],
    }
    for key, g in gates.items():
        a = actual[key]
        ok = a < g["threshold"]
        rows.append(dict(gate=key, threshold=g["threshold"], actual=a, result="PASS" if ok else "FAIL"))
    ctx["gate_rows"] = rows
    ctx["overall"] = all(r["result"] == "PASS" for r in rows)
    plots(per, keep)
    export(ctx)
    report(ctx)
    print("GATES", [(r["gate"], r["result"]) for r in rows])


def plots(per, keep):
    sel = sorted(per, key=lambda pe: -pe[0]["p3"].mean())
    sel = [sel[len(sel) // 4], sel[len(sel) // 2]]
    fig, axs = plt.subplots(2, 1, figsize=(16, 9))
    for ax, (e, out) in zip(axs, sel):
        t = pd.to_datetime(e["t"]).tz_localize("UTC").tz_convert("Europe/Budapest")
        for col, c, lab in ((0, "#d62728", "T_incoming"), (2, "#1f77b4", "T_forward"),
                            (3, "#2ca02c", "T_return"), (5, "#9467bd", "room")):
            meas = {0: e["tinc"], 2: e["tfwd"], 3: e["tret"], 5: e["troom"]}[col]
            ax.plot(t, meas, color=c, lw=1.1, label=f"{lab} measured")
            ax.plot(t, out[:, col], color=c, lw=1.1, ls="--", label=f"{lab} coupled sim")
        ax.axvline(t[WARM], color="grey", ls="-.")
        ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%m-%d %H:%M", tz=t.tz))
        ax.set_ylabel("degC"); ax.grid(alpha=0.3); ax.legend(ncol=4, fontsize=7)
        ax.set_title(f"Coupled open loop, TUNE block {pd.Timestamp(e['start']).strftime('%Y-%m-%d %H:%M')}")
    fig.tight_layout(); fig.savefig(PLOT_COUPLED, dpi=80); plt.close(fig)

    e, out, m = max(keep, key=lambda z: z[2].sum())
    t = pd.to_datetime(e["t"]).tz_localize("UTC").tz_convert("Europe/Budapest")
    fig, axs = plt.subplots(2, 1, figsize=(16, 8), gridspec_kw=dict(height_ratios=[2, 1]))
    axs[0].plot(t, e["tfwd"], "k", lw=1.1, label="T_forward measured (legacy controller)")
    axs[0].plot(t, out[:, 2], "#1f77b4", lw=1.1, ls="--", label="T_forward, replica controller on simulated plant")
    axs[0].axhline(PL.LEG_SP, color="grey", ls=":", label="legacy setpoint")
    axs[1].plot(t, e["pos"], "k", lw=1, label="valve measured")
    axs[1].plot(t, out[:, 6], "#1f77b4", lw=1, ls="--", label="valve simulated")
    for a in axs:
        a.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%m-%d %H:%M", tz=t.tz))
        a.grid(alpha=0.3); a.legend(fontsize=8)
    axs[0].set_title(f"Closed loop, TUNE block {pd.Timestamp(e['start']).strftime('%Y-%m-%d %H:%M')}")
    fig.tight_layout(); fig.savefig(PLOT_CLOSED, dpi=80); plt.close(fig)


def export(ctx):
    P = ctx["P"]
    out = dict(
        version=VERSION, generated=ctx["generated"],
        model="step6/plant_v1.py (reference) and step6/plant_v1.hpp (C++)",
        parameters=dict(DT_s=DT, actuator_full_stroke_s=PL.ACT_STROKE_S, max_pipe_cells=PL.MAX_PIPE_N,
                        max_mixer_delay_cells=PL.MAX_MIX_D, max_slab_delay_cells=PL.MAX_SLAB_D,
                        diverter_clear_s=PL.DIV_CLEAR_S,
                        boiler_emulator=ctx["boiler"]),
        parameter_units=dict(DT_s="s", actuator_full_stroke_s="s", diverter_clear_s="s"),
        constants=dict(legacy_replica=dict(setpoint=PL.LEG_SP, kp=PL.LEG_KP, kd=PL.LEG_KD,
                                           deadband_hi=PL.LEG_DB_HI, deadband_lo=PL.LEG_DB_LO)),
        source_parameter_sha256=P["sources"],
        acceptance=dict(gates=ctx["gate_rows"], overall="PASS" if ctx["overall"] else "FAIL",
                        gates_sha256=ctx["gates_sha"], coupled=ctx["coupled"], closed=ctx["closed"],
                        consistency=ctx["consistency"], parity=ctx["parity"]),
        report=str(REPORT.relative_to(SIM)),
    )
    PARAMS_OUT.write_text(json.dumps(out, indent=2, default=float))


def report(ctx):
    c, cl, b = ctx["coupled"], ctx["closed"], ctx["boiler"]
    L = []
    A = L.append
    A("# Step 6 Report v1.0: Integrated HIL Plant Simulator and Validation")
    A("")
    A("**Project:** ESP32 floor-heating HIL simulator - parameter extraction  ")
    A(f"**Generated:** {ctx['generated']} by `simulation/step6/step6_plant_validation_v1.py`  ")
    A(f"**Git HEAD:** `{ctx['head']}`  ")
    A(f"**Gates:** `step6/gates_step6.json` (SHA-256 `{ctx['gates_sha']}`, frozen before any step-6 code)")
    A("")
    A("## 0. Summary")
    A("")
    A(f"**Result: {'PASS' if ctx['overall'] else 'FAIL'}** "
      f"({sum(r['result'] == 'PASS' for r in ctx['gate_rows'])} of {len(ctx['gate_rows'])} gates pass)")
    A("")
    A(table(["Gate", "Threshold", "Actual", "Result"],
            [[r["gate"], f"< {r['threshold']:g}", f"{r['actual']:.4g}", r["result"]] for r in ctx["gate_rows"]]))
    A("")
    A("The plant model chains the identified sub-models of Steps 2-5 in one deterministic 10 s loop "
      "(`plant_v1.py`) and is ported to a header-only C++ class (`plant_v1.hpp`) for the ESP32 "
      "emulation backend or a host test bench.  No parameter was changed in Step 6; every value "
      "comes from the step parameter files (hashes in `step6_params_v1.json`) and is collected in "
      "`simulation/plant_parameters.json`.")
    A("")
    A("## 1. Plant structure")
    A("")
    A("```")
    A("inputs : boiler T_boiler, burner %, primary pump %, DHW flag   (replay or boiler emulator)")
    A("         pump relays P1, P2 (radiators), P3 (floor)             (controller under test)")
    A("         valve target (or position)                              (controller under test)")
    A("         outdoor temperature                                     (replay or synthetic)")
    A("valve  : travels at 100 % / 145 s toward the target")
    A("Step 2 : HX / fork water T and T_incoming sensor  <- T_return, T_room, valve, pumps, boiler")
    A("Step 3/4: Tmix = T_ret + x(pos)*m_rad*(T_inc,water - T_ret), delay, T_forward sensor lag")
    A("Step 5 : return water, slab node, room            <- T_forward, P3, outdoor")
    A("outputs: T_incoming (new controller's 'T_boiler' sensor position), T_forward (T_mix_out),")
    A("         T_return_floor, room, valve position")
    A("```")
    A("")
    A("Plant-only assumptions (not identified in Steps 2-5): with the floor pump off the forward "
      "sensor relaxes like the stagnant return manifold (Step 5 tau_off, kappa); at a pump start the "
      "forward delay line holds the current forward-sensor value; coupled sub-models exchange "
      "T_return and T_room with a one-sample (10 s) lag.")
    A("")
    A("## 2. Component consistency")
    A("")
    A(f"On {N_CONSIST} FIT blocks with the most floor heating, the plant in component mode (every "
      "sub-model fed the measured inputs it was identified with) is compared with the original "
      "step implementations (`hx_model_v71.simulate`, `step4_mixer_dynamics_v2.simulate`, "
      "`step5_floor_slab_v1.sim_slab/sim_room`):")
    A("")
    A(table(["Sub-model", "Max abs difference [degC]"], [[k, f"{v:.3g}"] for k, v in ctx["consistency"].items()]))
    A("")
    A("## 3. Coupled open-loop validation (TUNE)")
    A("")
    A(f"Inputs: measured boiler signals, pump states, valve position and outdoor temperature.  All "
      "temperatures are simulated from the block start (initialised from measurements) without any "
      f"re-anchoring; the first 2 h are a warm-up.  Blocks with a boiler-telemetry outage are not "
      f"used (the HIL has no outages): {ctx['n_tune']} of {ctx['n_tune_all']} TUNE blocks.  Step 3 "
      f"fault days excluded ({', '.join(ctx['fault_days'])}).")
    A("")
    A(table(["Signal", "Samples", "RMSE", "MAE", "Bias (sim - meas)", "Scored when"],
            [["T_incoming", f"{c['tinc']['n']:,}", fmt(c["tinc"]["rmse"]), fmt(c["tinc"]["mae"]), fmt(c["tinc"]["bias"]), "fresh"],
             ["T_forward", f"{c['tfwd']['n']:,}", fmt(c["tfwd"]["rmse"]), fmt(c["tfwd"]["mae"]), fmt(c["tfwd"]["bias"]), "floor pump on >= 5 min, no DHW"],
             ["T_return", f"{c['tret']['n']:,}", fmt(c["tret"]["rmse"]), fmt(c["tret"]["mae"]), fmt(c["tret"]["bias"]), "all"],
             ["room", f"{c['room']['n']:,}", fmt(c["room"]["rmse"]), fmt(c["room"]["mae"]), fmt(c["room"]["bias"]), "22:00-06:00"]]))
    A("")
    A("![coupled](step6_coupled_v1.png)")
    A("")
    A("## 4. Closed-loop check with a legacy-controller replica")
    A("")
    A(f"A Python replica of the legacy mixer logic (`analysis/OLD-CODE/heatpumps.yaml`: PID on "
      f"T_forward with setpoint {PL.LEG_SP} degC, kp {PL.LEG_KP}, kd {PL.LEG_KD}, deadband "
      f"+{PL.LEG_DB_HI}/-{PL.LEG_DB_LO} K, 20 s update, 2 % residue accumulation; T_incoming-dependent "
      "maximum opening 70-90 %, 60 % when T_forward exceeds the setpoint; over-temperature slam to "
      "the minimum at setpoint + 3 K until setpoint - 1 K; close on floor-pump stop) drives the "
      "simulated valve while boiler signals and pump states are replayed.  Not modelled: stall "
      "check, nightly re-zeroing, HA setpoint changes (the setpoint used by HA is not logged; the "
      "firmware default is used).")
    A("")
    A(table(["Quantity", "Simulated", "Measured"],
            [["mean T_forward [degC]", fmt(cl["tfwd_sim_mean"]), fmt(cl["tfwd_meas_mean"])],
             ["std T_forward [degC]", fmt(cl["tfwd_sim_std"]), fmt(cl["tfwd_meas_std"])],
             ["mean valve position [%]", fmt(cl["pos_sim_mean"]), fmt(cl["pos_meas_mean"])]]))
    A("")
    A(f"Samples: {cl['n']:,} (floor pump on >= 30 min, no DHW, boiler fired within 10 min, TUNE).")
    A("")
    A("The replica reproduces the regulated level around the setpoint and the operating valve band.  "
      "It does not reproduce the legacy controller's repeated valve slams to 0 % during heating "
      "(visible in the plot as T_forward dips to the return temperature; Step 1 classified most "
      "zero-position episodes as failsafe slams, and the stall check and pump-power glitches that "
      "trigger them are not replicated).  This is why the measured T_forward has a larger spread "
      "than the simulated one, and the mean-level gate should be read together with this "
      "difference: the plant is validated at the operating point, not for the legacy fault "
      "reactions.")
    A("")
    A("![closed](step6_closed_loop_v1.png)")
    A("")
    A("## 5. C++ port and parity")
    A("")
    A(f"`plant_v1.hpp` (class `hpn_plant::Plant`, one `step()` per 10 s) mirrors the coupled path of "
      f"`plant_run` with actuator modes 0 (position replay) and 1 (target + 145 s travel).  The "
      f"harness `plant_v1_harness.cpp` (g++ -O2 -std=c++17) reads parameters and inputs, prints "
      f"%.17g; on {N_PARITY} TUNE blocks x 2 modes ({ctx['parity']['rows']:,} rows) the maximum "
      f"absolute difference over all outputs is {ctx['parity']['max_abs']:.3g}.")
    A("")
    A("## 6. Boiler emulator statistics (FIT)")
    A("")
    A("For HIL runs without replayed boiler telemetry, a boiler emulator can be parameterised from "
      "these FIT statistics (not validated as a dynamic model):")
    A("")
    A(table(["Quantity", "Value"],
            [["T_boiler during steady CH firing, P10 / median / P90 [degC]",
              f"{b['tb_ch_steady_p10']:.1f} / {b['tb_ch_steady_median']:.1f} / {b['tb_ch_steady_p90']:.1f}"],
             ["burner modulation during steady CH firing, median [%]", f"{b['bp_ch_steady_median']:.0f}"],
             ["burner on-time per firing (outside DHW), median / P90 [s]", f"{b['burner_on_median_s']:.0f} / {b['burner_on_p90_s']:.0f}"],
             ["DHW events per day (FIT)", f"{b['dhw_per_day']:.1f}"],
             ["DHW duration median / P90 [s]", f"{b['dhw_duration_median_s']:.0f} / {b['dhw_duration_p90_s']:.0f}"],
             ["T_boiler peak during DHW, median [degC]", f"{b['tb_dhw_max_median']:.1f}"]]))
    A("")
    A("## 7. Limitations")
    A("")
    A("* The acceptance of the sub-models is mixed: Step 2 failed its frozen holdout (transitions "
      "and DHW), Step 4 failed its holdout narrowly, Steps 3 and 5 passed (reports in each step).  "
      "The coupled results above inherit these errors.")
    A("* The HIL plant reproduces the legacy operating envelope; behaviour far outside it "
      "(for example valve positions 10-45 % held for long periods, or new controller flush/pause "
      "strategies) relies on interpolated parts of the model.")
    A("* Room solar gains, radiator circuits' heat delivery and the boiler's internal control are "
      "not modelled; the boiler is an input (replay or emulator).")
    A("")
    A("## 8. Reproduction")
    A("")
    A("```bash")
    A("cd /home/gume/claude/HeatpumpsN && .venv/bin/python simulation/step6/step6_plant_validation_v1.py")
    A(".venv/bin/python simulation/collect_parameters.py")
    A("```")
    REPORT.write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
