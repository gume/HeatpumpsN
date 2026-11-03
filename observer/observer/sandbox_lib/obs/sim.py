"""The heating controller's mixer control, simulated with its own MPC core (the firmware's C++).

    from obs import sim
    sim.available()                         # {"ok": True, "where": ...}
    sets = sim.current_sets()               # the controller's parameter sets now {floor|rad1|rad2|both: {...}}
    r = sim.closed_loop(hours=4, setpoint=[(0, 35), (2, 30)], radiators=[(1, 1.5, "rad1")])
    r["metrics"], r["events"], r["trace"]   # trace: a DataFrame (fwd, setpoint, pos, target, inc, ret, mode)
    r = sim.replay("2026-11-02T08:00Z", "2026-11-02T10:00Z", sets={"floor": {"delay_s": 60}})
    sim.compare(sim.replay, variants={"now": None, "slower": {"floor": {"delay_s": 90}}}, start=..., stop=...)

closed_loop: the identified house (coupled boiler, heat exchanger, mixer, slab
and room; the observed boiler cycles), with the controller closing the loop.
replay: a recorded stretch (floor pump on, one radiator state) replayed with
its recorded supply, return and setpoint; the mixer is a model (by default the
controller's own) and the simulated controller drives it.

``sets`` are parameter sets by scenario; given fields override the controller's
current set (or the firmware default when it is not known); ``mixer`` replaces
the simulated plant's mixer (knots_pct, xk, delay_s, tau_fwd_s, tau_inc_s).
``settings``: the controller's settings around the core (min_open_pct 40,
ceiling_pct 85, stroke_s 145, overshoot_delta_k 3) - not published by the
controller, these are its defaults.

Metrics: time_to_setpoint_s, overshoot_k, rmse_k (rmse_supplied_k: only while
the supply can deliver), travel_pct_h, starts_h, reversals_h, fallbacks (to
homing/cautious), anomalies, mpc_share.
"""
from ._client import ObsError, call

SCENARIOS = ("floor", "rad1", "rad2", "both")
MODEL_FIELDS = ("knots_pct", "xk", "delay_s", "tau_fwd_s", "tau_inc_s")
_FROM_CONTROLLER = {}


def _mods():
    try:
        from observer.sim import fwcore, loop, replay  # mounted read-only by the observer
    except ImportError as exc:                          # pragma: no cover - depends on the sandbox
        raise RuntimeError(f"the simulation package is not available in this sandbox: {exc}") from exc
    return fwcore, loop, replay


def available():
    try:
        fwcore = _mods()[0]
    except RuntimeError as exc:
        return {"ok": False, "where": str(exc)}
    ok, where = fwcore.available()
    return {"ok": ok, "where": where}


def default_set(scenario="floor"):
    """The firmware's built-in parameter set of a scenario."""
    return _mods()[0].default_set(scenario)


def current_sets():
    """The controller's current sets as it last published them (missing scenarios: firmware defaults)."""
    if not _FROM_CONTROLLER:
        try:
            _FROM_CONTROLLER.update(call("mpc_params") or {"-": None})
        except ObsError:                    # outside an observer run: the firmware defaults
            _FROM_CONTROLLER["-"] = None
    fwcore = _mods()[0]
    return {sc: fwcore.full_set(sc, _FROM_CONTROLLER.get(sc)) for sc in SCENARIOS}


def _sets(sets):
    base = current_sets()
    return {sc: dict(base[sc], **((sets or {}).get(sc) or {})) for sc in SCENARIOS}


def _cfg(settings):
    loop = _mods()[1]
    s = dict(min_open_pct=40, ceiling_pct=85, stroke_s=145, overshoot_delta_k=3.0, **(settings or {}))
    return loop.LoopConfig(pmin=s["min_open_pct"] / 100, pmax=s["ceiling_pct"] / 100, travel_s=float(s["stroke_s"]),
                           overshoot_delta=float(s["overshoot_delta_k"]))


def _frame(trace, t0=None):
    import pandas as pd

    df = pd.DataFrame(trace, columns=["s", "fwd", "setpoint", "pos", "target", "inc", "ret", "mode", "scenario"])
    if t0 is not None:
        df.index = pd.to_datetime(t0 + df["s"], unit="s", utc=True)
        df.index.name = "time"
    return df


def closed_loop(hours=4.0, setpoint=35.0, preset="steady_winter", tout=2.0, radiators=None, dhw=None, sets=None,
                mixer=None, seed=0, settings=None, trace_every_s=60):
    """The identified house in closed loop.

    setpoint: a number or [(hour, value), ...]; radiators: [(from_h, to_h, "rad1"|"rad2"|"both")];
    dhw: [(at_h, minutes)]; preset: cold_slab_start | steady_winter | hot_slab | post_dhw_hot_slug;
    tout: outdoor temperature (°C)."""
    _, loop, _ = _mods()
    if isinstance(setpoint, (int, float)):
        sp = float(setpoint)
    else:
        steps = sorted((float(h) * 3600, float(v)) for h, v in setpoint)
        sp = loop.setpoint_steps(steps[0][1], steps[1:])
    plant = loop.SyntheticPlant(preset=preset, setpoint=sp, tout=float(tout), seed=int(seed),
                                rad_schedule=[(a * 3600, b * 3600, w) for a, b, w in (radiators or [])],
                                dhw_schedule=[(a * 3600, m * 60) for a, m in (dhw or [])], mixer=mixer)
    r = loop.run(plant, _sets(sets), seconds=int(hours * 3600), cfg=_cfg(settings), trace_every=int(trace_every_s))
    return {"metrics": r.metrics, "events": [(s / 3600, e) for s, e in r.events], "trace": _frame(r.trace)}


