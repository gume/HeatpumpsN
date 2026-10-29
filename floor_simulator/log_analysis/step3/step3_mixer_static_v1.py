#!/usr/bin/env python3
"""Step 3 - 3-way mixer static mixing characteristic.

    T_fwd,ss = T_ret + x(pos, regime, ...) * (T_inc - T_ret)

x is the hot-water fraction delivered through the mixer.  Run from the
HeatpumpsN folder:  .venv/bin/python simulation/step3/step3_mixer_static_v1.py

Protocol
  * data: heating season, 10 s causal grid (simulation/common/plantdata.py),
    pump states by the Step 1 centred 5-min majority filter;
  * split: FIT / TUNE blocks of step2/holdout_v7.json (12-h blocks);
    FIT identifies, candidate structures are compared by 2-fold week-parity
    cross-validation inside FIT, TUNE is the untouched test set evaluated
    against step3/gates_step3.json (frozen before any step-3 analysis);
  * quasi-steady samples (definition QS below) are used for fitting and for
    the gates.
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
sys.path.insert(0, str(SIM / "common"))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy.ndimage import median_filter  # noqa: E402
from scipy.optimize import lsq_linear  # noqa: E402

import plantdata as pdx  # noqa: E402

VERSION = "1.0"
SPLIT_PATH = SIM / "step2" / "holdout_v7.json"
GATES_PATH = HERE / "gates_step3.json"
PARAMS_PATH = HERE / "step3_params_v1.json"
REPORT_PATH = HERE / "step3_mixer_static_report_v1.md"
PLOT_LAW = HERE / "step3_mixing_law_v1.png"
PLOT_RES = HERE / "step3_residuals_v1.png"
RESULT_PATH = HERE / "step3_tune_result_v1.json"

# Quasi-steady definition (QS), frozen with the first commit of this script
QS = dict(
    window_s=300,          # trailing window
    pos_range_max=0.5,     # % (mixer_state max - min inside the window)
    min_lift_c=10.0,       # mean(T_inc - T_ret) over the window
    max_age_s=900.0,       # T_inc / T_fwd ages inside the window
    sample_every_s=180,    # one sample every 3 min (decorrelation)
)
KNOTS = np.array([0, 25, 50, 52.5, 55, 57.5, 60, 65, 70, 80, 90], float)   # legacy max clamp 90 %
SMOOTH_EQ_SAMPLES = 10.0   # second-difference penalty weight, in units of "typical samples"
FAULT_X_CLOSED = 0.15      # x above this at mixer_state <= 1 % is impossible for a closed valve
HUBER_C = 1.0              # degC, robust weighting threshold
SELECT_TOL_C = 0.02        # simplest model within this of the best CV RMSE wins
DRIFT_HOURS = 12.0         # drift regressor saturates after this time since the nightly reset


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


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------
def load() -> pd.DataFrame:
    S = pdx.season_grid(10)
    for i in (1, 2, 3):
        raw = (S[f"p{i}w"].fillna(0).values >= pdx.PUMP_ON_W).astype(np.int8)
        S[f"p{i}"] = median_filter(raw, size=31, mode="nearest").astype(bool)
    S["rad"] = S["p1"] | S["p2"]
    # time since the last nightly reset (mixer_state = 0 between 00:00 and 00:30)
    loc = S["t"]
    reset = (S["pos"].fillna(1) <= 0.0) & (loc.dt.hour == 0) & (loc.dt.minute <= 30)
    last = np.where(reset.values, np.arange(len(S)), -1)
    last = np.maximum.accumulate(last)
    S["h_since_reset"] = np.where(last >= 0, (np.arange(len(S)) - last) * 10 / 3600.0, np.nan)
    return S


def quasi_steady(S: pd.DataFrame, inside: np.ndarray) -> pd.DataFrame:
    w = int(QS["window_s"] / 10)
    roll = lambda s: s.rolling(w, min_periods=w)  # noqa: E731
    d = pd.DataFrame(dict(
        t=S["t"],
        pos=roll(S["pos"]).mean(),
        pos_rng=roll(S["pos"]).max() - roll(S["pos"]).min(),
        tinc=roll(S["tinc"]).mean(), tret=roll(S["tret"]).mean(), tfwd=roll(S["tfwd"]).mean(),
        p3=roll(S["p3"].astype(float)).min(),
        rad_min=roll(S["rad"].astype(float)).min(), rad_max=roll(S["rad"].astype(float)).max(),
        ww=roll(S["ww"].fillna(0)).max(),
        age=roll(S[["tinc_age", "tfwd_age"]].max(axis=1)).max(),
        inside=roll(pd.Series(inside.astype(float))).min(),
        bmi=roll(S["bmi"]).mean(), bmi_rng=roll(S["bmi"]).max() - roll(S["bmi"]).min(),
        h_reset=S["h_since_reset"],
    ))
    ok = ((d.p3 == 1) & (d.pos_rng < QS["pos_range_max"]) & (d.ww == 0)
          & (d.rad_min == d.rad_max) & (d.age <= QS["max_age_s"]) & (d.inside == 1)
          & ((d.tinc - d.tret) >= QS["min_lift_c"]))
    d = d[ok].copy()
    # decimate: keep one sample per sample_every_s
    keep = []
    last = None
    step = pd.Timedelta(seconds=QS["sample_every_s"])
    for i, t in zip(d.index, d["t"]):
        if last is None or t - last >= step:
            keep.append(i)
            last = t
    d = d.loc[keep].copy()
    d["rad"] = d["rad_max"].astype(int)
    d["lift"] = d["tinc"] - d["tret"]
    d["x"] = (d["tfwd"] - d["tret"]) / d["lift"]
    d["week"] = d["t"].dt.isocalendar().week.astype(int)
    d["day"] = d["t"].dt.strftime("%Y-%m-%d")
    return d


def fault_days(d: pd.DataFrame) -> list[str]:
    """Calendar days with an actuation/position fault: the dead-reckoned
    position says closed (<= 1 %) while the delivered hot fraction exceeds
    FAULT_X_CLOSED.  The whole day is excluded (the fault persists until the
    position reference is re-established)."""
    bad = d[(d["pos"] <= 1.0) & (d["x"] > FAULT_X_CLOSED)]
    return sorted(bad["day"].unique().tolist())


# ---------------------------------------------------------------------------
# models: T_fwd - T_ret = lift * x;  x linear in parameters
# ---------------------------------------------------------------------------
def hat_basis(pos: np.ndarray) -> np.ndarray:
    """Piecewise-linear interpolation weights on KNOTS (n x K)."""
    p = np.clip(pos, KNOTS[0], KNOTS[-1])
    B = np.zeros((len(p), len(KNOTS)))
    j = np.clip(np.searchsorted(KNOTS, p, side="right") - 1, 0, len(KNOTS) - 2)
    w = (p - KNOTS[j]) / (KNOTS[j + 1] - KNOTS[j])
    B[np.arange(len(p)), j] = 1 - w
    B[np.arange(len(p)), j + 1] = w
    return B


MODELS = {
    "M1 monotone table": dict(rad=False, drift=False),
    "M2 monotone table + radiator factor": dict(rad=True, drift=False),
    "M3 monotone table + dead-reckoning drift": dict(rad=False, drift=True),
    "M4 monotone table + radiator factor + drift": dict(rad=True, drift=True),
}


def design(d: pd.DataFrame, spec: dict) -> tuple[np.ndarray, np.ndarray, list]:
    """Linear design in increments: x(knot_k) = c0 + sum_{j<=k} inc_j, inc_j >= 0.
    Extra columns: radiator offset (x += r * rad) and drift (x += g * h/12)."""
    B = hat_basis(d["pos"].values)
    K = len(KNOTS)
    T = np.tril(np.ones((K, K)))            # x_k = sum_{j<=k} z_j (z_0 = level, z_j>=0)
    X = (B @ T) * d["lift"].values[:, None]
    names = ["x0"] + [f"inc_{KNOTS[k]:g}" for k in range(1, K)]
    lb = [0.0] + [0.0] * (K - 1)
    ub = [1.0] + [1.0] * (K - 1)
    cols = [X]
    if spec["rad"]:
        cols.append((d["rad"].values * d["lift"].values)[:, None])
        names.append("rad_offset"); lb.append(-1.0); ub.append(1.0)
    if spec["drift"]:
        h = np.nan_to_num(np.minimum(d["h_reset"].values, DRIFT_HOURS) / DRIFT_HOURS, nan=1.0)
        cols.append((h * d["lift"].values)[:, None])
        names.append("drift_x_per_12h"); lb.append(-1.0); ub.append(1.0)
    X = np.hstack(cols)
    y = (d["tfwd"] - d["tret"]).values
    # smoothness: second differences of x over the knots (scaled to the data)
    K = len(KNOTS)
    if K >= 3:
        scale = np.sqrt(SMOOTH_EQ_SAMPLES) * float(np.median(d["lift"].values))
        D = np.zeros((K - 2, X.shape[1]))
        for k in range(K - 2):
            h1, h2 = KNOTS[k + 1] - KNOTS[k], KNOTS[k + 2] - KNOTS[k + 1]
            row = np.zeros(K)
            row[k], row[k + 1], row[k + 2] = 1 / h1, -(1 / h1 + 1 / h2), 1 / h2
            D[k, :K] = (row @ T) * scale * 10.0   # per 10 % of travel
        X = np.vstack([X, D])
        y = np.concatenate([y, np.zeros(K - 2)])
    return X, y, (names, np.array(lb), np.array(ub))


def fit(d: pd.DataFrame, spec: dict) -> dict:
    X, y, (names, lb, ub) = design(d, spec)
    n = len(d)
    w = np.ones(len(y))
    for _ in range(20):  # IRLS Huber (penalty rows keep weight 1)
        sw = np.sqrt(w)
        r = lsq_linear(X * sw[:, None], y * sw, bounds=(lb, ub), method="bvls")
        res = y - X @ r.x
        w_new = np.where(np.abs(res) <= HUBER_C, 1.0, HUBER_C / np.maximum(np.abs(res), 1e-12))
        w_new[n:] = 1.0
        if np.max(np.abs(w_new - w)) < 1e-4:
            break
        w = w_new
    return dict(spec=spec, names=names, coef=r.x, n=n, weights=w[:n])


def predict(m: dict, d: pd.DataFrame) -> np.ndarray:
    X, _, _ = design(d, m["spec"])
    return d["tret"].values + X[:len(d)] @ m["coef"]


def x_table(m: dict) -> np.ndarray:
    K = len(KNOTS)
    return np.cumsum(m["coef"][:K])


def scores(pred: np.ndarray, d: pd.DataFrame) -> dict:
    e = pred - d["tfwd"].values
    return dict(n=int(len(e)), rmse=float(np.sqrt(np.mean(e ** 2))), bias=float(np.mean(e)),
                mae=float(np.mean(np.abs(e))), p95=float(np.percentile(np.abs(e), 95)),
                max=float(np.max(np.abs(e))))


# ---------------------------------------------------------------------------
def main() -> None:
    ctx = dict(generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), head=git_head())
    split = json.loads(SPLIT_PATH.read_text())
    gates = json.loads(GATES_PATH.read_text())
    ctx["gates_sha"] = sha256(GATES_PATH)
    S = load()
    blocks = pdx.split_blocks(split)
    fitm = pdx.blocks_mask(S["t"], blocks["FIT"])
    tunem = pdx.blocks_mask(S["t"], blocks["TUNE"])
    assert not (fitm & tunem).any(), "FIT/TUNE overlap"
    F_all = quasi_steady(S, fitm)
    T_all = quasi_steady(S, tunem)
    ctx["fault_days_fit"] = fault_days(F_all)
    ctx["fault_days_tune"] = fault_days(T_all)
    F = F_all[~F_all["day"].isin(ctx["fault_days_fit"])].copy()
    T = T_all[~T_all["day"].isin(ctx["fault_days_tune"])].copy()
    ctx["n_fit_all"], ctx["n_tune_all"] = len(F_all), len(T_all)
    ctx["n_fit"], ctx["n_tune"] = len(F), len(T)
    print("quasi-steady samples FIT", len(F), "TUNE", len(T))

    # support per knot interval (FIT)
    bins = [-0.01, 1, 10, 25, 40, 50, 52.5, 55, 57.5, 60, 65, 70, 80, 90, 100]
    sup = F.groupby(pd.cut(F["pos"], bins), observed=False).size()
    sup_t = T.groupby(pd.cut(T["pos"], bins), observed=False).size()
    ctx["support"] = [(str(i), int(a), int(b)) for i, a, b in zip(sup.index, sup.values, sup_t.values)]

    # CV on FIT: week parity
    even = (F["week"] % 2 == 0).values
    cv = {}
    for name, spec in MODELS.items():
        errs = []
        for tr, te in ((even, ~even), (~even, even)):
            m = fit(F[tr], spec)
            errs.append(predict(m, F[te]) - F[te]["tfwd"].values)
        e = np.concatenate(errs)
        cv[name] = dict(rmse=float(np.sqrt(np.mean(e ** 2))), mae=float(np.mean(np.abs(e))),
                        bias=float(np.mean(e)), p95=float(np.percentile(np.abs(e), 95)),
                        k=len(design(F.iloc[:5], spec)[2][0]))
        print("CV", name, cv[name])
    best = min(cv.values(), key=lambda v: v["rmse"])["rmse"]
    order = list(MODELS)  # listed from simplest to most complex
    chosen = next(n for n in order if cv[n]["rmse"] <= best + SELECT_TOL_C)
    ctx["cv"], ctx["chosen"] = cv, chosen
    print("chosen", chosen)

    final = fit(F, MODELS[chosen])
    ctx["final"] = final
    ctx["fit_scores"] = scores(predict(final, F), F)
    ctx["x_knots"] = x_table(final)
    extras = {n: float(c) for n, c in zip(final["names"], final["coef"]) if not n.startswith(("x0", "inc_"))}
    ctx["extras"] = extras
    ctx["downweighted"] = int(np.sum(final["weights"] < 1.0))

    if "--fit-only" in sys.argv:
        print("x_knots", np.round(ctx["x_knots"], 4).tolist(), "extras", extras,
              "fit", ctx["fit_scores"], "downweighted", ctx["downweighted"])
        print("support", ctx["support"])
        return
    # TUNE (untouched) -> gates
    pt = predict(final, T)
    ts = scores(pt, T)
    ctx["tune_scores"] = ts
    ctx["tune_scores_no_fault_rule"] = scores(predict(final, T_all), T_all)
    ctx["x_max_check"] = float(np.max(ctx["x_knots"]))
    g = gates["gates"]
    rows = []
    for key, actual in (("tune_steady_rmse_c", ts["rmse"]), ("tune_steady_abs_bias_c", abs(ts["bias"])),
                        ("tune_steady_p95_abs_error_c", ts["p95"])):
        thr = g[key]["threshold"]
        ok = actual < thr if g[key]["op"] == "<" else actual > thr
        margin = (thr - actual) / thr
        rows.append(dict(gate=key, op=g[key]["op"], threshold=thr, actual=actual,
                         result="PASS" if ok else "FAIL", margin_frac=margin,
                         marginal=abs(margin) < gates["marginal_margin_fraction"]))
    ctx["gate_rows"] = rows
    ctx["overall"] = all(r["result"] == "PASS" for r in rows)
    # per-band TUNE errors
    T = T.assign(pred=pt, err=pt - T["tfwd"].values)
    band = T.groupby(pd.cut(T["pos"], [-0.01, 1, 45, 50, 55, 60, 65, 100]), observed=True)["err"]
    ctx["tune_bands"] = [(str(k), int(v.size), float(np.sqrt(np.mean(v ** 2))), float(v.mean()))
                         for k, v in band]
    rb = T.groupby("rad")["err"]
    ctx["tune_rad"] = [(int(k), int(v.size), float(np.sqrt(np.mean(v ** 2))), float(v.mean())) for k, v in rb]
    mb = T.groupby(T["t"].dt.strftime("%Y-%m"))["err"]
    ctx["tune_month"] = [(k, int(v.size), float(np.sqrt(np.mean(v ** 2))), float(v.mean())) for k, v in mb]
    # closed-valve anomaly (x > 0.2 at pos <= 1) on FIT
    an = F[(F["pos"] <= 1) & (F["x"] > 0.2)]
    ctx["anomaly"] = dict(n=len(an), n_closed=int((F["pos"] <= 1).sum()),
                          median_x=float(an["x"].median()) if len(an) else np.nan,
                          bmi_median=float(an["bmi"].median()) if len(an) else np.nan)
    # position source check: BMI vs DR on FIT samples where the BMI moved recently? (diagnostic)
    result = dict(generated=ctx["generated"], gates_sha256=ctx["gates_sha"], chosen=chosen,
                  tune=ts, gates=rows, overall="PASS" if ctx["overall"] else "FAIL")
    RESULT_PATH.write_text(json.dumps(result, indent=2, default=float))
    plots(F, T, final, ctx)
    export(ctx)
    report(ctx)
    print("TUNE", ts, "->", "PASS" if ctx["overall"] else "FAIL")


def plots(F, T, final, ctx):
    fig, axs = plt.subplots(1, 2, figsize=(15, 5.5))
    ax = axs[0]
    for rad, c, lab in ((0, "#1f77b4", "FIT, floor only"), (1, "#ff7f0e", "FIT, floor + radiator")):
        s = F[F["rad"] == rad]
        ax.scatter(s["pos"], s["x"], s=6, alpha=0.35, color=c, label=lab)
    s = T
    ax.scatter(s["pos"], s["x"], s=6, alpha=0.25, color="grey", marker="x", label="TUNE (all)")
    pg = np.linspace(0, 100, 401)
    xs = np.interp(pg, KNOTS, ctx["x_knots"])
    ax.plot(pg, xs, "k-", lw=2, label="identified x(pos) (floor only)")
    if "rad_offset" in ctx["extras"]:
        ax.plot(pg, xs + ctx["extras"]["rad_offset"], "k--", lw=1.5, label="with radiator pump(s)")
    sup = {k: a for k, a, b in ctx["support"]}
    ax.set_xlabel("mixer_state position [%]"); ax.set_ylabel("hot fraction x = (T_fwd - T_ret)/(T_inc - T_ret)")
    ax.set_ylim(-0.05, 0.8); ax.grid(alpha=0.3); ax.legend(fontsize=8)
    ax.set_title("Static mixing law (quasi-steady samples)")
    ax = axs[1]
    ax.scatter(T["tfwd"], T["pred"], s=6, alpha=0.4)
    lo, hi = T["tfwd"].min() - 1, T["tfwd"].max() + 1
    ax.plot([lo, hi], [lo, hi], "k-", lw=1)
    ax.set_xlabel("measured T_forward [degC]"); ax.set_ylabel("predicted T_forward [degC]")
    ax.set_title("TUNE (untouched): predicted vs measured"); ax.grid(alpha=0.3)
    fig.tight_layout(); fig.savefig(PLOT_LAW, dpi=80); plt.close(fig)

    fig, axs = plt.subplots(1, 3, figsize=(16, 4.5))
    axs[0].scatter(T["pos"], T["err"], s=6, alpha=0.4); axs[0].set_xlabel("position [%]")
    axs[1].scatter(T["tinc"], T["err"], s=6, alpha=0.4); axs[1].set_xlabel("T_incoming [degC]")
    axs[2].plot(T["t"], T["err"], ".", ms=3, alpha=0.5); axs[2].set_xlabel("date")
    for a in axs:
        a.axhline(0, color="k", lw=0.8); a.set_ylabel("TUNE error (pred - meas) [degC]"); a.grid(alpha=0.3)
    fig.tight_layout(); fig.savefig(PLOT_RES, dpi=80); plt.close(fig)


def export(ctx):
    final = ctx["final"]
    sup = {k: a for k, a, b in ctx["support"]}
    out = dict(
        version=VERSION, generated=ctx["generated"],
        model="T_fwd_ss = T_ret + x(pos) * (T_inc - T_ret), x piecewise linear on knots"
              + (" + rad_offset * radiator_pump_on" if "rad_offset" in ctx["extras"] else "")
              + (" + drift_x_per_12h * min(h_since_reset, 12)/12" if "drift_x_per_12h" in ctx["extras"] else ""),
        parameters=dict(knots_pos_pct=KNOTS.tolist(), x_at_knots=[float(v) for v in ctx["x_knots"]],
                        **ctx["extras"]),
        parameter_units=dict(knots_pos_pct="% (mixer_state)", x_at_knots="-",
                             **{k: "-" for k in ctx["extras"]}),
        support_fit_samples_per_bin=sup,
        chosen_structure=ctx["chosen"], cv=ctx["cv"],
        fit=ctx["fit_scores"], tune=ctx["tune_scores"],
        acceptance=dict(gates=ctx["gate_rows"], overall="PASS" if ctx["overall"] else "FAIL",
                        gates_sha256=ctx["gates_sha"], test_set="TUNE blocks (untouched for T_forward)"),
        quasi_steady_definition=QS, report=str(REPORT_PATH.relative_to(SIM)),
    )
    PARAMS_PATH.write_text(json.dumps(out, indent=2, default=float))


def report(ctx):
    L = []
    A = L.append
    ts, fs = ctx["tune_scores"], ctx["fit_scores"]
    A("# Step 3 Report v1.0: 3-Way Mixer Static Mixing Characteristic")
    A("")
    A("**Project:** ESP32 floor-heating HIL simulator - parameter extraction  ")
    A(f"**Generated:** {ctx['generated']} by `simulation/step3/step3_mixer_static_v1.py`  ")
    A(f"**Git HEAD:** `{ctx['head']}`  ")
    A(f"**Gates:** `step3/gates_step3.json` (SHA-256 `{ctx['gates_sha']}`, frozen before any step-3 analysis)")
    A("")
    A("## 0. Summary")
    A("")
    A(f"**TUNE (untouched) result: {'PASS' if ctx['overall'] else 'FAIL'}**")
    A("")
    A(table(["Gate", "Threshold", "Actual", "Result", "Margin"],
            [[r["gate"], f"{r['op']} {r['threshold']}", fmt(r["actual"]), r["result"],
              f"{100 * r['margin_frac']:+.1f}%" + (" (marginal)" if r["marginal"] else "")] for r in ctx["gate_rows"]]))
    A("")
    A("The mixer is modelled as an ideal 3-way mixing point: the forward temperature is the "
      "flow-weighted mix of the incoming (hot, fork) water and the floor return water,")
    A("")
    A("```")
    A("T_fwd,ss = T_ret + x(pos) * (T_inc - T_ret)")
    A("```")
    A("")
    A(f"with the hot fraction x a monotone piecewise-linear function of the dead-reckoned position "
      f"(`mixer_state`).  Selected structure: **{ctx['chosen']}** (2-fold week-parity cross-validation "
      "on FIT; the simplest model within "
      f"{SELECT_TOL_C} degC of the best CV RMSE).")
    A("")
    A("## 1. Data and quasi-steady selection")
    A("")
    A("* Heating season 10 s grid (causal zero-order hold, ages per signal), Step 1 pump debounce.")
    A("* FIT and TUNE are the 12-hour blocks of `step2/holdout_v7.json`.  Step 2 used TUNE only for "
      "T_incoming structure checks; T_forward was never fitted or used for decisions on TUNE, so "
      "TUNE is an untouched test set for this step.")
    A(f"* Quasi-steady sample (QS): over the trailing {QS['window_s']} s the floor pump is on, the "
      f"mixer position varies by < {QS['pos_range_max']} %, no DHW, the radiator-pump state is "
      f"constant, T_inc and T_fwd ages <= {QS['max_age_s']:.0f} s, and mean(T_inc - T_ret) >= "
      f"{QS['min_lift_c']} degC.  Window means of T_inc, T_ret, T_fwd and position are used; one "
      f"sample per {QS['sample_every_s']} s.")
    A(f"* **Actuation/position-fault days:** a calendar day is excluded when any QS sample has "
      f"mixer_state <= 1 % (valve reported closed) together with x > {FAULT_X_CLOSED} - physically "
      "impossible for a closed valve (closed-valve QS samples are otherwise at x of a few "
      "hundredths).  Example (FIT, 2025-12-25): the legacy over-temperature slam drives the "
      "dead-reckoned position to 0 %, but T_forward stays near 38-40 degC for hours and the "
      "BMI160 alternates between two frozen values, i.e. the valve did not move.  No input-only "
      "signal identifies these days.  Excluded days - FIT: "
      f"{', '.join(ctx['fault_days_fit']) or 'none'}; TUNE: {', '.join(ctx['fault_days_tune']) or 'none'}.  "
      "The rule is applied identically to FIT and TUNE and was fixed (script commit) before "
      "TUNE was evaluated; TUNE results without the rule are reported in Section 3.")
    A(f"* Samples after the rule: FIT {ctx['n_fit']:,} (of {ctx['n_fit_all']:,}), "
      f"TUNE {ctx['n_tune']:,} (of {ctx['n_tune_all']:,}).")
    A("")
    A("Position support (QS samples per position bin):")
    A("")
    A(table(["mixer_state bin [%]", "FIT", "TUNE"], [[k, a, b] for k, a, b in ctx["support"]]))
    A("")
    A("The legacy PID parks the valve almost only at 0 % or around 50-60 %; the 1-45 % range is "
      "crossed only while moving (full stroke 145 s).  **x(pos) is data-supported only where the "
      "table above shows FIT samples**; between them it is a monotone linear interpolation, and it "
      "is refined with dynamic (moving-valve) data in Step 4.")
    A("")
    A("## 2. Identification")
    A("")
    A("x is parameterised on knots " + ", ".join(f"{k:g}" for k in KNOTS) + " % as x(0) plus "
      "non-negative increments (monotone non-decreasing), and fitted by bounded linear least "
      f"squares on T_fwd - T_ret = x * (T_inc - T_ret) with Huber re-weighting (c = {HUBER_C} degC) "
      f"and a light second-difference smoothness penalty (weight equivalent to {SMOOTH_EQ_SAMPLES:g} "
      "samples per 10 % of travel), so that ranges without data are bridged by a smooth, "
      "nearly linear curve instead of an arbitrary step.  "
      "Candidate extras: a radiator-pump offset on x (radiator pumps change the pressure at the "
      "fork) and a dead-reckoning drift term on x growing linearly over the 12 h after the nightly "
      "re-zeroing (Step 1 found mixer_state - BMI160 rising from about +0.9 % to +1.8 % after a reset).")
    A("")
    A(table(["Candidate", "Free parameters", "CV RMSE", "CV MAE", "CV bias", "Chosen"],
            [[n, v["k"], fmt(v["rmse"]), fmt(v["mae"]), fmt(v["bias"]), "yes" if n == ctx["chosen"] else ""]
             for n, v in ctx["cv"].items()]))
    A("")
    A("Identified law (final fit on all FIT samples):")
    A("")
    sup = {k: a for k, a, b in ctx["support"]}
    supk = []
    for k in KNOTS:
        near = F_support(k, ctx["support"])
        supk.append(near)
    A(table(["Knot position [%]", "x", "FIT QS samples within +/-2.5 %"],
            [[f"{k:g}", f"{x:.4f}", s] for k, x, s in zip(KNOTS, ctx["x_knots"], supk)]))
    A("")
    for k, v in ctx["extras"].items():
        A(f"* `{k}` = {v:+.4f}")
    A("")
    A(f"FIT in-sample: RMSE {fmt(fs['rmse'])}, MAE {fmt(fs['mae'])}, bias {fmt(fs['bias'])} degC; "
      f"{ctx['downweighted']} of {fs['n']} samples were down-weighted by the Huber step.")
    A("")
    xk = dict(zip(KNOTS, ctx["x_knots"]))
    A("Findings:")
    A("")
    A(f"* **Leak-by:** x(0) = {xk[0.0]:.3f} - a closed valve still passes about "
      f"{100 * xk[0.0]:.0f} % hot water (supported by {F_support(0.0, ctx['support'])} samples).")
    A(f"* **Operating band:** x rises from {xk[50.0]:.3f} at 50 % to {xk[60.0]:.3f} at 60 %, i.e. "
      f"about {(xk[60.0] - xk[50.0]) / 10:.4f} per % of travel.  A 2 % dead-reckoning error there "
      "shifts T_forward by about "
      f"{2 * (xk[60.0] - xk[50.0]) / 10 * 40:.1f} degC at a 40 degC lift.")
    A(f"* **Above 60 % the law is not identified.** The fitted curve stays near "
      f"{xk[60.0]:.2f}-{xk[90.0]:.2f}, but this is a compromise between conflicting sparse data: with a "
      "radiator pump on at the legacy 70 % clamp the hot fraction is about 0.2, while the few "
      "floor-only samples at 80-90 % (low T_incoming) reach 0.6-0.8 (see the figure).  The values "
      "above 60 % must not be used as a physical saturation; Step 4 re-examines this range with "
      "moving-valve data.")
    A("* **Mid-range (1-45 %):** no quasi-steady data; the curve is the smooth bridge between 0 % and "
      "50 % and is provisional until the dynamic identification in Step 4.")
    A("")
    A("![law](step3_mixing_law_v1.png)")
    A("")
    A("## 3. Test on TUNE (untouched)")
    A("")
    A(f"TUNE: n = {ts['n']:,}, RMSE {fmt(ts['rmse'])}, MAE {fmt(ts['mae'])}, bias {fmt(ts['bias'])}, "
      f"P95 |error| {fmt(ts['p95'])}, max |error| {fmt(ts['max'])} degC.")
    tn = ctx["tune_scores_no_fault_rule"]
    A("")
    A(f"Without the fault-day rule (all {tn['n']:,} TUNE QS samples): RMSE {fmt(tn['rmse'])}, "
      f"bias {fmt(tn['bias'])}, P95 {fmt(tn['p95'])}, max {fmt(tn['max'])} degC - "
      f"{'the gates would still pass' if (tn['rmse'] < 1.0 and abs(tn['bias']) < 0.3 and tn['p95'] < 2.0) else 'the gates would not all pass'}.  "
      "The gate result above uses the rule as frozen.")
    A("")
    A(table(["Position band [%]", "n", "RMSE", "Bias"], [[k, n, fmt(r), fmt(b)] for k, n, r, b in ctx["tune_bands"]]))
    A("")
    A(table(["Radiator pump", "n", "RMSE", "Bias"], [[k, n, fmt(r), fmt(b)] for k, n, r, b in ctx["tune_rad"]]))
    A("")
    A(table(["Month", "n", "RMSE", "Bias"], [[k, n, fmt(r), fmt(b)] for k, n, r, b in ctx["tune_month"]]))
    A("")
    A("![residuals](step3_residuals_v1.png)")
    A("")
    A("## 4. Use in the HIL simulator and limitations")
    A("")
    A("* The simulator evaluates x at the *physical* valve position.  The table is expressed in "
      "`mixer_state` (dead-reckoned) units; Step 1 found mixer_state = 0.9925 * BMI160 + 2.29 % "
      "when the accelerometer is not frozen, i.e. physical and dead-reckoned scales agree within "
      "about 2 %.")
    A("* Outside the supported bins the curve is a smooth monotone bridge and must be treated as "
      "provisional until Step 4.")
    rad = {k: (n, r, b) for k, n, r, b in ctx["tune_rad"]}
    if 1 in rad and 0 in rad:
        A(f"* **Radiator pumps:** TUNE RMSE is {fmt(rad[1][1])} degC with a radiator pump on versus "
          f"{fmt(rad[0][1])} degC without (bias {fmt(rad[1][2])} vs {fmt(rad[0][2])}).  The radiator "
          "offset on x did not pass the cross-validation selection rule, so the mixing law is "
          "regime-independent; the larger scatter with radiators on reflects pressure/flow "
          "interactions at the fork that a static law does not capture.")
    A("* The legacy controller's fault days show that dead-reckoned position can be grossly wrong "
      "after an over-temperature slam; the new controller's encoder consistency check addresses "
      "this.  In the HIL the actuator position is known exactly.")
    A("* x is a steady-state quantity.  Transport delays, actuator travel and sensor lag are Step 4.")
    A("")
    A("## 5. Reproduction")
    A("")
    A("```bash")
    A("cd /home/gume/claude/HeatpumpsN && .venv/bin/python simulation/step3/step3_mixer_static_v1.py")
    A("```")
    REPORT_PATH.write_text("\n".join(L) + "\n")


def F_support(k, support):
    """FIT samples whose bin overlaps [k-2.5, k+2.5] (approximate, from the bin table)."""
    tot = 0
    for lab, a, b in support:
        lo, hi = [float(v) for v in lab.strip("(]").split(",")]
        if hi >= k - 2.5 and lo <= k + 2.5:
            tot += a
    return tot


if __name__ == "__main__":
    main()
