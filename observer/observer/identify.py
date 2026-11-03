"""Identification of the controller's mixer model from live data.

The controller's predictive mixer control (firmware/src/mpc_core.cpp,
firmware/docs/predictive-mixer.md) uses, per pump scenario (floor, rad1,
rad2, both), this model on a 10 s step:

    slope  += 0.3 * ((T_inc - T_inc_prev)/10 - slope)
    T_w     = T_inc + tau_inc * slope                    supply water (boiler-role sensor lead)
    T_mix   = T_ret + x(pos) * (T_w - T_ret)             x(): 11-knot piecewise-linear law
    T_fwd   = lag(tau_fwd) of T_mix delayed by round(delay/10) steps

`simulate` reproduces it exactly (checked against the controller's published
model output). For fixed delay and lags the output is linear in the law's
values, so `fit_scenario` solves the law by bounded least squares (monotone,
0..1, pulled towards the current law where the valve was never seen) for every
delay/lag combination of a grid, and keeps the combination whose free-run
simulation fits best. The current and the identified parameters are then
compared on held-out data (the last part of the recorded segments, never used
for fitting): a proposal is made only when the identified ones predict
clearly better. Nothing is sent to the controller here.
"""
from __future__ import annotations

import math
import time

import numpy as np

DT = 10.0
MAX_DELAY_CELLS = 15                 # the firmware keeps 16 cells; delay_s <= 150 s
KNOTS = 11
SCENARIOS = ("floor", "rad1", "rad2", "both")
C = "heatpumpsn/controller/state/"
SLOW = "heatpumpsn/controller/log/slow#"
# The model's inputs come from the controller's slow log: exactly the values its model uses, every
# 10 s (sensor resolution, the unrounded dead-reckoned position 0..1); the published state values
# are rounded or filtered and gave offsets of up to 0.6 K against the controller's own model output.
SIGNALS = {"inc": SLOW + "boiler", "ret": SLOW + "ret", "fwd": SLOW + "mix",
           "pos": SLOW + "pos", "floor": C + "floor_pump_physical", "r1": C + "pump_1_physical",
           "r2": C + "pump_2_physical", "dhw": "ems-esp/boiler_data#dhw.charging", "homing": C + "mpc#homing",
           "model": C + "mpc#modelFwd", "sp": C + "floor_setpoint"}
# Fallback while the slow log has not been recorded long enough: the published state values
# (rounded position, filtered temperatures): usable, but marked as approximate.
STATE_INPUTS = {"inc": C + "temp_boiler", "ret": C + "temp_return", "fwd": C + "temp_mix",
                "pos": C + "mixer_position"}
# how long a value may be held on the 10 s grid (seconds); state signals are published on change
HOLD = {"inc": 120, "ret": 120, "fwd": 120, "model": 30, "pos": 6 * 3600, "floor": 1e9, "r1": 1e9, "r2": 1e9,
        "dhw": 1e9, "homing": 1e9, "sp": 1e9}
MODEL_FIELDS = ("knots_pct", "xk", "delay_s", "tau_fwd_s", "tau_inc_s")


# --- data ------------------------------------------------------------------------------
def to_grid(series: dict, start: float, stop: float) -> dict[str, np.ndarray]:
    """{name: (times_s, values)} -> values on a common 10 s grid (zero-order hold within HOLD)."""
    grid = np.arange(math.floor(start / DT) * DT, stop, DT)
    out = {"t": grid}
    for name, (t, v) in series.items():
        t, v = np.asarray(t, float), np.asarray(v, float)
        ok = np.isfinite(v)
        t, v = t[ok], v[ok]
        if not len(t):
            out[name] = np.full(len(grid), np.nan)
            continue
        idx = np.searchsorted(t, grid, side="right") - 1
        vals = np.where(idx >= 0, v[np.clip(idx, 0, None)], np.nan)
        age = np.where(idx >= 0, grid - t[np.clip(idx, 0, None)], np.inf)
        vals[age > HOLD.get(name, 120)] = np.nan
        out[name] = vals
    return out


