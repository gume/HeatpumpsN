"""Closed-loop backtest: recorded situations replayed with the controller's MPC core.

A situation is a recorded stretch of one scenario (floor pump on, no DHW, no
homing; identify.segments). Its supply (boiler-role sensor) and return are
replayed as recorded, the setpoint as it was; the mixer is a model (law,
transport delay, sensor lags; the firmware's model equations, as in
identify.simulate) driven by the valve position the simulated controller
commands. The controller starts at the recorded position.

The core runs once with the current parameter set and once with the
proposed one, each on two "true" mixers: the re-identified one (the best
estimate) and the current one (if the proposal were wrong). A proposal
should win on the first and not lose much on the second.

Limits, shown with the result: the return is replayed as recorded (in the
house it follows the forward temperature over hours), and the controller's
settings around the core (minimum opening, ceiling, stroke) are assumptions.
"""
from __future__ import annotations

import math

import numpy as np

from .. import identify
from . import fwcore
from .loop import LoopConfig, run

DT = identify.DT


class ReplayPlant:
    """Recorded supply and return, a modelled mixer (step by step as the firmware's model)."""

    def __init__(self, inc, ret, sp, scenario: int, mixer: dict, fwd0: float):
        self.inc, self.ret, self.sp_arr = np.asarray(inc, float), np.asarray(ret, float), np.asarray(sp, float)
        self.n = len(self.inc)
        self.tw = identify.supply_water(self.inc, mixer["tau_inc_s"])
        self.knots, self.xk = np.asarray(mixer["knots_pct"], float), np.asarray(mixer["xk"], float)
        self.cells = min(int(round(mixer["delay_s"] / DT)), identify.MAX_DELAY_CELLS)
        self.a = DT / (mixer["tau_fwd_s"] + DT)
        self.ring = [fwd0] * max(self.cells, 1)
        self.rp = 0
        self.y = fwd0
        self.sc = scenario
        self.k = -1

    def _k(self, s):
        return min(int(s // DT), self.n - 1)

    def setpoint(self, s):
        return float(self.sp_arr[self._k(s)])

    def scenario(self, s):
        return self.sc

    def dhw(self, s):
        return False

    def step(self, pos_pct, s):
        k = self._k(s)
        if k != self.k:
            self.k = k
            x = float(np.interp(pos_pct, self.knots, self.xk))
            tmix = self.ret[k] + x * (self.tw[k] - self.ret[k])
            if self.cells > 0:
                delayed = self.ring[self.rp]
                self.ring[self.rp] = tmix
                self.rp = (self.rp + 1) % self.cells
            else:
                delayed = tmix
            self.y += self.a * (delayed - self.y)
        return {"t_inc": float(self.inc[k]), "t_ret": float(self.ret[k]), "t_fwd": self.y}


def situations(g: dict, scenario: str, window_h: float = 2.0, max_count: int = 8, min_minutes: float = 30) -> list:
    """The most recent recorded stretches of a scenario with a known setpoint, cut into windows."""
    out = []
    segs = sorted((s for s in identify.segments(g) if s["scenario"] == scenario), key=lambda s: -s["start"])
    w = int(window_h * 3600 / DT)
    sp = g.get("sp")
    if sp is None:
        return []
    for s in segs:
        a, stop = s["slice"].start, s["slice"].stop
        while stop > a and len(out) < max_count:          # windows from the end of the stretch back
            start = max(a, stop - w)
            if (stop - start) * DT >= min_minutes * 60 and np.all(np.isfinite(sp[start:stop])):
                out.append(slice(start, stop))
            stop = start
    return sorted(out, key=lambda x: x.start)


def _aggregate(runs: list[dict]) -> dict:
    h = sum(r["hours"] for r in runs)
    if not h:
        return {}
    wsum = lambda key: sum(r[key] * r["hours"] for r in runs if r[key] is not None)  # noqa: E731
    sq = sum((r["rmse_k"] ** 2) * r["hours"] for r in runs if r["rmse_k"] is not None)
    reach = [r["time_to_setpoint_s"] for r in runs if r["started_off"] and r["time_to_setpoint_s"] is not None]
    return dict(hours=round(h, 2), rmse_k=round(math.sqrt(sq / h), 3), overshoot_k=max(r["overshoot_k"] for r in runs),
                travel_pct_h=round(wsum("travel_pct_h") / h, 1), starts_h=round(wsum("starts_h") / h, 1),
                reversals_h=round(wsum("reversals_h") / h, 1),
                time_to_setpoint_s=round(float(np.median(reach))) if reach else None,
                heat_ups=sum(1 for r in runs if r["started_off"]),
                never_reached=sum(1 for r in runs if r["started_off"] and r["time_to_setpoint_s"] is None),
                fallbacks=sum(r["fallbacks"] for r in runs), anomalies=sum(r["anomalies"] for r in runs))


def _verdict(cur: dict, new: dict) -> str:
    if not cur or not new:
        return "no data"
    d_rmse = new["rmse_k"] - cur["rmse_k"]
    worse_moves = new["travel_pct_h"] > 1.3 * cur["travel_pct_h"] + 5 or new["starts_h"] > 1.3 * cur["starts_h"] + 1
    if new["fallbacks"] > cur["fallbacks"] or new["anomalies"] > cur["anomalies"]:
        return "worse (more fallbacks)"
    if d_rmse <= -0.05 and not worse_moves:
        return "better"
    if d_rmse >= 0.1 or (worse_moves and d_rmse > -0.05):
        return "worse"
    return "similar"


def backtest(g: dict, scenario: str, current: dict, proposed: dict, identified: dict,
             cfg: LoopConfig | None = None, window_h: float = 2.0, max_count: int = 8,
             trace_every: int = 60) -> dict:
    """current/proposed: whole parameter sets (the controller runs them); identified: the re-identified mixer
    (model fields). Returns metrics per plant ("identified", "current") and controller, and a verdict."""
    c = cfg or LoopConfig(pmin=0.40, pmax=0.85, travel_s=145.0, overshoot_delta=3.0)
    sc = fwcore.SCENARIOS.index(scenario)
    sits = situations(g, scenario, window_h, max_count)
    if not sits:
        return {"status": "no recorded situations with a known setpoint", "situations": 0}
    plants = {"identified": identified, "current": {k: current[k] for k in identify.MODEL_FIELDS}}
    sets = {"current": current, "proposed": proposed}
    out = {"status": "done", "situations": len(sits), "assumed": {
        "min_open_pct": round(c.pmin * 100), "ceiling_pct": round(c.pmax * 100), "stroke_s": c.travel_s,
        "overshoot_delta_k": c.overshoot_delta, "return": "as recorded"}, "plants": {}}
    rows = [{"start": float(g["t"][sl.start]), "hours": round((sl.stop - sl.start) * DT / 3600, 2)} for sl in sits]
    traces = {}
    for pname, mixer in plants.items():
        res = {}
        for sname, pset in sets.items():
            runs = []
            for i, sl in enumerate(sits):
                fwd0 = float(g["fwd"][sl.start])
                plant = ReplayPlant(g["inc"][sl], g["ret"][sl], g["sp"][sl], sc, mixer, fwd0)
                secs = int((sl.stop - sl.start) * DT)
                r = run(plant, {scenario: pset}, seconds=secs, cfg=c, start_pos=float(g["pos"][sl.start]) / 100,
                        trace_every=trace_every if pname == "identified" else 0)
                m = dict(r.metrics, hours=secs / 3600, started_off=abs(fwd0 - float(g["sp"][sl.start])) > c.reach_k)
                runs.append(m)
                if pname == "identified":
                    rows[i][sname] = {k: m[k] for k in ("rmse_k", "overshoot_k", "travel_pct_h", "starts_h",
                                                        "fallbacks")}
                    rows[i][sname]["events"] = [e for _, e in r.events if not e.startswith("setpoint")][:6]
                    traces[(i, sname)] = [[row[0], row[1], row[2], row[3]] for row in r.trace]   # t, fwd, sp, pos
            res[sname] = _aggregate(runs)
        res["verdict"] = _verdict(res["current"], res["proposed"])
        out["plants"][pname] = res
    out["rows"] = rows
    wins = sum(1 for r in rows if (r["proposed"]["rmse_k"] or 0) < (r["current"]["rmse_k"] or 0) - 0.02)
    out["wins"] = wins
    # the example: the situation where the two controllers differ most
    i = max(range(len(rows)), key=lambda j: abs((rows[j]["proposed"]["rmse_k"] or 0) - (rows[j]["current"]["rmse_k"] or 0)))
    sl = sits[i]
    out["example"] = {"index": i, "t0": rows[i]["start"], "measured": [
        round(float(v), 2) for v in g["fwd"][sl][:: max(1, int(trace_every / DT))]],
        "current": traces[(i, "current")], "proposed": traces[(i, "proposed")]}
    v1, v2 = out["plants"]["identified"]["verdict"], out["plants"]["current"]["verdict"]
    out["verdict"] = ("recommended" if v1 == "better" and not v2.startswith("worse") and 2 * wins >= len(rows) else
                      "not recommended" if v1.startswith("worse") or v2.startswith("worse (") else "neutral")
    return out