ON_CHANGE = ("floor", "r1", "r2", "dhw", "homing", "sp")      # published on change: the last value before start counts


def _recorded(start, stop, lookback="-14d"):
    """The model's signals on the 10 s grid (the controller's slow log; published values as a fallback)."""
    import numpy as np
    import pandas as pd

    from . import data
    _, _, replay = _mods()
    identify = replay.identify
    out = None
    for source in ("slow", "state"):
        names = dict(identify.SIGNALS) if source == "slow" else {**identify.SIGNALS, **identify.STATE_INPUTS}
        cont = {k: v for k, v in names.items() if k not in ON_CHANGE}
        df = data.series(list(cont.values()), start=start, stop=stop, every="10s")
        if df.empty:
            continue
        t0, t1 = df.index[0], df.index[-1]
        since = (t0 - pd.Timedelta(lookback.lstrip("-"))).isoformat()
        ch = data.series([names[k] for k in ON_CHANGE], start=since, stop=stop, every="raw")
        series = {}
        for k, v in names.items():
            src = df if k in cont else ch
            if v in src:
                col = pd.to_numeric(src[v], errors="coerce").dropna()
                series[k] = (col.index.astype("int64").to_numpy() / 1e9, col.to_numpy(float))
        g = identify.to_grid(series, t0.timestamp(), t1.timestamp() + identify.DT)
        for k in names:
            g.setdefault(k, np.full(len(g["t"]), np.nan))
        if source == "slow":
            g["pos"] = g["pos"] * 100.0
        g["source"] = source
        if identify.segments(g, min_minutes=10):
            return g
        out = out or g
    if out is None:
        raise ValueError("no recorded data of the mixer in this range")
    return out


def replay(start, stop, scenario=None, sets=None, mixer=None, settings=None, trace_every_s=60):
    """A recorded stretch replayed with the simulated controller.

    Uses the longest usable stretch (floor pump on, no DHW, no homing, one radiator state) in [start, stop].
    mixer: the plant's mixer model (default: the controller's own model for that scenario).
    The trace has the recorded forward temperature as column "recorded"."""
    import numpy as np

    fwcore, loop, replay_mod = _mods()
    identify = replay_mod.identify
    g = _recorded(start, stop)
    segs = [s for s in identify.segments(g, min_minutes=10) if scenario in (None, s["scenario"])]
    if not segs:
        return {"error": "no usable stretch in this range (floor pump on, no DHW, one radiator state, 10 min)"}
    if not np.all(np.isfinite(g["sp"][max(segs, key=lambda s: s["hours"])["slice"]])):
        segs = [s for s in segs if np.all(np.isfinite(g["sp"][s["slice"]]))] or segs
    seg = max(segs, key=lambda s: s["hours"])
    sl, sc = seg["slice"], seg["scenario"]
    all_sets = _sets(sets)
    mix = dict({k: all_sets[sc][k] for k in MODEL_FIELDS}, **(mixer or {}))
    sp = g["sp"][sl]
    if not np.all(np.isfinite(sp)):
        sp = np.where(np.isfinite(sp), sp, np.nanmedian(sp) if np.any(np.isfinite(sp)) else 35.0)
    plant = replay_mod.ReplayPlant(g["inc"][sl], g["ret"][sl], sp, fwcore.SCENARIOS.index(sc), mix,
                                   float(g["fwd"][sl.start]))
    secs = int((sl.stop - sl.start) * identify.DT)
    r = loop.run(plant, all_sets, seconds=secs, cfg=_cfg(settings), start_pos=float(g["pos"][sl.start]) / 100,
                 trace_every=int(trace_every_s))
    t0 = float(g["t"][sl.start])
    df = _frame(r.trace, t0)
    step = max(1, int(trace_every_s / identify.DT))
    df["recorded"] = g["fwd"][sl][::step][: len(df)]
    df["recorded_pos"] = g["pos"][sl][::step][: len(df)]
    return {"metrics": r.metrics, "events": [(s / 3600, e) for s, e in r.events], "trace": df,
            "stretch": {"start": str(df.index[0]), "hours": round(secs / 3600, 2), "scenario": sc,
                        "inputs": "exact (slow log)" if g["source"] == "slow" else "approximate (published values)"}}


def compare(fn, variants, **kwargs):
    """Run closed_loop or replay once per variant ({name: sets or None}); a DataFrame of the metrics."""
    import pandas as pd

    rows = {}
    for name, sets in variants.items():
        r = fn(sets=sets, **kwargs)
        rows[name] = r.get("metrics") or {"error": r.get("error")}
    return pd.DataFrame(rows).T