def fetch(data_source, start: str = "-14d", stop: str = "now", source: str = "slow") -> dict[str, np.ndarray]:
    """The model's signals from the database (read-only) on the 10 s grid.

    source "slow": the controller's slow log (exact model inputs); "state": the published state values."""
    signals = dict(SIGNALS) if source == "slow" else {**SIGNALS, **STATE_INPUTS}
    raw = data_source.series(list(signals.values()), start, stop, "10s", "mean", limit=5_000_000)
    inv = {v: k for k, v in signals.items()}
    series = {inv[s]: ([x.timestamp() for x in r.times], [float(y) if isinstance(y, (int, float)) else np.nan
                                                          for y in r.values]) for s, r in raw.items()}
    times = [t for ts, _ in series.values() for t in (ts[:1] + ts[-1:])]
    if not times:
        raise ValueError("no data for the mixer model in this range (is the controller's log streaming on?)")
    g = to_grid(series, min(times), max(times) + DT)
    if source == "slow":
        g["pos"] = g["pos"] * 100.0        # the slow log has 0..1, the law's knots are in %
    g["source"] = source
    return g


def scenario_of(g: dict) -> np.ndarray:
    r1, r2 = g["r1"] > 0.5, g["r2"] > 0.5
    return np.where(r1 & r2, "both", np.where(r1, "rad1", np.where(r2, "rad2", "floor")))


