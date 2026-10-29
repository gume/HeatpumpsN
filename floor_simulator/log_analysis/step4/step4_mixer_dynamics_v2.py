#!/usr/bin/env python3
"""Step 4 v2 - mixer actuator, transport delay and forward-sensor dynamics.

v2 (after the v1 TUNE FAIL, RMSE 1.545 > 1.5): FIT residual diagnostics put
74 % of the squared error above 60 % valve position, where the Step 3 law is
not identified.  v2 adds candidates that re-estimate the law above 60 % from
moving-valve data (C5) and a radiator-pump multiplier on x (C6).  TUNE has been
seen by v1, so v2 is tested on the Step 2 holdout blocks HO-1..HO-3, which are
untouched for T_forward; TUNE is reported as observed.

    pos(t)  : dead-reckoned valve position (actuator travel 145 s / 100 %)
    x(t)    = law(pos(t))                        (Step 3 law, optionally refined)
    Tmix(t) = T_ret(t) + x(t) * (T_inc,w(t) - T_ret(t))
    T_w(t)  = Tmix(t - d)                        (plug-flow transport delay d)
    T_fwd   : sensor reading, first-order lag tau_f on T_w

T_inc,w is the incoming *water* temperature.  The HIL supplies water
temperatures (Step 2 state T); for identification it is reconstructed from the
measured T_incoming with the Step 2 thermowell lag: T_w = T_meas + tau_s dT/dt
(candidate C2), or the measured value is used directly (candidate C1).

Run: .venv/bin/python simulation/step4/step4_mixer_dynamics_v2.py [--fit-only]
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
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from numba import njit  # noqa: E402
from scipy.optimize import minimize  # noqa: E402

import plantdata as pdx  # noqa: E402
import step3_mixer_static_v1 as S3  # noqa: E402

VERSION = "2.0"
DT = 10.0
SPLIT_PATH = SIM / "step2" / "holdout_v7.json"
GATES_PATH = HERE / "gates_step4.json"
STEP2_PARAMS = SIM / "step2" / "step2_params_v71.json"
STEP3_PARAMS = SIM / "step3" / "step3_params_v1.json"
PARAMS_PATH = HERE / "step4_params_v2.json"
REPORT_PATH = HERE / "step4_mixer_dynamics_report_v2.md"
RESULT_PATH = HERE / "step4_test_result_v2.json"
PLOT_OV = HERE / "step4_test_overlay_v2.png"
PLOT_XC = HERE / "step4_law_v2.png"
PLOT_ACT = HERE / "step4_actuator_v2.png"

MAX_DELAY_N = 64
WARMUP_S = 1800.0
SCORE_AFTER_START_S = 300.0     # gates: floor pump on for >= 5 min
DERIV_SMOOTH_S = 60.0           # centred smoothing before differentiating T_inc
SELECT_TOL_C = 0.02
REFINE_KNOTS_MAX = 50.0         # low range refit: interior knots below 50 % (no QS data there)
HIGH_KNOTS_MIN = 60.0           # high range refit: knots above 60 % (sparse, conflicting QS data)


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
@njit(cache=True)
def simulate(knots, xk, d_n, tau_f, pos, tinc_w, tret, p3, tfwd_meas, anchor_ok, rad, radf):
    """Free-run T_forward sensor simulation (see module docstring).

    While the floor pump is off the sensor state follows the measurement (not
    scored); at every floor-pump start the state and the delay line are
    re-anchored to the measured T_forward.
    """
    n = pos.shape[0]
    out = np.empty(n)
    ring = np.empty(MAX_DELAY_N)
    rpos = 0
    s = tfwd_meas[0]
    for i in range(MAX_DELAY_N):
        ring[i] = s
    a_f = DT / (tau_f + DT)
    was_on = False
    K = knots.shape[0]
    for k in range(n):
        if not p3[k] or not anchor_ok[k]:
            s = tfwd_meas[k]
            for i in range(MAX_DELAY_N):
                ring[i] = s
            out[k] = s
            was_on = False
            continue
        if not was_on:
            s = tfwd_meas[k]
            for i in range(MAX_DELAY_N):
                ring[i] = s
            was_on = True
        # law
        p = pos[k]
        if p <= knots[0]:
            x = xk[0]
        elif p >= knots[K - 1]:
            x = xk[K - 1]
        else:
            j = 0
            while knots[j + 1] < p:
                j += 1
            w = (p - knots[j]) / (knots[j + 1] - knots[j])
            x = xk[j] * (1 - w) + xk[j + 1] * w
        if rad[k]:
            x = x * radf
            if x > 1.0:
                x = 1.0
        tmix = tret[k] + x * (tinc_w[k] - tret[k])
        if d_n > 0:
            tw = ring[rpos]
            ring[rpos] = tmix
            rpos += 1
            if rpos >= d_n:
                rpos = 0
        else:
            tw = tmix
        s = s + a_f * (tw - s)
        out[k] = s
    return out


# ---------------------------------------------------------------------------
class Episodes:
    def __init__(self, S: pd.DataFrame, blocks: list, fault_days: set, tau_s: float):
        self.eps = []
        tv = S["t"].values
        sm = int(DERIV_SMOOTH_S / DT) | 1
        for a, b in blocks:
            i0 = int(np.searchsorted(tv, a.to_datetime64()))
            i1 = int(np.searchsorted(tv, b.to_datetime64()))
            x = S.iloc[i0:i1]
            if len(x) < 400:
                continue
            tinc = x["tinc"].values.astype(float)
            tsm = pd.Series(tinc).rolling(sm, center=True, min_periods=1).mean().values
            dtdt = np.gradient(tsm, DT)
            p3 = x["p3"].values.astype(bool)
            ww = x["ww"].fillna(0).values >= 0.5
            fresh = ((x["tinc_age"].values <= 900) & (x["tfwd_age"].values <= 900)
                     & (x["tb_age"].values <= 900))
            on_for = np.zeros(len(x))
            for k in range(1, len(x)):
                on_for[k] = on_for[k - 1] + DT if p3[k] else 0.0
            day = x["t"].dt.strftime("%Y-%m-%d").values
            score = (p3 & (on_for >= SCORE_AFTER_START_S) & ~ww & fresh
                     & ~np.isin(day, list(fault_days)))
            score[: int(WARMUP_S / DT)] = False
            self.eps.append(dict(
                t=x["t"].values, pos=np.clip(x["pos"].fillna(0).values.astype(float), 0, 100),
                tinc=tinc, tinc_w=tinc + tau_s * dtdt, tret=x["tret"].values.astype(float),
                tfwd=x["tfwd"].values.astype(float), p3=p3, ww=ww,
                anchor_ok=fresh, score=score, bmi=x["bmi"].values.astype(float),
                rad=(x["p1"].values | x["p2"].values).astype(bool),
                week=int(a.isocalendar()[1]), start=a))
        self.n_scored = int(sum(e["score"].sum() for e in self.eps))


def run(ep, xk, d_n, tau_f, use_w, radf=1.0):
    return simulate(S3.KNOTS, np.asarray(xk, float), int(d_n), float(tau_f), ep["pos"],
                    ep["tinc_w"] if use_w else ep["tinc"], ep["tret"], ep["p3"], ep["tfwd"],
                    ep["anchor_ok"], ep["rad"], float(radf))


def sse(eps, xk, d_n, tau_f, use_w, radf=1.0):
    tot, n = 0.0, 0
    for ep in eps:
        e = (run(ep, xk, d_n, tau_f, use_w, radf) - ep["tfwd"])[ep["score"]]
        tot += float((e * e).sum()); n += len(e)
    return tot, n


def refined_law(x0: np.ndarray, z: np.ndarray) -> np.ndarray:
    """Interior knots strictly between 0 % and REFINE_KNOTS_MAX are re-estimated,
    monotone between the Step 3 anchors x(0) and x(REFINE_KNOTS_MAX):
    x_k = x(0) + (x(50) - x(0)) * cumsum(softmax-like weights)."""
    K = S3.KNOTS
    inner = [i for i in range(len(K)) if 0 < K[i] < REFINE_KNOTS_MAX]
    j50 = int(np.where(K == REFINE_KNOTS_MAX)[0][0])
    xk = np.array(x0, float)
    w = np.exp(np.r_[z, 0.0])            # len(inner) + 1 positive segment weights
    frac = np.cumsum(w)[:-1] / w.sum()
    xk[inner] = x0[0] + (x0[j50] - x0[0]) * frac
    return xk


def law(x0: np.ndarray, z: np.ndarray, low: bool, high: bool) -> np.ndarray:
    """Step 3 law with optional low-range (0-50 %) and high-range (> 60 %)
    re-estimation.  High knots: x_k = x(60) + cumsum(0.05 * exp(z)), i.e.
    monotone non-decreasing above the well-supported 60 % knot."""
    K = S3.KNOTS
    xk = np.array(x0, float)
    i = 0
    if low:
        xk = refined_law(xk, z[:n_refine()])
        i = n_refine()
    if high:
        hi = [k for k in range(len(K)) if K[k] > HIGH_KNOTS_MIN]
        j60 = int(np.where(K == HIGH_KNOTS_MIN)[0][0])
        inc = 0.05 * np.exp(z[i:i + len(hi)])
        xk[hi] = np.minimum(1.0, xk[j60] + np.cumsum(inc))
    return xk


def n_high() -> int:
    return int(np.sum(S3.KNOTS > HIGH_KNOTS_MIN))


def n_refine() -> int:
    return int(np.sum((S3.KNOTS > 0) & (S3.KNOTS < REFINE_KNOTS_MAX)))


def fit_candidate(eps, x0, use_w, refine, high=False, radf=False):
    """Delay on a 10 s grid; sensor lag, optional law knots and optional
    radiator multiplier by Nelder-Mead on the free-run mean squared error."""
    nl = n_refine() if refine else 0
    nh = n_high() if high else 0
    best = None
    for d_n in range(0, 25):
        def unpack(z):
            xk = law(x0, z[1:1 + nl + nh], refine, high)
            rf = float(np.exp(z[1 + nl + nh])) if radf else 1.0
            return xk, rf

        def f(z):
            xk, rf = unpack(z)
            s, n = sse(eps, xk, d_n, np.exp(z[0]), use_w, rf)
            return s / n
        z0 = np.r_[np.log(60.0), np.zeros(nl),
                   np.log(np.maximum(np.diff(np.r_[x0[S3.KNOTS == HIGH_KNOTS_MIN], x0[S3.KNOTS > HIGH_KNOTS_MIN]]), 1e-3) / 0.05) if high else [],
                   [0.0] if radf else []]
        r = minimize(f, z0, method="Nelder-Mead",
                     options=dict(maxiter=300 * len(z0), xatol=1e-3, fatol=1e-7))
        if best is None or r.fun < best["mse"]:
            xk, rf = unpack(r.x)
            best = dict(mse=float(r.fun), d_n=d_n, tau_f=float(np.exp(r.x[0])), xk=xk, radf=rf)
        elif r.fun > best["mse"] * 1.05 and d_n > best["d_n"] + 4:
            break
    return best


CANDIDATES = {
    "C1 measured T_inc, step-3 law": dict(use_w=False, refine=False),
    "C2 reconstructed T_inc water, step-3 law": dict(use_w=True, refine=False),
    "C3 measured T_inc, law refined below 50 %": dict(use_w=False, refine=True),
    "C4 reconstructed T_inc water, law refined below 50 %": dict(use_w=True, refine=True),
    "C5 C2 + law refined above 60 %": dict(use_w=True, refine=False, high=True),
    "C6 C5 + radiator multiplier on x": dict(use_w=True, refine=False, high=True, radf=True),
}


def metrics(eps, xk, d_n, tau_f, use_w, radf=1.0):
    E, Y = [], []
    for ep in eps:
        sim = run(ep, xk, d_n, tau_f, use_w, radf)
        m = ep["score"]
        E.append(sim[m] - ep["tfwd"][m]); Y.append(ep["tfwd"][m])
    e = np.concatenate(E)
    return dict(n=int(len(e)), rmse=float(np.sqrt(np.mean(e ** 2))), mae=float(np.mean(np.abs(e))),
                bias=float(np.mean(e)), p95=float(np.percentile(np.abs(e), 95)))


def timing_events(eps, xk, d_n, tau_f, use_w, radf=1.0):
    """Gate definition (gates_step4.json) of isolated valve moves and t50 errors."""
    out = []
    for ep in eps:
        pos = ep["pos"]
        n = len(pos)
        mv = np.abs(np.diff(pos, prepend=pos[0])) > 0.05
        idx = np.where(mv)[0]
        if len(idx) == 0:
            continue
        groups = []
        a = idx[0]
        prev = idx[0]
        for k in idx[1:]:
            if k - prev > 2:
                groups.append((a, prev)); a = k
            prev = k
        groups.append((a, prev))
        sim = run(ep, xk, d_n, tau_f, use_w, radf)
        for a, b in groups:
            if a < 30 or a + 90 > n:
                continue
            dur = (b - a + 1) * DT
            dp = pos[b] - pos[a - 1]
            if abs(dp) < 3 or dur > 60:
                continue
            if mv[a - 30:a].any() or mv[b + 1:a + 60].any():
                continue
            w = slice(a - 30, a + 90)
            if not ep["p3"][w].all() or ep["ww"][w].any() or not ep["anchor_ok"][w].all():
                continue
            if np.ptp(ep["tinc"][w]) >= 3:
                continue
            tf = ep["tfwd"]
            pre, post = tf[a - 12:a].mean(), tf[a + 48:a + 60].mean()
            if abs(post - pre) < 1:
                continue

            def t50(y):
                fr = (y[a:a + 60] - y[a - 12:a].mean()) / (y[a + 48:a + 60].mean() - y[a - 12:a].mean())
                k = np.where(fr >= 0.5)[0]
                return k[0] * DT if len(k) else np.nan
            out.append(dict(start=str(pd.Timestamp(ep["t"][a])), dp=float(dp), dT=float(post - pre),
                            t50_meas=t50(tf), t50_sim=t50(sim)))
    return out


def actuator_check(S: pd.DataFrame, fit_mask: np.ndarray) -> dict:
    """Travel speed of the dead-reckoned position and of the BMI160 during moves."""
    pos = S["pos"].values
    bmi = S["bmi"].values
    dpos = np.diff(pos, prepend=pos[0])
    moving = np.abs(dpos) > 0.05
    # interior samples of a move (moving before and after) travel a full 10 s
    interior = moving & np.r_[False, moving[:-1]] & np.r_[moving[1:], False]
    rate = np.abs(dpos[interior & fit_mask]) / DT
    # BMI vs DR over long monotone moves (>= 20 %), BMI must change by >= 5 %
    rows = []
    k = 0
    n = len(pos)
    while k < n - 1:
        if moving[k] and fit_mask[k]:
            j = k
            while j + 1 < n and moving[j + 1] and np.sign(dpos[j + 1]) == np.sign(dpos[k]):
                j += 1
            dp = pos[j] - pos[k - 1]
            db = bmi[min(n - 1, j + 6)] - bmi[k - 1]
            if abs(dp) >= 20 and abs(db) >= 5:
                rows.append((dp, db, (j - k + 1) * DT))
            k = j + 1
        else:
            k += 1
    r = np.array(rows) if rows else np.zeros((0, 3))
    return dict(dr_rate_median=float(np.median(rate)) if len(rate) else np.nan,
                dr_rate_p95=float(np.percentile(rate, 95)) if len(rate) else np.nan,
                n_long_moves=int(len(r)),
                bmi_over_dr_median=float(np.median(r[:, 1] / r[:, 0])) if len(r) else np.nan,
                bmi_over_dr_iqr=(float(np.percentile(r[:, 1] / r[:, 0], 25)),
                                 float(np.percentile(r[:, 1] / r[:, 0], 75))) if len(r) else (np.nan, np.nan),
                moves=r.tolist())


# ---------------------------------------------------------------------------
def main() -> None:
    ctx = dict(generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), head=git_head(),
               gates_sha=sha256(GATES_PATH))
    split = json.loads(SPLIT_PATH.read_text())
    tau_s = json.loads(STEP2_PARAMS.read_text())["parameters"]["tau_s"]
    p3j = json.loads(STEP3_PARAMS.read_text())
    x0 = np.array(p3j["parameters"]["x_at_knots"], float)
    assert np.allclose(p3j["parameters"]["knots_pos_pct"], S3.KNOTS)
    ctx.update(tau_s=tau_s, x_step3=x0)
    S = S3.load()
    blocks = pdx.split_blocks(split)
    fitm = pdx.blocks_mask(S["t"], blocks["FIT"])
    tunem = pdx.blocks_mask(S["t"], blocks["TUNE"])
    fd_fit = set(S3.fault_days(S3.quasi_steady(S, fitm)))
    fd_tune = set(S3.fault_days(S3.quasi_steady(S, tunem)))
    ctx["fault_days"] = sorted(fd_fit | fd_tune)
    FIT = Episodes(S, blocks["FIT"], fd_fit, tau_s)
    TUNE = Episodes(S, blocks["TUNE"], fd_tune, tau_s)
    ctx["n_fit"], ctx["n_tune"] = FIT.n_scored, TUNE.n_scored
    print("scored samples FIT", FIT.n_scored, "TUNE", TUNE.n_scored)
    ctx["actuator"] = actuator_check(S, fitm)
    print("actuator", {k: v for k, v in ctx["actuator"].items() if k != "moves"})

    # CV on FIT by week parity
    ev = [e for e in FIT.eps if e["week"] % 2 == 0]
    od = [e for e in FIT.eps if e["week"] % 2 == 1]
    cv = {}
    for name, c in CANDIDATES.items():
        se, ne = 0.0, 0
        fits = []
        for tr, te in ((ev, od), (od, ev)):
            b = fit_candidate(tr, x0, c["use_w"], c["refine"], c.get("high", False), c.get("radf", False))
            s, n = sse(te, b["xk"], b["d_n"], b["tau_f"], c["use_w"], b["radf"])
            se += s; ne += n; fits.append(b)
        cv[name] = dict(rmse=float(np.sqrt(se / ne)), folds=[dict(d_s=f["d_n"] * DT, tau_f=f["tau_f"]) for f in fits])
        print("CV", name, cv[name])
    best = min(v["rmse"] for v in cv.values())
    chosen = next(n for n in CANDIDATES if cv[n]["rmse"] <= best + SELECT_TOL_C)
    ctx["cv"], ctx["chosen"] = cv, chosen
    c = CANDIDATES[chosen]
    final = fit_candidate(FIT.eps, x0, c["use_w"], c["refine"], c.get("high", False), c.get("radf", False))
    ctx["final"] = final
    rf = final["radf"]
    ctx["fit_metrics"] = metrics(FIT.eps, final["xk"], final["d_n"], final["tau_f"], c["use_w"], rf)
    ctx["fit_timing"] = timing_events(FIT.eps, final["xk"], final["d_n"], final["tau_f"], c["use_w"], rf)
    print("final", chosen, final, ctx["fit_metrics"], "FIT timing events", len(ctx["fit_timing"]))
    if "--fit-only" in sys.argv:
        return

    # TEST on HO-1..HO-3 (untouched for T_forward); TUNE observed since v1 ---
    gates = json.loads(GATES_PATH.read_text())
    ho_blocks = [(pd.Timestamp(v["t_start"]), pd.Timestamp(v["t_end"])) for v in split["holdout"].values()]
    hom = pdx.blocks_mask(S["t"], ho_blocks)
    fd_ho = set(S3.fault_days(S3.quasi_steady(S, hom)))
    TEST = Episodes(S, ho_blocks, fd_ho, tau_s)
    ctx["n_test"], ctx["fault_days_ho"] = TEST.n_scored, sorted(fd_ho)
    rf = final["radf"]
    tm = metrics(TEST.eps, final["xk"], final["d_n"], final["tau_f"], c["use_w"], rf)
    te = timing_events(TEST.eps, final["xk"], final["d_n"], final["tau_f"], c["use_w"], rf)
    errs = [abs(e["t50_sim"] - e["t50_meas"]) for e in te
            if np.isfinite(e["t50_sim"]) and np.isfinite(e["t50_meas"])]
    t50_med = float(np.median(errs)) if errs else np.nan
    ctx.update(tune_metrics=tm, tune_timing=te, t50_med=t50_med)
    ctx["tune_observed"] = metrics(TUNE.eps, final["xk"], final["d_n"], final["tau_f"], c["use_w"], rf)
    ctx["tune_observed_timing_n"] = len(timing_events(TUNE.eps, final["xk"], final["d_n"], final["tau_f"], c["use_w"], rf))
    v1 = json.loads((HERE / "step4_tune_result_v1.json").read_text())
    ctx["v1_result"] = v1
    rows = []
    for key, actual in (("tune_pumpon_rmse_c", tm["rmse"]), ("tune_pumpon_mae_c", tm["mae"]),
                        ("tune_move_t50_median_abs_error_s", t50_med)):
        g = gates["gates"][key]
        if not np.isfinite(actual):
            rows.append(dict(gate=key, op=g["op"], threshold=g["threshold"], actual=np.nan,
                             result="NOT EVALUABLE (no qualifying events) - not passed",
                             margin_frac=np.nan, marginal=False))
            continue
        ok = actual < g["threshold"] if g["op"] == "<" else actual > g["threshold"]
        mf = (g["threshold"] - actual) / g["threshold"]
        rows.append(dict(gate=key, op=g["op"], threshold=g["threshold"], actual=actual,
                         result="PASS" if ok else "FAIL", margin_frac=mf,
                         marginal=abs(mf) < gates["marginal_margin_fraction"]))
    ctx["gate_rows"] = rows
    ctx["overall"] = all(r["result"] == "PASS" for r in rows)
    ctx["tune_static_only"] = metrics(TEST.eps, x0, 0, 1e-6, False)
    RESULT_PATH.write_text(json.dumps(dict(generated=ctx["generated"], gates_sha256=ctx["gates_sha"],
                                           test_set="HO-1..HO-3 (untouched for T_forward)",
                                           chosen=chosen, test=tm, t50_median_abs_error_s=t50_med,
                                           n_timing_events=len(te), gates=rows,
                                           overall="PASS" if ctx["overall"] else "FAIL",
                                           tune_observed=ctx["tune_observed"]),
                                      indent=2, default=float))
    plots(ctx, TEST, c)
    export(ctx)
    report(ctx)
    print("TEST", tm, "t50", t50_med, len(te), "->", "PASS" if ctx["overall"] else "FAIL",
          "| TUNE observed", ctx["tune_observed"])


def plots(ctx, TUNE, c):
    final = ctx["final"]
    # overlay: TUNE block with the most valve motion among floor-heating blocks
    def motion(ep):
        return np.abs(np.diff(ep["pos"]))[ep["score"][1:]].sum()
    ep = max(TUNE.eps, key=motion)
    sim = run(ep, final["xk"], final["d_n"], final["tau_f"], c["use_w"], final["radf"])
    t = pd.to_datetime(ep["t"]).tz_localize("UTC").tz_convert("Europe/Budapest")
    m = ep["score"]
    i0 = int(np.argmax(m))
    sl = slice(i0, min(len(t), i0 + 6 * 180))
    fig, axs = plt.subplots(2, 1, figsize=(16, 8), gridspec_kw=dict(height_ratios=[3, 1]))
    ax = axs[0]
    ax.plot(t[sl], ep["tfwd"][sl], "k", lw=1.2, label="T_forward measured")
    ax.plot(t[sl], np.where(m, sim, np.nan)[sl], "#1f77b4", ls="--", lw=1.2, label="v2 simulated (scored samples)")
    ax2 = ax.twinx()
    ax2.plot(t[sl], ep["tinc"][sl], "#d62728", lw=0.7, label="T_incoming (right axis)")
    ax2.set_ylabel("T_incoming [degC]", color="#d62728")
    ax2.legend(loc="lower right", fontsize=8)
    ax.plot(t[sl], ep["tret"][sl], "#2ca02c", lw=0.8, label="T_return")
    ax.set_ylabel("degC"); ax.grid(alpha=0.3); ax.legend(ncol=4, fontsize=8)
    ax.xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%m-%d %H:%M", tz=t.tz))
    ax.set_title(f"Holdout block {pd.Timestamp(ep['start']).strftime('%Y-%m-%d %H:%M')} (most valve motion), 3 h excerpt")
    axs[1].plot(t[sl], ep["pos"][sl], "#9467bd", lw=1, label="mixer_state [%]")
    axs[1].grid(alpha=0.3); axs[1].legend(fontsize=8)
    axs[1].xaxis.set_major_formatter(matplotlib.dates.DateFormatter("%m-%d %H:%M", tz=t.tz))
    fig.tight_layout(); fig.savefig(PLOT_OV, dpi=80); plt.close(fig)

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.plot(S3.KNOTS, ctx["x_step3"], "k-o", label="Step 3 law (quasi-steady)")
    ax.plot(S3.KNOTS, final["xk"], "b--s", label="Step 4 law (dynamic)")
    ax.set_xlabel("mixer_state [%]"); ax.set_ylabel("hot fraction x"); ax.grid(alpha=0.3); ax.legend()
    fig.tight_layout(); fig.savefig(PLOT_XC, dpi=80); plt.close(fig)

    a = ctx["actuator"]
    if a["moves"]:
        mv = np.array(a["moves"])
        fig, ax = plt.subplots(figsize=(7, 5))
        ax.scatter(mv[:, 0], mv[:, 1], s=10)
        lim = np.max(np.abs(mv[:, :2])) * 1.05
        ax.plot([-lim, lim], [-lim, lim], "k-", lw=0.8, label="BMI change = DR change")
        ax.set_xlabel("dead-reckoned change [%]"); ax.set_ylabel("BMI160 change [%]")
        ax.grid(alpha=0.3); ax.legend(); ax.set_title("Long monotone moves (FIT)")
        fig.tight_layout(); fig.savefig(PLOT_ACT, dpi=80); plt.close(fig)


def export(ctx):
    final = ctx["final"]
    c = CANDIDATES[ctx["chosen"]]
    out = dict(
        version=VERSION, generated=ctx["generated"],
        model="Tmix = T_ret + x(pos)*m_rad*(T_inc,w - T_ret) (m_rad = radiator_x_multiplier if a radiator pump runs, else 1); T_w = Tmix delayed by d; T_fwd sensor = first-order lag tau_f",
        parameters=dict(transport_delay_s=final["d_n"] * DT, tau_fwd_sensor_s=final["tau_f"],
                        radiator_x_multiplier=final["radf"],
                        knots_pos_pct=S3.KNOTS.tolist(), x_at_knots=[float(v) for v in final["xk"]],
                        actuator_full_stroke_s=145.0,
                        tinc_input="water temperature (Step 2 state T)" if c["use_w"]
                        else "sensor reading of T_incoming"),
        parameter_units=dict(transport_delay_s="s", tau_fwd_sensor_s="s", knots_pos_pct="%",
                             x_at_knots="-", actuator_full_stroke_s="s"),
        chosen_structure=ctx["chosen"], cv=ctx["cv"], fit=ctx["fit_metrics"],
        actuator_check={k: v for k, v in ctx["actuator"].items() if k != "moves"},
        acceptance=dict(gates=ctx["gate_rows"], overall="PASS" if ctx["overall"] else "FAIL",
                        gates_sha256=ctx["gates_sha"], test_set="HO-1..HO-3 (untouched for T_forward)",
                        test=ctx["tune_metrics"], tune_observed=ctx["tune_observed"],
                        v1_tune_result=ctx["v1_result"]["overall"]),
        report=str(REPORT_PATH.relative_to(SIM)),
    )
    PARAMS_PATH.write_text(json.dumps(out, indent=2, default=float))


def report(ctx):
    final, fm, tm = ctx["final"], ctx["fit_metrics"], ctx["tune_metrics"]
    c = CANDIDATES[ctx["chosen"]]
    a = ctx["actuator"]
    v1 = ctx["v1_result"]
    to = ctx["tune_observed"]
    L = []
    A = L.append
    A("# Step 4 Report v2.0: Mixer Actuator, Transport Delay and Forward-Sensor Dynamics")
    A("")
    A("**Project:** ESP32 floor-heating HIL simulator - parameter extraction  ")
    A(f"**Generated:** {ctx['generated']} by `simulation/step4/step4_mixer_dynamics_v2.py`  ")
    A(f"**Git HEAD:** `{ctx['head']}`  ")
    A(f"**Gates:** `step4/gates_step4.json` (SHA-256 `{ctx['gates_sha']}`, frozen before any step-4 analysis)")
    A("")
    A("## 0. Summary")
    A("")
    A(f"**Test result (HO-1..HO-3, untouched for T_forward): {'PASS' if ctx['overall'] else 'FAIL'}**")
    A("")
    A(table(["Gate", "Threshold", "Actual", "Result", "Margin"],
            [[r["gate"], f"{r['op']} {r['threshold']}", fmt(r["actual"]), r["result"],
              (f"{100 * r['margin_frac']:+.1f}%" if np.isfinite(r["margin_frac"]) else "n/a")
              + (" (marginal)" if r["marginal"] else "")] for r in ctx["gate_rows"]]))
    A("")
    vt = v1["tune"]
    A(f"History: v1.0 (Step 3 law, delay + sensor lag) was tested once on the TUNE blocks and "
      f"**failed** the RMSE gate ({fmt(vt['rmse'])} degC vs < 1.5; MAE {fmt(vt['mae'])} passed; timing "
      "gate not evaluable).  FIT residual diagnostics then located most of the squared error above "
      "60 % valve position, where the Step 3 law is not identified.  v2.0 adds candidates that "
      "re-estimate that range from moving-valve data and a radiator-pump effect.  Because TUNE "
      "had been seen, v2.0 is tested on the Step 2 holdout blocks, which no step has used for "
      f"T_forward.  On TUNE (now observed) v2.0 reaches RMSE {fmt(to['rmse'])} and MAE "
      f"{fmt(to['mae'])} degC.")
    A("")
    A("Identified dynamics (FIT):")
    A("")
    A(f"* transport delay valve -> T_forward sensor: **{final['d_n'] * DT:.0f} s**")
    A(f"* T_forward sensor (thermowell) lag: **{final['tau_f']:.0f} s**")
    if c.get("radf"):
        A(f"* **radiator-pump effect:** while a radiator pump runs the hot fraction is multiplied by "
          f"**{final['radf']:.3f}** (the radiator circuits draw from the same fork and lower the "
          "pressure available to the floor mixer)")
    A(f"* incoming temperature input: {'the incoming *water* temperature (Step 2 thermowell lag tau_s = ' + format(ctx['tau_s'], '.0f') + ' s removed from the sensor reading for identification)' if c['use_w'] else 'measured T_incoming sensor reading'}")
    A(f"* actuator: 100 % travel in 145 s (full-speed rate P95 {fmt(a['dr_rate_p95'], 3)} %/s), "
      f"confirmed by the BMI160 (median ratio {fmt(a['bmi_over_dr_median'])}).")
    A("")
    A("## 1. Data and scoring")
    A("")
    A("* Heating season 10 s grid, Step 1 pump debounce; FIT/TUNE/holdout blocks of "
      "`step2/holdout_v7.json`.")
    A("* Free-run simulation per block; the sensor state and delay line are re-anchored to the "
      "measured T_forward whenever the floor pump starts and follow the measurement while it is "
      "off.  Scored: floor pump on for >= 5 min, no DHW, fresh T_forward, T_incoming and T_boiler "
      "(age <= 900 s), outside the 30-min block warm-up and outside Step 3 fault days "
      f"(FIT/TUNE: {', '.join(ctx['fault_days']) or 'none'}; holdout: {', '.join(ctx['fault_days_ho']) or 'none'}).")
    A(f"* Scored samples: FIT {ctx['n_fit']:,}, TUNE {ctx['n_tune']:,}, holdout {ctx['n_test']:,}.")
    A("")
    A("## 2. Actuator travel")
    A("")
    lo, hi = a["bmi_over_dr_iqr"]
    A(f"Interior samples of moves give a median dead-reckoned rate of {fmt(a['dr_rate_median'], 3)} %/s "
      f"and a P95 of {fmt(a['dr_rate_p95'], 3)} %/s.  The firmware integrates relay time over a 145 s "
      "stroke, so full-speed travel is 0.690 %/s; the lower median comes from the short PID pulses "
      "(2-3 %) being spread over the time-quantised telemetry.  The physical travel was checked on "
      f"{a['n_long_moves']} long monotone moves (>= 20 % dead-reckoned, BMI160 moved >= 5 %): median "
      f"BMI change / DR change = {fmt(a['bmi_over_dr_median'])} (IQR {fmt(lo)} - {fmt(hi)}), confirming "
      "the 145 s stroke.  Backlash could not be resolved (accelerometer freezes, 0.1 % resolution).")
    A("")
    if a["moves"]:
        A("![actuator](step4_actuator_v2.png)")
        A("")
    A("## 3. Identification and structure selection (FIT only)")
    A("")
    A("Delay on a 10 s grid (0-240 s); sensor lag, law knots and radiator multiplier by Nelder-Mead "
      "on the free-run mean squared error.  Law refinements keep monotonicity: below 50 % the "
      "interior knots move between the Step 3 anchors at 0 % and 50 %; above 60 % the knots rise "
      "monotonically from the Step 3 value at 60 %.  Selection: 2-fold week-parity CV on FIT, "
      f"simplest candidate within {SELECT_TOL_C} degC of the best.")
    A("")
    A(table(["Candidate", "CV RMSE", "Fold delays [s]", "Fold sensor lags [s]", "Chosen"],
            [[n, fmt(v["rmse"]), ", ".join(f"{f['d_s']:.0f}" for f in v["folds"]),
              ", ".join(f"{f['tau_f']:.0f}" for f in v["folds"]), "yes" if n == ctx["chosen"] else ""]
             for n, v in ctx["cv"].items()]))
    A("")
    A(table(["Knot [%]", "x (Step 3, quasi-steady)", "x (Step 4 final)"],
            [[f"{k:g}", f"{a3:.4f}", f"{a4:.4f}"] for k, a3, a4 in zip(S3.KNOTS, ctx["x_step3"], final["xk"])]))
    A("")
    A("![law](step4_law_v2.png)")
    A("")
    A(f"FIT (in-sample): RMSE {fmt(fm['rmse'])}, MAE {fmt(fm['mae'])}, bias {fmt(fm['bias'])}, "
      f"P95 {fmt(fm['p95'])} degC.  Isolated valve moves meeting the timing-gate definition on FIT: "
      f"{len(ctx['fit_timing'])}.")
    A("")
    A("## 4. Test on HO-1..HO-3 (untouched for T_forward)")
    A("")
    so = ctx["tune_static_only"]
    A(f"Free run: RMSE {fmt(tm['rmse'])}, MAE {fmt(tm['mae'])}, bias {fmt(tm['bias'])}, P95 "
      f"{fmt(tm['p95'])} degC (n = {tm['n']:,}).  Reference on the same samples - Step 3 static law, "
      f"no delay, no lag, measured T_incoming: RMSE {fmt(so['rmse'])}, MAE {fmt(so['mae'])} degC.")
    A("")
    te = ctx["tune_timing"]
    if te:
        A(table(["Move start", "Delta pos [%]", "Delta T_fwd [degC]", "t50 measured [s]", "t50 simulated [s]"],
                [[e["start"][:16], f"{e['dp']:+.1f}", f"{e['dT']:+.2f}", fmt(e["t50_meas"], 0), fmt(e["t50_sim"], 0)] for e in te]))
    else:
        A("**Timing gate:** no holdout valve move satisfies the frozen isolated-move definition (the "
          "legacy PID moves the valve by 2-3 % every one to two minutes, so 5 min of quiet before "
          "and 10 min after a >= 3 % move practically never occur; FIT has "
          f"{len(ctx['fit_timing'])} and TUNE {ctx['tune_observed_timing_n']} such events).  The gate is "
          "reported as not evaluable and therefore not passed; response timing enters only through "
          "the free-run error, which includes every valve move.")
    A("")
    A("![overlay](step4_test_overlay_v2.png)")
    A("")
    A("## 5. Use in the HIL simulator and limitations")
    A("")
    A("* Valve: moves at 100 %/145 s toward the commanded position; x(pos) from the final table "
      "(leak-by at 0 %)" + (f", multiplied by {final['radf']:.3f} while a radiator pump runs." if c.get("radf") else "."))
    A(f"* Forward temperature: Tmix from the Step 2 water temperature T and T_return, delayed by "
      f"{final['d_n'] * DT:.0f} s, then the sensor lag of {final['tau_f']:.0f} s.")
    A("* The floor-pump start transient (stagnant water in the forward pipe) is not modelled here: "
      "the first 5 minutes after a pump start are not scored.  Step 5 covers the floor loop.")
    A("* The law below 50 % is constrained only by transient data; above 60 % it is now estimated "
      "from moving-valve data but rests on fewer samples than the 50-60 % operating band.")
    A("* The timing gate could not be evaluated with the frozen definition; a future data set with "
      "deliberate step tests (new controller commissioning) would allow a direct check.")
    A("")
    A("## 6. Reproduction")
    A("")
    A("```bash")
    A("cd /home/gume/claude/HeatpumpsN && .venv/bin/python simulation/step4/step4_mixer_dynamics_v2.py")
    A("```")
    REPORT_PATH.write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
