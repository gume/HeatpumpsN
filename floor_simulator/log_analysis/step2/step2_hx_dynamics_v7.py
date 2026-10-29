#!/usr/bin/env python3
"""Step 2 v7 - Primary heat exchanger dynamics (T_boiler -> T_incoming).

Run:  ../.venv/bin/python simulation/step2/step2_hx_dynamics_v7.py
      (from the HeatpumpsN folder; any cwd works)

Order of execution (enforced in main()):
  0. verify the Phase 0 freeze (holdout_v7.json / gates_v7.json hashes)
  1. assert FIT / TUNE / holdout / observed-VAL disjointness (incl. warm-ups)
  2. identify the v7 model on FIT blocks (simulation-error, global search)
  3. TUNE: report metrics, per-mode breakdown and refitted ablations
  4. freeze the parameter dict (SHA-256)
  5. observed validation episodes VAL-1..VAL-9 (not sign-off)
  6. holdout HO-1..HO-3, evaluated once against the frozen gates
  7. v6.3 comparison, plots, parameter JSON and report

The model itself lives in hx_model_v7.py (numba).  Data access goes through
simulation/common/plantdata.py (read-only DuckDB).
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SIM = HERE.parent
os.environ.setdefault("MPLCONFIGDIR", str(SIM / ".cache" / "mpl"))
os.environ.setdefault("NUMBA_CACHE_DIR", str(SIM / ".cache" / "numba"))
sys.path.insert(0, str(SIM / "common"))
sys.path.insert(0, str(HERE))

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from scipy.ndimage import median_filter  # noqa: E402
from scipy.optimize import differential_evolution, minimize  # noqa: E402

import plantdata as pdx  # noqa: E402
import hx_model_v7 as M  # noqa: E402

VERSION = "7.0"
DT = M.DT
WARMUP_S = 1800.0
W = int(WARMUP_S / DT)          # warm-up samples inside every episode
BLOCK = pd.Timedelta(hours=12)
PUMP_MEDIAN_N = 31              # centred 5-min (310 s) majority, Step 1 canonical
DE_SEED = 7
DE_MAXITER = 200
DE_POPSIZE = 12

HOLDOUT_PATH = HERE / "holdout_v7.json"
GATES_PATH = HERE / "gates_v7.json"
FREEZE_PATH = HERE / "freeze_v7.json"
HO_RESULT_PATH = HERE / "holdout_result_v7.json"
PARAMS_PATH = HERE / "step2_params_v7.json"
REPORT_PATH = HERE / "step2_hx_dynamics_report_v7.md"
PLOT_TUNE = HERE / "step2_v7_tune_overlay.png"
PLOT_HO = HERE / "step2_v7_holdout_overlay.png"
PLOT_VAL = HERE / "step2_v7_observed_val_overlay.png"

# name: (lower, upper, log-scaled, unit, meaning)
BOUNDS = {
    "tau_p": (1, 600, True, "s", "lag of the primary water entering the HX"),
    "tau_hx": (5, 600, True, "s", "HX secondary outlet time constant (ACT)"),
    "eps_A": (0.7, 1.05, False, "-", "HX effectiveness, floor pump only"),
    "eps_B": (0.7, 1.05, False, "-", "HX effectiveness, floor + radiator pump(s)"),
    "eps_C": (0.7, 1.05, False, "-", "HX effectiveness, radiator pump(s) only"),
    "eps_N": (0.3, 1.05, False, "-", "HX effectiveness, no secondary pump"),
    "tau_hc": (30, 3e4, True, "s", "heat-store charging time constant (ACT)"),
    "k_hc": (0.0, 1.1, False, "-", "heat-store charge level above T_return (ACT)"),
    "V": (5, 3000, True, "s", "flushing volume / unit flow (floor full open = 1)"),
    "q1": (0.0, 5.0, False, "-", "radiator circuit 1 flow through the fork"),
    "q2": (0.0, 5.0, False, "-", "radiator circuit 2 flow through the fork"),
    "tau_fn": (100, 1e5, True, "s", "stagnant exchange sensor water <-> heat store"),
    "tau_h": (300, 1e5, True, "s", "heat-store relaxation time constant (OFF)"),
    "k_b": (0.0, 1.0, False, "-", "heat-store coupling to the boiler body (OFF)"),
    "k_b_park": (0.0, 1.0, False, "-", "same, diverter parked on DHW"),
    "tau_rc": (60, 3e4, True, "s", "radiator-loop charging time constant"),
    "tau_rd": (300, 1e5, True, "s", "radiator-loop cooling time constant"),
    "dhw_hold_s": (0, 300, False, "s", "state hold at the start of a DHW divert"),
    "d_amb": (-4, 4, False, "degC", "ambient offset relative to the room sensor"),
    "L_pipe": (0, 630, False, "s", "primary pipe plug-flow transport delay"),
    "tau_pipe": (10, 1e5, True, "s", "stagnant primary pipe equilibration"),
    "div_travel_s": (0, 600, False, "s", "DHW->CH diverter travel time"),
    "tau_s": (1, 300, True, "s", "T_incoming sensor (thermowell) lag"),
}
assert tuple(BOUNDS) == M.PARAM_NAMES, "BOUNDS must follow hx_model_v7.PARAM_NAMES"
NAMES = M.PARAM_NAMES
LO = np.array([BOUNDS[n][0] for n in NAMES], float)
HI = np.array([BOUNDS[n][1] for n in NAMES], float)
LOGI = np.array([BOUNDS[n][2] for n in NAMES], bool)

# Ablations: structural element -> parameter overrides held fixed during refit
ABLATIONS = {
    "no secondary flushing (V -> 1e9, q -> 0)": {"V": 1e9, "q1": 0.0, "q2": 0.0},
    "no diverter travel time": {"div_travel_s": 0.0},
    "no sensor lag": {"tau_s": 1e-6},
    "no primary pipe delay": {"L_pipe": 0.0},
    "no boiler-body coupling (k_b = k_b_park = 0)": {"k_b": 0.0, "k_b_park": 0.0},
}

# v6.3 frozen parameters (step2_params_v63.json, identified_parameters)
V63_PARAMS_PATH = HERE / "step2_params_v63.json"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def sha256_params(p: dict) -> str:
    return hashlib.sha256(json.dumps({k: float(v) for k, v in p.items()},
                                     sort_keys=True).encode()).hexdigest()


def git_head() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=HERE,
                                       text=True).strip()
    except Exception:
        return "unknown"


def git_dirty() -> bool:
    try:
        out = subprocess.check_output(["git", "status", "--porcelain", "--", "."],
                                      cwd=HERE, text=True)
        return bool(out.strip())
    except Exception:
        return True


def pack(p: np.ndarray) -> np.ndarray:
    z = np.asarray(p, float).copy()
    z[LOGI] = np.log(z[LOGI])
    return z


def unpack(z: np.ndarray) -> np.ndarray:
    p = np.asarray(z, float).copy()
    p[LOGI] = np.exp(p[LOGI])
    return p


def fmt(x, nd=3):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return "n/a"
    return f"{x:.{nd}f}"


def table(headers, rows) -> str:
    out = ["| " + " | ".join(headers) + " |",
           "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(c) for c in r) + " |")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# data
# ---------------------------------------------------------------------------
class Data:
    """Whole-season 10 s grid with offline-debounced pump states."""

    def __init__(self):
        S = pdx.season_grid(10)
        for i in (1, 2, 3):
            raw = (S[f"p{i}w"].fillna(0).values >= pdx.PUMP_ON_W).astype(np.int8)
            S[f"p{i}"] = median_filter(raw, size=PUMP_MEDIAN_N, mode="nearest").astype(bool)
        S["troom_f"] = S["troom"].ffill()
        self.S = S
        self.tv = S["t"].values

    def episode(self, t0, t1, warmup_before: bool = False) -> dict:
        a = pd.Timestamp(t0)
        b = pd.Timestamp(t1)
        if warmup_before:
            a = a - pd.Timedelta(seconds=WARMUP_S)
        i0 = int(np.searchsorted(self.tv, a.to_datetime64()))
        i1 = int(np.searchsorted(self.tv, b.to_datetime64()))
        x = self.S.iloc[i0:i1]
        ep = dict(
            t=x["t"].values,
            tb=x["tb"].values.astype(float), tret=x["tret"].values.astype(float),
            troom=x["troom_f"].values.astype(float),
            pos=x["pos"].fillna(0).values.astype(float),
            bp=x["bp"].fillna(0).values.astype(float),
            pmod=x["pmod"].fillna(0).values.astype(float),
            ww=x["ww"].fillna(0).values.astype(float),
            p1=x["p1"].values, p2=x["p2"].values, p3=x["p3"].values,
            tinc=x["tinc"].values.astype(float),
            fresh=(x["tinc_age"].values <= pdx.ANALOG_MAX_AGE_S),
            inputs_ok=((x["tb_age"].values <= pdx.ANALOG_MAX_AGE_S)
                       & (x["tret_age"].values <= pdx.ANALOG_MAX_AGE_S)),
            frame=x,
        )
        assert not np.isnan(ep["tb"]).any() and not np.isnan(ep["tret"]).any()
        assert not np.isnan(ep["troom"]).any() and not np.isnan(ep["tinc"]).any()
        m = ep["fresh"].copy()
        m[:W] = False
        ep["score"] = m
        return ep


def run_model(p: np.ndarray, ep: dict, full: bool = False):
    out = M.simulate(np.asarray(p, float), ep["tb"], ep["tret"], ep["troom"],
                     ep["pos"], ep["bp"], ep["pmod"], ep["ww"], ep["p1"],
                     ep["p2"], ep["p3"], ep["tinc"][0], ep["tinc"][0],
                     ep["tinc"][0], W, ep["tinc"])
    return out if full else out[0]


def dhw_windows(ep: dict, ext_s: float = 600.0) -> np.ndarray:
    """Boolean mask of [ww rise, ww fall + ext_s] for every ww event."""
    ww = ep["ww"] >= 0.5
    m = ww.copy()
    n = len(ww)
    ext = int(ext_s / DT)
    for i in range(1, n):
        if ww[i - 1] and not ww[i]:
            m[i:min(n, i + ext + 1)] = True
    return m


def circulating_dhw_mask(ep: dict) -> tuple[np.ndarray, int]:
    """Gate definition: ww rising edge with CH firing (bp > 0, ww = 0) in one
    of the preceding 10 one-minute samples and a secondary pump on.  The
    window is [rise, fall + 600 s]."""
    ww = ep["ww"] >= 0.5
    bp = ep["bp"] > 0
    fr = ep["frame"]
    pump = ((fr["p1w"].fillna(0).values >= pdx.PUMP_ON_W)
            | (fr["p2w"].fillna(0).values >= pdx.PUMP_ON_W)
            | (fr["p3w"].fillna(0).values >= pdx.PUMP_ON_W))
    n = len(ww)
    m = np.zeros(n, bool)
    cnt = 0
    for i in range(1, n):
        if ww[i] and not ww[i - 1]:
            prev = [i - 6 * j for j in range(1, 11) if i - 6 * j >= 0]
            if any(bp[j] and not ww[j] for j in prev) and pump[i]:
                cnt += 1
                j = i
                while j < n and ww[j]:
                    j += 1
                m[i:min(n, j + 61)] = True
    return m, cnt


def metrics(sim: np.ndarray, y: np.ndarray, mask: np.ndarray) -> dict:
    e = (sim - y)[mask]
    if len(e) == 0:
        return dict(n=0, rmse=np.nan, mae=np.nan, bias=np.nan, r2=np.nan, p95=np.nan)
    yy = y[mask]
    ss = ((yy - yy.mean()) ** 2).sum()
    return dict(n=int(len(e)), rmse=float(np.sqrt((e ** 2).mean())),
                mae=float(np.abs(e).mean()), bias=float(e.mean()),
                r2=float(1 - (e ** 2).sum() / ss) if ss > 0 else np.nan,
                p95=float(np.percentile(np.abs(e), 95)))


def suite(records: list[dict]) -> dict:
    e = np.concatenate([r["err"] for r in records]) if records else np.array([])
    ys = np.concatenate([r["y"] for r in records]) if records else np.array([])
    per = [r for r in records if r["m"]["n"] > 0]
    ss = ((ys - ys.mean()) ** 2).sum() if len(ys) else np.nan
    return dict(
        episodes=len(per),
        mean_rmse=float(np.mean([r["m"]["rmse"] for r in per])) if per else np.nan,
        pooled_rmse=float(np.sqrt((e ** 2).mean())) if len(e) else np.nan,
        mean_mae=float(np.mean([r["m"]["mae"] for r in per])) if per else np.nan,
        min_r2=float(np.min([r["m"]["r2"] for r in per])) if per else np.nan,
        median_r2=float(np.median([r["m"]["r2"] for r in per])) if per else np.nan,
        pooled_r2=float(1 - (e ** 2).sum() / ss) if len(e) else np.nan,
        n=int(len(e)),
    )


def evaluate(p: np.ndarray, eps: list[dict], mask_key: str = "score") -> list[dict]:
    out = []
    for ep in eps:
        sim = run_model(p, ep)
        m = ep[mask_key]
        out.append(dict(m=metrics(sim, ep["tinc"], m), err=(sim - ep["tinc"])[m],
                        y=ep["tinc"][m], sim=sim))
    return out


# ---------------------------------------------------------------------------
# identification
# ---------------------------------------------------------------------------
def make_objective(eps: list[dict], fixed: dict | None = None):
    fixed = fixed or {}
    fix_idx = np.array([NAMES.index(k) for k in fixed], int)
    fix_val = np.array([fixed[k] for k in fixed], float)
    free = np.array([i for i in range(len(NAMES)) if i not in set(fix_idx)], int)
    n_tot = sum(int(ep["score"].sum()) for ep in eps)

    def full_params(zfree):
        z = np.empty(len(NAMES))
        z[free] = zfree
        p = unpack(z) if len(fix_idx) == 0 else unpack(np.where(
            np.isin(np.arange(len(NAMES)), fix_idx), 0.0, z))
        p[fix_idx] = fix_val
        return p

    def f(zfree):
        p = full_params(zfree)
        tot = 0.0
        for ep in eps:
            e = (run_model(p, ep) - ep["tinc"])[ep["score"]]
            tot += float((e * e).sum())
        return tot / n_tot

    return f, free, full_params


DRY_RUN = "--dry-run" in sys.argv


def identify(fit_eps: list[dict]) -> tuple[np.ndarray, dict]:
    f, free, full = make_objective(fit_eps)
    bounds = list(zip(pack(LO), pack(HI)))
    t0 = time.time()
    r = differential_evolution(f, bounds, seed=DE_SEED, popsize=DE_POPSIZE,
                               maxiter=2 if DRY_RUN else DE_MAXITER, tol=1e-8,
                               polish=not DRY_RUN,
                               updating="immediate", workers=1)
    p = full(r.x)
    info = dict(mse=float(r.fun), rmse=float(np.sqrt(r.fun)), nfev=int(r.nfev),
                nit=int(r.nit), success=bool(r.success), message=str(r.message),
                seconds=round(time.time() - t0, 1))
    return p, info


def refit_with(p0: np.ndarray, fit_eps: list[dict], fixed: dict) -> np.ndarray:
    f, free, full = make_objective(fit_eps, fixed)
    z0 = pack(np.clip(p0, LO, HI))[free]
    lo = pack(LO)[free]
    hi = pack(HI)[free]
    r = minimize(f, z0, method="L-BFGS-B", bounds=list(zip(lo, hi)),
                 options=dict(maxiter=150))
    return full(r.x)


def boundary_flags(p: np.ndarray) -> dict:
    out = {}
    for i, n in enumerate(NAMES):
        lo, hi, lg = LO[i], HI[i], LOGI[i]
        if lg:
            near_lo = p[i] <= lo * 1.02
            near_hi = p[i] >= hi / 1.02
        else:
            span = hi - lo
            near_lo = p[i] <= lo + 0.01 * span
            near_hi = p[i] >= hi - 0.01 * span
        out[n] = "lower" if near_lo else ("upper" if near_hi else "")
    return out


def mode_breakdown(p: np.ndarray, eps: list[dict]) -> list[dict]:
    E, MD, EL = [], [], []
    for ep in eps:
        sim, H, R, Tp, md = run_model(p, ep, full=True)
        el = np.zeros(len(md))
        for k in range(1, len(md)):
            el[k] = el[k - 1] + DT if md[k] == md[k - 1] else 0.0
        m = ep["score"]
        E.append((sim - ep["tinc"])[m]); MD.append(md[m]); EL.append(el[m])
    e = np.concatenate(E); md = np.concatenate(MD); el = np.concatenate(EL)
    tot = (e ** 2).sum()
    rows = []
    for k, name in enumerate(M.MODE_NAMES):
        for lab, a, b in (("all", 0, 1e12), ("0-2 min", 0, 120), ("2-10 min", 120, 600),
                          ("10-30 min", 600, 1800), (">30 min", 1800, 1e12)):
            s = (md == k) & (el >= a) & (el < b)
            if s.sum() == 0:
                continue
            rows.append(dict(mode=name, elapsed=lab, n=int(s.sum()),
                             rmse=float(np.sqrt((e[s] ** 2).mean())),
                             bias=float(e[s].mean()),
                             sse_share=float((e[s] ** 2).sum() / tot)))
    return rows


# ---------------------------------------------------------------------------
# v6.3 reference
# ---------------------------------------------------------------------------
def v63_simulator():
    os.environ.setdefault("HEATPUMPS_OUT_DIR", str(HERE))
    import importlib
    mod = importlib.import_module("step2_hx_dynamics_v63")
    params = json.loads(V63_PARAMS_PATH.read_text())["identified_parameters"]
    return mod, params


def v63_run(mod, params, ep: dict) -> np.ndarray:
    fr = ep["frame"]
    df = pd.DataFrame(dict(
        t_boiler=ep["tb"], t_inc=ep["tinc"], t_ret=ep["tret"], t_room=ep["troom"],
        burner_pct=ep["bp"], pump_mod=ep["pmod"],
        p1_w=fr["p1w"].fillna(0).values, p2_w=fr["p2w"].fillna(0).values,
        p3_w=fr["p3w"].fillna(0).values, ww_flag=ep["ww"],
        bp_age_s=fr["bp_age"].fillna(1e9).values, pmod_age_s=fr["pmod_age"].fillna(1e9).values,
        tb_age_s=fr["tb_age"].values, tret_age_s=fr["tret_age"].values,
        p3_age_s=fr["p3w_age"].values, p1_age_s=fr["p1w_age"].values,
        p2_age_s=fr["p2w_age"].values,
        tinc_age_s=np.minimum(fr["tinc_age"].values, pdx.ANALOG_MAX_AGE_S),
    ))
    return mod.simulate_episode(df, params, mode="v6", reanchor_index=W)["sim"]


# ---------------------------------------------------------------------------
# plots
# ---------------------------------------------------------------------------
def overlay(path: Path, eps: list[tuple[str, dict, np.ndarray, np.ndarray | None]],
            title: str) -> None:
    n = len(eps)
    fig, axs = plt.subplots(2 * n, 1, figsize=(16, 5.2 * n),
                            gridspec_kw={"height_ratios": [3, 1] * n})
    for i, (lab, ep, sim, ref) in enumerate(eps):
        t = pd.to_datetime(ep["t"])
        ax = axs[2 * i]
        ax.plot(t, ep["tb"], color="#d62728", lw=0.8, label="T_boiler (input)")
        ax.plot(t, ep["tinc"], color="black", lw=1.3, label="T_incoming measured")
        ax.plot(t, sim, color="#1f77b4", lw=1.1, ls="--", label="v7 simulated")
        if ref is not None:
            ax.plot(t, ref, color="#ff7f0e", lw=0.8, ls=":", label="v6.3 simulated")
        ax.plot(t, ep["tret"], color="#2ca02c", lw=0.8, label="T_return (input)")
        ax.axvline(t[W], color="grey", ls="-.", lw=0.8)
        dm = dhw_windows(ep)
        ax.fill_between(t, 0, 1, where=dm, transform=ax.get_xaxis_transform(),
                        color="#e377c2", alpha=0.12, label="DHW window (excluded)")
        ax.set_ylabel("degC"); ax.grid(alpha=0.3); ax.set_title(lab, fontsize=10)
        ax.legend(ncol=7, fontsize=7, loc="upper left")
        ax = axs[2 * i + 1]
        ax.step(t, (ep["pmod"] > 0) * 1.0, color="orange", lw=0.8, label="primary pump")
        ax.step(t, (ep["bp"] > 0) * 0.8, color="#d62728", lw=0.8, label="burner")
        ax.step(t, ep["ww"] * 0.6 + 1.2, color="#e377c2", lw=0.8, label="DHW")
        ax.step(t, ep["p1"] * 0.3 + 2.0, color="#1f77b4", lw=0.8, label="P1")
        ax.step(t, ep["p2"] * 0.3 + 2.4, color="#17becf", lw=0.8, label="P2")
        ax.step(t, ep["p3"] * 0.3 + 2.8, color="black", lw=0.8, label="P3")
        ax.plot(t, ep["pos"] / 100 * 0.8 + 3.3, color="#9467bd", lw=0.8, label="mixer pos")
        ax.set_yticks([]); ax.grid(alpha=0.3); ax.legend(ncol=7, fontsize=7, loc="upper left")
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(path, dpi=80)
    plt.close(fig)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main() -> None:
    t_start = time.time()
    ctx: dict = dict(version=VERSION, generated=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    # 0. freeze verification ------------------------------------------------
    freeze = json.loads(FREEZE_PATH.read_text())
    ho_sha = sha256_file(HOLDOUT_PATH)
    gate_sha = sha256_file(GATES_PATH)
    holdout_ok = (ho_sha == freeze["holdout_sha256"]) and (gate_sha == freeze["gates_sha256"])
    assert holdout_ok, "Frozen holdout/gates hash mismatch - refusing to run"
    HO = json.loads(HOLDOUT_PATH.read_text())
    gates = json.loads(GATES_PATH.read_text())
    ctx.update(freeze=freeze, ho_sha=ho_sha, gate_sha=gate_sha, head=git_head())
    print("[0] freeze verified", ho_sha[:12], gate_sha[:12])

    # 1. split + disjointness -------------------------------------------------
    fit_starts = [pd.Timestamp(a) for a in HO["split"]["FIT"]]
    tune_starts = [pd.Timestamp(a) for a in HO["split"]["TUNE"]]
    ho = {k: (pd.Timestamp(v["t_start"]), pd.Timestamp(v["t_end"]))
          for k, v in HO["holdout"].items()}
    obs = {k: (pd.Timestamp(a) - pd.Timedelta(seconds=WARMUP_S), pd.Timestamp(b))
           for k, (a, b) in HO["observed_val"].items()}
    # Block end = min(start + 12 h, next block start).  The Phase 0 selector
    # built blocks as "local midnight + 12 h absolute", so on the DST change
    # day (2026-03-29) one block would overlap the next; it is truncated here.
    all_starts = sorted(pd.Timestamp(a) for k in ("FIT", "TUNE", "EXCLUDED")
                        for a in HO["split"][k])
    block_end = {a: min(a + BLOCK, b) for a, b in zip(all_starts, all_starts[1:])}
    block_end[all_starts[-1]] = all_starts[-1] + BLOCK
    ctx["truncated_blocks"] = [a.isoformat() for a in all_starts
                               if block_end[a] < a + BLOCK]
    wins = ([("FIT", a, block_end[a]) for a in fit_starts]
            + [("TUNE", a, block_end[a]) for a in tune_starts]
            + [("HO", a, b) for a, b in ho.values()]
            + [("VAL", a, b) for a, b in obs.values()])
    wins.sort(key=lambda w: w[1])
    for (k1, a1, b1), (k2, a2, b2) in zip(wins, wins[1:]):
        assert a2 >= b1, f"overlap {k1} {a1}-{b1} / {k2} {a2}-{b2}"
    buf = pd.Timedelta(hours=HO["criteria"]["buffer_hours"])
    for a, b in ho.values():
        for kind, s, e in wins:
            if kind in ("FIT", "TUNE"):
                assert s >= b + buf or e <= a - buf, "holdout buffer violated"
    ctx["disjoint"] = dict(n_windows=len(wins), buffer_h=HO["criteria"]["buffer_hours"])
    print("[1] disjointness OK:", len(wins), "windows")

    D = Data()
    FIT = [D.episode(a, block_end[a]) for a in fit_starts]
    TUNE = [D.episode(a, block_end[a]) for a in tune_starts]
    ctx["n_fit_blocks"], ctx["n_tune_blocks"], ctx["n_ho_blocks"] = len(FIT), len(TUNE), len(ho)
    ctx["n_fit_scored"] = int(sum(ep["score"].sum() for ep in FIT))
    ctx["n_tune_scored"] = int(sum(ep["score"].sum() for ep in TUNE))
    ctx["fit_input_stale_frac"] = float(np.mean(np.concatenate([~ep["inputs_ok"] for ep in FIT])))

    # 2. identification --------------------------------------------------------
    p_fit, info = identify(FIT)
    ctx["ident"] = info
    print("[2] identified", info)

    # 3. TUNE ------------------------------------------------------------------
    fit_rec = evaluate(p_fit, FIT)
    tune_rec = evaluate(p_fit, TUNE)
    ctx["fit_suite"] = suite(fit_rec)
    ctx["tune_suite"] = suite(tune_rec)
    ctx["tune_modes"] = mode_breakdown(p_fit, TUNE)
    abl = []
    for lab, fixed in ABLATIONS.items():
        pa = refit_with(p_fit, FIT, fixed) if not DRY_RUN else np.where(
            np.isin(np.array(NAMES), list(fixed)), 0, p_fit)
        if DRY_RUN:
            for kk, vv in fixed.items():
                pa[NAMES.index(kk)] = vv
        abl.append(dict(label=lab, fixed=fixed, fit=suite(evaluate(pa, FIT)),
                        tune=suite(evaluate(pa, TUNE))))
        print("   ablation", lab, round(abl[-1]["tune"]["pooled_rmse"], 3))
    ctx["ablations"] = abl
    print("[3] TUNE", ctx["tune_suite"])

    # 4. freeze ------------------------------------------------------------------
    params = {n: float(v) for n, v in zip(NAMES, p_fit)}
    p_hash = sha256_params(params)
    ctx["params"] = params
    ctx["p_hash"] = p_hash
    ctx["bounds_hit"] = boundary_flags(p_fit)
    print("[4] params frozen", p_hash[:16])

    # 5. observed validation ------------------------------------------------------
    mod63, par63 = v63_simulator()
    val = []
    for k, (a, b) in HO["observed_val"].items():
        ep = D.episode(a, b, warmup_before=True)
        sim = run_model(p_fit, ep)
        ref = v63_run(mod63, par63, ep)
        m = ep["score"]
        val.append(dict(id=k, start=a, end=b, v7=metrics(sim, ep["tinc"], m),
                        v63=metrics(ref, ep["tinc"], m), ep=ep, sim=sim, ref=ref))
    ctx["val"] = val
    print("[5] observed VAL done")

    # TUNE comparison with v6.3 (same blocks, same scoring)
    tune63 = []
    for ep in TUNE:
        ref = v63_run(mod63, par63, ep)
        m = ep["score"]
        tune63.append(dict(m=metrics(ref, ep["tinc"], m), err=(ref - ep["tinc"])[m],
                           y=ep["tinc"][m]))
    ctx["tune63_suite"] = suite(tune63)

    # 6. holdout, once --------------------------------------------------------------
    if DRY_RUN:
        # substitute three TUNE blocks for the holdout; outputs go to .cache
        global HO_RESULT_PATH, PARAMS_PATH, REPORT_PATH, PLOT_TUNE, PLOT_HO, PLOT_VAL
        dd = SIM / ".cache" / "dryrun"
        dd.mkdir(parents=True, exist_ok=True)
        HO_RESULT_PATH, PARAMS_PATH, REPORT_PATH = (dd / HO_RESULT_PATH.name, dd / PARAMS_PATH.name,
                                                    dd / REPORT_PATH.name)
        PLOT_TUNE, PLOT_HO, PLOT_VAL = dd / PLOT_TUNE.name, dd / PLOT_HO.name, dd / PLOT_VAL.name
        if HO_RESULT_PATH.exists():
            HO_RESULT_PATH.unlink()
        ho = {f"DRY-{i}": (a, block_end[a]) for i, a in enumerate(tune_starts[10:40:10])}
        print("[6] dry run: holdout replaced by TUNE blocks", list(ho.values()))
    if HO_RESULT_PATH.exists():
        prev = json.loads(HO_RESULT_PATH.read_text())
        assert prev["param_sha256"] == p_hash, (
            "Holdout already evaluated with a different parameter set; refusing "
            "to evaluate it again")
        ctx["ho_reeval"] = True
    else:
        ctx["ho_reeval"] = False
    ho_rec_sh, ho_rec_dhw, ho_eps = [], [], []
    for k, (a, b) in ho.items():
        ep = D.episode(a, b)
        sim = run_model(p_fit, ep)
        ref = v63_run(mod63, par63, ep)
        dm = dhw_windows(ep)
        m_sh = ep["score"] & ~dm
        cm, ncirc = circulating_dhw_mask(ep)
        m_dhw = ep["score"] & cm
        rec = dict(id=k, start=a, end=b, sh=metrics(sim, ep["tinc"], m_sh),
                   sh63=metrics(ref, ep["tinc"], m_sh), n_circ=ncirc,
                   dhw=metrics(sim, ep["tinc"], m_dhw), dhw63=metrics(ref, ep["tinc"], m_dhw))
        ho_rec_sh.append(dict(m=rec["sh"], err=(sim - ep["tinc"])[m_sh], y=ep["tinc"][m_sh]))
        if ncirc > 0:
            ho_rec_dhw.append(dict(m=rec["dhw"], err=(sim - ep["tinc"])[m_dhw], y=ep["tinc"][m_dhw]))
        ho_eps.append((rec, ep, sim, ref))
    sh = suite(ho_rec_sh)
    dh = suite(ho_rec_dhw)
    g_sh = gates["space_heating_suite"]["metrics"]
    g_dh = gates["dhw_suite"]["metrics"]
    gate_rows = []

    def gate(name, suite_name, metric, actual):
        g = (g_sh if suite_name == "space_heating" else g_dh)[metric]
        thr = g["threshold"]
        ok = actual < thr if g["op"] == "<" else actual > thr
        margin = (thr - actual) if g["op"] == "<" else (actual - thr)
        frac = margin / abs(thr)
        gate_rows.append(dict(suite=suite_name, metric=name, op=g["op"], threshold=thr,
                              actual=actual, result="PASS" if ok else "FAIL",
                              margin=margin, margin_frac=frac,
                              marginal=abs(frac) < gates["marginal_margin_fraction"]))

    gate("Mean episode RMSE (degC)", "space_heating", "mean_episode_rmse_c", sh["mean_rmse"])
    gate("Pooled RMSE (degC)", "space_heating", "pooled_rmse_c", sh["pooled_rmse"])
    gate("Mean episode MAE (degC)", "space_heating", "mean_episode_mae_c", sh["mean_mae"])
    gate("Minimum per-episode R2", "space_heating", "min_episode_r2", sh["min_r2"])
    gate("DHW pooled RMSE (degC)", "dhw", "pooled_rmse_c", dh["pooled_rmse"])
    gate("DHW minimum per-episode R2", "dhw", "min_episode_r2", dh["min_r2"])
    overall = all(g["result"] == "PASS" for g in gate_rows)
    ctx.update(ho_sh=sh, ho_dhw=dh, gate_rows=gate_rows, overall=overall, ho_eps=ho_eps)
    result = dict(version=VERSION, param_sha256=p_hash, holdout_sha256=ho_sha,
                  gates_sha256=gate_sha, space_heating=sh, dhw=dh,
                  gates=gate_rows, overall="PASS" if overall else "FAIL")
    if ctx["ho_reeval"]:
        prev = json.loads(HO_RESULT_PATH.read_text())
        assert abs(prev["space_heating"]["pooled_rmse"] - sh["pooled_rmse"]) < 1e-9, \
            "holdout re-evaluation is not reproducible"
        ctx["ho_first_eval"] = prev.get("evaluated_at")
    else:
        result["evaluated_at"] = ctx["generated"]
        result["git_head_at_evaluation"] = ctx["head"]
        result["git_dirty_at_evaluation"] = git_dirty()
        HO_RESULT_PATH.write_text(json.dumps(result, indent=2, default=float))
        ctx["ho_first_eval"] = ctx["generated"]
    print("[6] holdout", "PASS" if overall else "FAIL", sh, dh)

    # 7. plots, export, report ------------------------------------------------------
    ranked = sorted(range(len(TUNE)), key=lambda i: tune_rec[i]["m"]["rmse"])
    pick = [ranked[len(ranked) // 2], ranked[int(len(ranked) * 0.9)]]
    overlay(PLOT_TUNE, [(f"TUNE block {pd.Timestamp(TUNE[i]['t'][0])} "
                         f"(RMSE rank {'median' if j == 0 else 'P90'}, "
                         f"{fmt(tune_rec[i]['m']['rmse'], 2)} degC)",
                         TUNE[i], tune_rec[i]["sim"], None) for j, i in enumerate(pick)],
            "Step 2 v7 - representative TUNE blocks")
    overlay(PLOT_HO, [(f"{r['id']} {r['start']} - {r['end']}", ep, sim, ref)
                      for r, ep, sim, ref in ho_eps],
            "Step 2 v7 - untouched holdout (evaluated once)")
    vsel = [v for v in val if v["id"] in ("VAL-5", "VAL-8", "VAL-9")]
    overlay(PLOT_VAL, [(f"{v['id']} (observed) {v['start']} - {v['end']}", v["ep"], v["sim"], v["ref"])
                       for v in vsel], "Step 2 v7 - observed episodes that failed in v6.2/v6.3")
    ctx["runtime_s"] = round(time.time() - t_start, 1)
    export_json(ctx)
    build_report(ctx)
    print(f"[7] done in {ctx['runtime_s']} s")


def export_json(ctx: dict) -> None:
    out = dict(
        version=VERSION, generated=ctx["generated"],
        model="step2/hx_model_v7.py (numba); DT = 10 s",
        parameters=ctx["params"],
        parameter_units={n: BOUNDS[n][3] for n in NAMES},
        parameter_meaning={n: BOUNDS[n][4] for n in NAMES},
        bounds={n: [BOUNDS[n][0], BOUNDS[n][1]] for n in NAMES},
        boundary_limited={k: v for k, v in ctx["bounds_hit"].items() if v},
        constants=dict(DT=DT, MAX_PIPE_N=M.MAX_PIPE_N, DIV_CLEAR_S=M.DIV_CLEAR_S,
                       pump_on_w=pdx.PUMP_ON_W, pump_debounce_samples=PUMP_MEDIAN_N),
        param_sha256=ctx["p_hash"],
        identification=ctx["ident"],
        fit_suite=ctx["fit_suite"], tune_suite=ctx["tune_suite"],
        tune_suite_v63=ctx["tune63_suite"],
        ablations=[dict(label=a["label"], fixed=a["fixed"], tune=a["tune"]) for a in ctx["ablations"]],
        observed_val={v["id"]: dict(v7=v["v7"], v63=v["v63"]) for v in ctx["val"]},
        holdout=dict(space_heating=ctx["ho_sh"], dhw=ctx["ho_dhw"], gates=ctx["gate_rows"],
                     overall="PASS" if ctx["overall"] else "FAIL",
                     first_evaluated=ctx["ho_first_eval"]),
        provenance=dict(holdout_sha256=ctx["ho_sha"], gates_sha256=ctx["gate_sha"],
                        git_head=ctx["head"]),
    )
    PARAMS_PATH.write_text(json.dumps(out, indent=2, default=float))


def build_report(ctx: dict) -> None:
    P = ctx["params"]
    fs, ts, t63 = ctx["fit_suite"], ctx["tune_suite"], ctx["tune63_suite"]
    sh, dh = ctx["ho_sh"], ctx["ho_dhw"]
    L = []
    A = L.append
    A("# Step 2 Report v7: Primary Heat Exchanger Dynamics (T_boiler -> T_incoming)")
    A("")
    A(f"**Project:** ESP32 floor-heating HIL simulator - parameter extraction  ")
    A(f"**Step / version:** Step 2, v{VERSION} (self-contained; supersedes v6.3)  ")
    A(f"**Generated:** {ctx['generated']} by `simulation/step2/step2_hx_dynamics_v7.py`  ")
    A(f"**Model code:** `simulation/step2/hx_model_v7.py`; data access `simulation/common/plantdata.py`  ")
    A(f"**Database:** `analysis/heatpumps.duckdb` (read-only)  ")
    A(f"**Git HEAD at generation:** `{ctx['head']}`")
    A("")
    A("## 0. Summary")
    A("")
    verdict = "PASS" if ctx["overall"] else "FAIL"
    npass = sum(g["result"] == "PASS" for g in ctx["gate_rows"])
    A(f"**Holdout result: {verdict}** ({npass} of {len(ctx['gate_rows'])} frozen gates pass). "
      "The holdout (HO-1..HO-3) was selected from telemetry only, frozen with the gates "
      "before any v7 model code existed, and evaluated once after the parameter freeze.")
    A("")
    A(table(["Suite", "Metric", "Gate", "Actual", "Result", "Margin", "Margin %", "Note"],
            [[g["suite"], g["metric"], f"{g['op']} {g['threshold']}", fmt(g["actual"], 4),
              g["result"], fmt(g["margin"], 4), f"{100 * g['margin_frac']:+.1f}%",
              "marginal" if g["marginal"] else ""] for g in ctx["gate_rows"]]))
    A("")
    A(f"On the same TUNE blocks and scoring, v7 reaches a pooled RMSE of "
      f"{fmt(ts['pooled_rmse'])} degC versus {fmt(t63['pooled_rmse'])} degC for the "
      f"v6.3 model (mean episode MAE {fmt(ts['mean_mae'])} vs {fmt(t63['mean_mae'])} degC). "
      f"On the holdout space-heating suite v7 reaches {fmt(sh['pooled_rmse'])} degC pooled "
      f"RMSE; the v6.3 model on the same samples is listed in Section 7.")
    A("")
    A("Main physical findings (Section 2), each identified on FIT data:")
    A("")
    A("1. When the boiler primary pump stops, T_incoming is set by the *secondary* flow through the "
      "fork, not by T_return.  The floor branch draws water through the fork only in proportion "
      "to the mixer opening; with the mixer closed and only the floor pump running the fork is "
      "stagnant.  Radiator circuits flush the fork with their own (latent) loop temperature.")
    A("2. The stagnant HX/fork water keeps a heat store that decays over tens of minutes; it is "
      "what v6.x missed when it relaxed T_incoming to T_return within minutes (VAL-5/8/9 failures).")
    A("3. The boiler's internal DHW/CH diverter has a memory: after a DHW cycle the HX primary stays "
      "isolated until the next space-heating burner firing, and the diverter then needs "
      f"{fmt(P['div_travel_s'], 0)} s to deliver heat to the HX.")
    A(f"4. The T_incoming sensor reading lags the water by about {fmt(P['tau_s'], 0)} s (thermowell).")
    A("")
    A("## 1. Data, split and protocol")
    A("")
    A("* Heating season only (2025-12-02 .. 2026-05-17; summer excluded per Step 1).")
    A("* 10 s causal zero-order-hold grid; each sample carries the age of the last raw report.  "
      "T_incoming samples with age > 900 s are not scored (Step 1 outage rule).")
    A(f"* Pump states: raw power >= {pdx.PUMP_ON_W} W, then the Step 1 canonical centred 5-minute "
      f"majority filter ({PUMP_MEDIAN_N} samples).  This is an offline identification filter; in the "
      "HIL the simulator receives the relay states directly.")
    A("* Episodes are 12-hour blocks aligned to 00:00/12:00 local time.  Each block contains its own "
      "30-minute warm-up: the model starts at the block start with all states set to the measured "
      "T_incoming, the sensor state is re-anchored at minute 30, and only minutes 30..720 are scored.  "
      "Blocks are therefore strictly disjoint *including warm-ups*.  A block ends at "
      "min(start + 12 h, next block start); this truncates "
      f"{len(ctx['truncated_blocks'])} block(s) at the DST change "
      f"({', '.join(ctx['truncated_blocks']) or 'none'}).")
    A(f"* Split (from `holdout_v7.json`, committed before modelling): FIT = blocks on even local day "
      f"ordinals ({ctx['n_fit_blocks']} blocks, {ctx['n_fit_scored']:,} scored samples), TUNE = odd "
      f"ordinals ({ctx['n_tune_blocks']} blocks, {ctx['n_tune_scored']:,} samples), holdout "
      f"{ctx['n_ho_blocks']} blocks.  "
      "Blocks overlapping a previously observed validation window (VAL-1..VAL-9 of v5-v6.3, incl. "
      "their 30-min warm-ups) or within 6 h of a holdout block are excluded.")
    A(f"* Disjointness of all {ctx['disjoint']['n_windows']} FIT/TUNE/holdout/observed windows and the "
      f"{ctx['disjoint']['buffer_h']} h holdout buffer are asserted in `main()` before any computation.")
    A("* Parameters are identified on FIT only.  TUNE is used for structure decisions (ablations) and "
      "is never fitted.  The holdout is untouched until Section 6.")
    A("")
    A(table(["Provenance", "Value"], [
        ["holdout_v7.json SHA-256", f"`{ctx['ho_sha']}`"],
        ["gates_v7.json SHA-256", f"`{ctx['gate_sha']}`"],
        ["Parameter dict SHA-256 (frozen before holdout)", f"`{ctx['p_hash']}`"],
        ["Holdout first evaluated", str(ctx["ho_first_eval"])],
        ["Holdout re-evaluated in this run (same parameters, reproducibility check)",
         "yes" if ctx["ho_reeval"] else "no (first evaluation)"],
    ]))
    A("")
    A("## 2. Model")
    A("")
    A("Discrete-time grey-box model, DT = 10 s.  States: T (water at the T_incoming sensor), Ts "
      "(sensor reading), Tp (primary water entering the HX), H (HX body / stagnant-water heat "
      "store), R (radiator-loop return water, latent), a primary-pipe plug-flow ring, and the "
      "diverter memory `div`.")
    A("")
    A("```")
    A("secondary flow through the fork:  F_f = P3 * pos/100,  F_r = q1*P1 + q2*P2,  F = F_f + F_r")
    A(f"div := 1 while ww = 1; div := 0 at the first burner firing >= {M.DIV_CLEAR_S:g} s after ww falls")
    A("ACT  (pmod > 0, ww = 0, div = 0, >= div_travel_s since div cleared):")
    A("      ring: pop oldest cell, push T_boiler;  Tp += a(tau_p) * (popped - Tp)")
    A("      T  += a(tau_hx) * (T_ret + eps_r * (Tp - T_ret) - T)      eps_r: A / B / C / N")
    A("      H  += a(tau_hc) * (T_ret + k_hc * (T - T_ret) - H)")
    A("HOLD (first dhw_hold_s of a DHW divert): states held")
    A("OFF  (all other samples, incl. DHW divert):")
    A("      Tmix = (F_f * T_ret + F_r * R) / F")
    A("      T  += (DT*F/V * (Tmix - T) + DT/tau_fn * (H - T)) / (1 + DT*F/V + DT/tau_fn)")
    A("      H  += a(tau_h) * (amb + k_b' * (T_boiler - amb) - H),  k_b' = k_b_park if div else k_b")
    A("      ring cells += a(tau_pipe) * (T - cell)")
    A("R:   += a(tau_rc) * (T - R) in ACT with a radiator pump, else += a(tau_rd) * (amb - R)")
    A("Ts:  += a(tau_s) * (T - Ts)      (model output)")
    A("a(tau) = DT / (tau + DT);  amb = T_room + d_amb")
    A("```")
    A("")
    A("Physical basis of each element (evidence on FIT data, see `v7_notes.md` for the exploration log):")
    A("")
    A("* *Flushing by secondary flow:* after a primary stop with the mixer open, T_incoming collapses "
      "to T_return within minutes; with the mixer closed it does not.  A decision tree on FIT "
      "primary-stop events separates the two cases by the mixer position after the stop "
      "(exploration log in `v7_notes.md`; the ablation in Section 4.2 is the in-run evidence).")
    A("* *Heat store:* with no secondary flow, T_incoming decays over tens of minutes and stays well "
      "above T_return while the boiler body is hot.")
    A("* *Diverter memory and travel:* after a DHW cycle, the median delay from primary-pump start to "
      "a 3 degC T_incoming rise is longer than after a plain pause (FIT onsets; numbers in the notes).")
    A("* *Primary pipe and sensor lag:* the start of a firing shows a delayed, smoothed rise.")
    A("")
    A("## 3. Identification (FIT only)")
    A("")
    info = ctx["ident"]
    A(f"Objective: mean squared simulation error of the sensor output over all scored FIT samples "
      f"(free-run simulation, no re-anchoring inside the scored window).  Global search: SciPy "
      f"differential evolution (seed {DE_SEED}, population {DE_POPSIZE}, max {DE_MAXITER} "
      f"generations, log-scaled time constants) with L-BFGS-B polish.  Result: "
      f"{info['nfev']:,} evaluations, {info['nit']} generations, {info['seconds']} s, "
      f"FIT RMSE {fmt(info['rmse'])} degC; optimiser message: \"{info['message']}\".  "
      f"Input staleness on FIT (T_boiler or T_return age > 900 s): "
      f"{100 * ctx['fit_input_stale_frac']:.2f} % of samples.")
    A("")
    rows = []
    for n in NAMES:
        bh = ctx["bounds_hit"][n]
        rows.append([f"`{n}`", f"{P[n]:.6g}", BOUNDS[n][3], f"[{BOUNDS[n][0]:g}, {BOUNDS[n][1]:g}]",
                     bh + " bound" if bh else "", BOUNDS[n][4]])
    A(table(["Parameter", "Value", "Unit", "Search range", "Boundary", "Meaning"], rows))
    A("")
    nb = [n for n, v in ctx["bounds_hit"].items() if v]
    A(f"Boundary-limited parameters: {', '.join('`' + n + '`' for n in nb) if nb else 'none'}.  "
      "A boundary value means the corresponding element is switched off or saturated by the data "
      "(for example a time constant at its lower bound acts as an instantaneous step, `q2 = 0` "
      "means radiator circuit 2 does not flush the fork measurably).  These are reported as is and "
      "are not extrapolated.")
    A("")
    A("## 4. FIT and TUNE performance")
    A("")
    A(table(["Set", "Blocks", "Scored samples", "Mean RMSE", "Pooled RMSE", "Mean MAE",
             "Median R2", "Min R2"],
            [["FIT (v7)", fs["episodes"], f"{fs['n']:,}", fmt(fs["mean_rmse"]), fmt(fs["pooled_rmse"]),
              fmt(fs["mean_mae"]), fmt(fs["median_r2"]), fmt(fs["min_r2"])],
             ["TUNE (v7)", ts["episodes"], f"{ts['n']:,}", fmt(ts["mean_rmse"]), fmt(ts["pooled_rmse"]),
              fmt(ts["mean_mae"]), fmt(ts["median_r2"]), fmt(ts["min_r2"])],
             ["TUNE (v6.3 model)", t63["episodes"], f"{t63['n']:,}", fmt(t63["mean_rmse"]),
              fmt(t63["pooled_rmse"]), fmt(t63["mean_mae"]), fmt(t63["median_r2"]), fmt(t63["min_r2"])]]))
    A("")
    A("The minimum R2 is strongly negative on both sets because some blocks are almost isothermal "
      "(no heating; T_incoming varies by a few tenths of a degree), where R2 is not informative.  "
      "The median R2 and the RMSE/MAE describe typical behaviour.")
    A("")
    A("### 4.1 TUNE error by mode and time in mode")
    A("")
    A(table(["Mode", "Time in mode", "Samples", "RMSE", "Bias (sim-meas)", "Share of TUNE SSE"],
            [[r["mode"], r["elapsed"], f"{r['n']:,}", fmt(r["rmse"]), fmt(r["bias"]),
              f"{100 * r['sse_share']:.1f}%"] for r in ctx["tune_modes"]]))
    A("")
    act_long = [r for r in ctx["tune_modes"] if r["mode"] == "active" and r["elapsed"] == ">30 min"]
    trans = sum(r["sse_share"] for r in ctx["tune_modes"]
                if r["elapsed"] in ("0-2 min", "2-10 min", "10-30 min"))
    A(f"Transitions (the first 30 minutes of each mode) carry {100 * trans:.0f} % of the TUNE squared "
      f"error.  Settled active heating (> 30 min) has an RMSE of "
      f"{fmt(act_long[0]['rmse']) if act_long else 'n/a'} degC.  Most of the remaining error is "
      "transition timing: the boiler signals (burner %, pump %) are event-reported by the EMS bus "
      "with irregular latency; for example, a timing error of half a minute on a swing of several "
      "tens of degrees produces errors of the order of ten degrees for that half minute.")
    A("")
    A("### 4.2 Structure check: refitted ablations (fit on FIT, scored on TUNE)")
    A("")
    A("Each row removes one structural element (parameters held at the stated values), refits the "
      "remaining parameters on FIT with L-BFGS-B from the v7 optimum, and scores TUNE.")
    A("")
    A(table(["Removed element", "TUNE pooled RMSE", "TUNE mean MAE", "Delta pooled vs v7"],
            [[a["label"], fmt(a["tune"]["pooled_rmse"]), fmt(a["tune"]["mean_mae"]),
              f"{a['tune']['pooled_rmse'] - ts['pooled_rmse']:+.3f}"] for a in ctx["ablations"]]
            + [["(none - full v7)", fmt(ts["pooled_rmse"]), fmt(ts["mean_mae"]), "+0.000"]]))
    A("")
    A("A positive delta means the element improves out-of-sample (TUNE) accuracy.")
    A("")
    A("## 5. Observed validation episodes (not sign-off evidence)")
    A("")
    A("These windows were seen by earlier versions (VAL-1..VAL-7 since v5/v6.1, VAL-5 failed in "
      "v6.2, VAL-8/VAL-9 failed as the v6.3 holdout).  They are excluded from FIT/TUNE and reported "
      "for continuity only.  Each episode uses a 30-minute warm-up before its start.")
    A("")
    A(table(["ID", "Window", "Scored", "v7 RMSE", "v7 MAE", "v7 R2", "v6.3 RMSE", "v6.3 MAE"],
            [[v["id"], f"{v['start'][:16]} -> {v['end'][11:16]}", v["v7"]["n"], fmt(v["v7"]["rmse"]),
              fmt(v["v7"]["mae"]), fmt(v["v7"]["r2"]), fmt(v["v63"]["rmse"]), fmt(v["v63"]["mae"])]
             for v in ctx["val"]]))
    A("")
    A("![observed](step2_v7_observed_val_overlay.png)")
    A("")
    A("## 6. Untouched holdout (evaluated once)")
    A("")
    rows = []
    for r, ep, sim, ref in ctx["ho_eps"]:
        rows.append([r["id"], f"{r['start'].isoformat()[:16]} -> {r['end'].isoformat()[11:16]}",
                     r["sh"]["n"], fmt(r["sh"]["rmse"]), fmt(r["sh"]["mae"]), fmt(r["sh"]["r2"]),
                     r["n_circ"], r["dhw"]["n"], fmt(r["dhw"]["rmse"]), fmt(r["dhw"]["r2"])])
    A(table(["ID", "Window", "SH scored", "SH RMSE", "SH MAE", "SH R2",
             "Circ. DHW events", "DHW scored", "DHW RMSE", "DHW R2"], rows))
    A("")
    A(f"Space-heating suite (all DHW windows [ww rise, ww fall + 600 s] excluded): mean episode RMSE "
      f"{fmt(sh['mean_rmse'])}, pooled RMSE {fmt(sh['pooled_rmse'])}, mean MAE {fmt(sh['mean_mae'])} "
      f"degC, per-episode R2 min {fmt(sh['min_r2'])} (pooled R2 {fmt(sh['pooled_r2'])}; pooled R2 is "
      "higher because pooling adds the between-episode variance).  DHW suite (circulating DHW "
      f"windows): pooled RMSE {fmt(dh['pooled_rmse'])} degC, min per-episode R2 {fmt(dh['min_r2'])}.")
    A("")
    A("![holdout](step2_v7_holdout_overlay.png)")
    A("")
    A("## 7. Comparison with v6.3 on the same samples")
    A("")
    rows = [[r["id"], fmt(r["sh"]["rmse"]), fmt(r["sh63"]["rmse"]), fmt(r["sh"]["mae"]),
             fmt(r["sh63"]["mae"]), fmt(r["dhw"]["rmse"]), fmt(r["dhw63"]["rmse"])]
            for r, *_ in ctx["ho_eps"]]
    A(table(["Holdout", "v7 SH RMSE", "v6.3 SH RMSE", "v7 SH MAE", "v6.3 SH MAE",
             "v7 DHW RMSE", "v6.3 DHW RMSE"], rows))
    A("")
    A("The v6.3 model is run with its frozen parameters (`step2_params_v63.json`) and its own causal "
      "pump filter, re-anchored at the same sample as v7.")
    A("")
    A("## 8. Use in the HIL simulator and limitations")
    A("")
    A("* **Inputs in closed loop.** The HIL must provide T_boiler, burner and primary-pump state and "
      "the DHW flag from a boiler model (Phase 6), the pump relay states, the mixer position (from "
      "the Phase 4 actuator model) and T_return (Phase 5).  The v7 model was identified with "
      "measured inputs; its accuracy in closed loop also depends on those models.")
    A("* **Gate outcome.** The frozen gates were set before modelling; their outcome above is final "
      f"for v7.  The error budget (Section 4.1) shows that settled active heating is reproduced with "
      f"an RMSE of {fmt(act_long[0]['rmse']) if act_long else 'n/a'} degC on TUNE and that the "
      "residual is dominated by transitions, whose timing is limited by the event-driven boiler "
      "telemetry.")
    A("* **Unmeasured radiator loop.** The radiator return temperature is not measured; R is a "
      "latent state identified only through T_incoming.")
    A("* **Boundary-limited parameters** (Section 3) are not identifiable from this data set; the "
      "simulator should keep them at the reported values.")
    A("* **Mixer position** enters via the dead-reckoned `mixer_state` (drift about 2 % per Step 1).")
    A("* **No C++ export in this step.** The C++ implementation and its parity test are produced once "
      "for the integrated plant in Phase 6; `hx_model_v7.py` is written for a line-by-line port.")
    A("")
    A("## 9. Reproduction")
    A("")
    A("```bash")
    A("cd /home/gume/claude/HeatpumpsN")
    A(".venv/bin/python simulation/step2/select_holdout_v7.py   # Phase 0 (already frozen; do not rerun)")
    A(".venv/bin/python simulation/step2/step2_hx_dynamics_v7.py")
    A("```")
    A("")
    A(f"Runtime of this run: {ctx['runtime_s']} s.  Outputs: `step2_params_v7.json`, this report, "
      "`holdout_result_v7.json`, and the three PNG overlays in `simulation/step2/`.")
    REPORT_PATH.write_text("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