def segments(g: dict, min_minutes: float = 20, skip_start_s: float = 300, min_contrast_k: float = 3.0) -> list[dict]:
    """Stretches usable for identification: floor pump on, no DHW charging, no homing, all
    inputs present, one scenario; the first minutes after a pump start (flush) are skipped."""
    n = len(g["t"])
    scen = scenario_of(g)
    ok = (g["floor"] > 0.5) & (g["dhw"] < 0.5) & (g["homing"] < 0.5)
    for k in ("inc", "ret", "fwd", "pos"):
        ok &= np.isfinite(g[k])
    out, i = [], 0
    while i < n:
        if not ok[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and ok[j + 1] and scen[j + 1] == scen[i]:
            j += 1
        # skip the flush after a floor-pump start (the controller does not run its model then)
        a = i + int(skip_start_s / DT) if i == 0 or g["floor"][i - 1] < 0.5 else i
        if (j - a + 1) * DT >= min_minutes * 60:
            sl = slice(a, j + 1)
            contrast = np.nanmedian(g["inc"][sl] - g["ret"][sl])
            if contrast >= min_contrast_k:
                out.append({"scenario": str(scen[i]), "slice": sl, "start": float(g["t"][a]),
                            "hours": (j - a + 1) * DT / 3600, "contrast_k": float(contrast)})
        i = j + 1
    return out


# --- the firmware model -------------------------------------------------------------------
def supply_water(inc: np.ndarray, tau_inc: float) -> np.ndarray:
    """T_w = T_inc + tau_inc * slope (the firmware's filtered slope, alpha 0.3)."""
    slope = np.zeros(len(inc))
    s = 0.0
    for k in range(1, len(inc)):
        s += 0.3 * ((inc[k] - inc[k - 1]) / DT - s)
        slope[k] = s
    return inc + tau_inc * slope


def law(pos_pct: np.ndarray, knots: np.ndarray, xk: np.ndarray) -> np.ndarray:
    return np.interp(pos_pct, knots, xk)


def hat_basis(pos_pct: np.ndarray, knots: np.ndarray) -> np.ndarray:
    """B[:, k] such that law(pos) = B @ xk (piecewise-linear interpolation weights)."""
    B = np.zeros((len(pos_pct), len(knots)))
    p = np.clip(pos_pct, knots[0], knots[-1])
    j = np.clip(np.searchsorted(knots, p, side="right") - 1, 0, len(knots) - 2)
    w = (p - knots[j]) / (knots[j + 1] - knots[j])
    B[np.arange(len(p)), j] = 1 - w
    B[np.arange(len(p)), j + 1] += w
    return B


def _delay_lag(u: np.ndarray, init: float, cells: int, tau: float) -> np.ndarray:
    """Delay by `cells` steps (pre-filled with `init`) then first-order lag starting at `init`."""
    from scipy.signal import lfilter

    if cells > 0:
        u = np.concatenate([np.full(cells, init), u[:-cells]]) if cells < len(u) else np.full(len(u), init)
    a = DT / (tau + DT)
    y, _ = lfilter([a], [1, -(1 - a)], u, zi=[(1 - a) * init])
    return y


def simulate(seg: dict, params: dict, g: dict) -> np.ndarray:
    """The firmware model's T_fwd over one segment (initialised like the firmware: on T_fwd)."""
    sl = seg["slice"]
    inc, ret, fwd, pos = g["inc"][sl], g["ret"][sl], g["fwd"][sl], g["pos"][sl]
    tw = supply_water(inc, params["tau_inc_s"])
    tmix = ret + law(pos, np.asarray(params["knots_pct"], float), np.asarray(params["xk"], float)) * (tw - ret)
    cells = min(int(round(params["delay_s"] / DT)), MAX_DELAY_CELLS)
    return _delay_lag(tmix, fwd[0], cells, params["tau_fwd_s"])


def rmse(segs: list[dict], params: dict, g: dict, warmup_s: float = 300) -> tuple[float, int]:
    """Free-run error of the model against the measured T_fwd (after a warm-up per segment)."""
    err, n = 0.0, 0
    w = int(warmup_s / DT)
    for s in segs:
        y = simulate(s, params, g)
        m = g["fwd"][s["slice"]]
        e = (y - m)[w:]
        e = e[np.isfinite(e)]
        err += float(np.sum(e ** 2))
        n += len(e)
    return (math.sqrt(err / n) if n else float("nan")), n


# --- the fit --------------------------------------------------------------------------------
def _law_lsq(segs, g, knots, delay_s, tau_fwd, tau_inc, prior_xk, coverage, warmup_s=300, smooth=2.0):
    """The law for fixed timing: bounded least squares on the linear model, monotone and 0..1."""
    from scipy.optimize import lsq_linear

    cells = min(int(round(delay_s / DT)), MAX_DELAY_CELLS)
    rows, rhs = [], []
    w = int(warmup_s / DT)
    for s in segs:
        sl = s["slice"]
        inc, ret, fwd, pos = g["inc"][sl], g["ret"][sl], g["fwd"][sl], g["pos"][sl]
        tw = supply_water(inc, tau_inc)
        B = hat_basis(pos, knots) * (tw - ret)[:, None]
        const = _delay_lag(ret, fwd[0], cells, tau_fwd)           # the part not depending on the law
        cols = np.column_stack([_delay_lag(B[:, k], 0.0, cells, tau_fwd) for k in range(len(knots))])
        # the pre-filled delay cells and the lag's start hold T_fwd(0): in `const`, not in the law part
        rows.append(cols[w:])
        rhs.append((fwd - const)[w:])
    A, b = np.vstack(rows), np.concatenate(rhs)
    ok = np.isfinite(b) & np.all(np.isfinite(A), axis=1)
    A, b = A[ok], b[ok]
    scale = math.sqrt(len(b)) if len(b) else 1.0
    # monotone 0..1 by x = L @ d, d >= 0 (x0 and increments); prior where the valve was not seen,
    # a little smoothness everywhere
    L = np.tril(np.ones((KNOTS, KNOTS)))
    # knots the valve did not visit keep the current value: a strong pull there, fading with
    # the minutes spent near a knot (10 minutes halve it)
    prior_w = scale * 2.0 / (1.0 + coverage / 10.0)
    P = np.diag(prior_w) @ L
    D2 = np.diff(np.eye(KNOTS), 2, axis=0) * scale * 0.01 * smooth
    M = np.vstack([A @ L, P, D2 @ L])
    y = np.concatenate([b, prior_w * prior_xk, np.zeros(KNOTS - 2)])
    res = lsq_linear(M, y, bounds=(0, 1), lsmr_tol="auto", max_iter=2000)
    xk = np.clip(L @ res.x, 0, 1)
    return xk


def fit_scenario(segs: list[dict], g: dict, current: dict, delays=None, taus_fwd=None, taus_inc=None) -> dict:
    """Search delay and lags on a grid, the law by least squares; the best free-run fit wins."""
    knots = np.asarray(current["knots_pct"], float)
    prior = np.asarray(current["xk"], float)
    pos_all = np.concatenate([g["pos"][s["slice"]] for s in segs])
    # how much each knot was visited (hat weights), to trust the data only where the valve was
    coverage = hat_basis(pos_all, knots).sum(axis=0) * DT / 60          # minutes
    delays = delays if delays is not None else [d * DT for d in range(0, MAX_DELAY_CELLS + 1)]
    taus_fwd = taus_fwd if taus_fwd is not None else [10, 20, 35, 50, 75, 100, 150, 200, 300]
    taus_inc = taus_inc if taus_inc is not None else [0, 25, 50, 75, 100, 150, 200]
    best = None
    for tau_inc in taus_inc:
        for tau_fwd in taus_fwd:
            for delay in delays:
                xk = _law_lsq(segs, g, knots, delay, tau_fwd, tau_inc, prior, coverage)
                cand = {"knots_pct": knots.tolist(), "xk": xk.tolist(), "delay_s": float(delay),
                        "tau_fwd_s": float(tau_fwd), "tau_inc_s": float(tau_inc)}
                e, n = rmse(segs, cand, g)
                if best is None or e < best[0]:
                    best = (e, cand)
    # refine the lags around the best grid point
    e0, cand = best
    for tau_fwd in np.linspace(max(1, cand["tau_fwd_s"] * 0.6), cand["tau_fwd_s"] * 1.4, 7):
        for tau_inc in np.linspace(max(0, cand["tau_inc_s"] * 0.6), max(10, cand["tau_inc_s"] * 1.4), 5):
            xk = _law_lsq(segs, g, knots, cand["delay_s"], tau_fwd, tau_inc, prior, coverage)
            c = {**cand, "xk": xk.tolist(), "tau_fwd_s": round(float(tau_fwd), 2), "tau_inc_s": round(float(tau_inc), 2)}
            e, _ = rmse(segs, c, g)
            if e < best[0]:
                best = (e, c)
    best[1]["xk"] = [round(x, 6) for x in best[1]["xk"]]
    return {"params": best[1], "train_rmse": best[0], "coverage_min": coverage.round(1).tolist()}


def split_holdout(segs: list[dict], share: float = 0.3) -> tuple[list[dict], list[dict]]:
    """The last `share` of the recorded time (whole segments) is held out."""
    segs = sorted(segs, key=lambda s: s["start"])
    total = sum(s["hours"] for s in segs)
    hold, acc = [], 0.0
    for s in reversed(segs):
        if acc >= share * total:
            break
        hold.insert(0, s)
        acc += s["hours"]
    train = [s for s in segs if s not in hold]
    return train, hold


def identify(g: dict, current_sets: dict, min_train_h: float = 2.0, min_hold_h: float = 0.5,
             min_gain: float = 0.15, min_gain_k: float = 0.15) -> dict:
    """Identify every scenario with enough data; compare with the current set on held-out data."""
    started = time.time()
    segs = segments(g)
    out = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "range": [float(g["t"][0]), float(g["t"][-1])],
           "scenarios": {}}
    for scen in SCENARIOS:
        ss = [s for s in segs if s["scenario"] == scen]
        hours = sum(s["hours"] for s in ss)
        current = current_sets.get(scen)
        r = {"hours": round(hours, 2), "segments": len(ss)}
        if current is None:
            r["status"] = "no current parameters from the controller"
        elif not ss:
            r["status"] = "no usable data"
        else:
            train, hold = split_holdout(ss)
            th, hh = sum(s["hours"] for s in train), sum(s["hours"] for s in hold)
            r.update(train_h=round(th, 2), holdout_h=round(hh, 2))
            if th < min_train_h or hh < min_hold_h:
                r["status"] = f"not enough data (needs {min_train_h} h to fit and {min_hold_h} h to check)"
            else:
                fit = fit_scenario(train, g, current)
                cur_hold, n = rmse(hold, current, g)
                new_hold, _ = rmse(hold, fit["params"], g)
                cur_train, _ = rmse(train, current, g)
                better = (cur_hold - new_hold) >= max(min_gain * cur_hold, min_gain_k)
                r.update(current=dict((k, current[k]) for k in MODEL_FIELDS), identified=fit["params"],
                         coverage_min=fit["coverage_min"],
                         rmse={"train_current": round(cur_train, 3), "train_identified": round(fit["train_rmse"], 3),
                               "holdout_current": round(cur_hold, 3), "holdout_identified": round(new_hold, 3),
                               "holdout_points": n},
                         status="proposal" if better else "current parameters are as good")
        out["scenarios"][scen] = r
    out["seconds"] = round(time.time() - started, 1)
    return out
