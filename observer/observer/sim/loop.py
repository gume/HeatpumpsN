"""Closed loop: the firmware MPC core drives a simulated mixer valve on a plant.

Replicates what the controller does around the core (as floor_simulator's
mixer_tuning_sim does): the core is ticked every 2 s with the dead-reckoned
valve position, a target becomes motor movement at the full-stroke speed
(with the position deadband), a homing command drives the valve closed and
re-zeroes it, the radiator pumps select the parameter set, and during a DHW
charge the core is not ticked. The plant steps every 10 s.

A plant is any object with ``step(pos_pct, t_s) -> {"t_inc", "t_ret", "t_fwd"}``
(sensor values) plus ``scenario(t_s)``, ``dhw(t_s)`` and ``setpoint(t_s)``;
``SyntheticPlant`` is the identified house (plant.py + boiler.py).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import fwcore
from .boiler import BoilerEmulator
from .plant import DT, PlantEngine, PlantInputs, PlantParams

PRESETS = {     # floor_simulator/app/engine.py
    "cold_slab_start": dict(t_inc=22.0, t_fwd=19.0, t_ret=19.0, t_slab=18.5, t_room=18.0, t_boiler=22.0),
    "steady_winter": dict(t_inc=60.0, t_fwd=33.0, t_ret=27.5, t_slab=26.0, t_room=21.0, t_boiler=68.7),
    "hot_slab": dict(t_inc=55.0, t_fwd=33.0, t_ret=31.0, t_slab=30.0, t_room=23.0, t_boiler=65.0),
    "post_dhw_hot_slug": dict(t_inc=76.0, t_fwd=30.0, t_ret=27.0, t_slab=26.0, t_room=21.0, t_boiler=81.5,
                              park_diverter=True),
}


@dataclass
class LoopConfig:
    """The controller's settings around the core (tuning and L1)."""
    pmin: float = 0.20              # mixer minimum opening (fraction)
    pmax: float = 0.85              # ceiling
    travel_s: float = 120.0         # full stroke
    overshoot_delta: float = 3.0
    tick_s: int = 2                 # L2 period
    deadband_pos: float = 0.02      # L1 position deadband
    sensor_res: float = 0.0625      # DS18B20 resolution
    reach_k: float = 0.5            # "at the setpoint": within this band


def _q(v, res):
    return round(v / res) * res if res > 0 else v


@dataclass
class Result:
    seconds: int
    trace: list = field(default_factory=list)       # (t_s, fwd, setpoint, pos %, target %, inc, ret, mode, scenario)
    events: list = field(default_factory=list)      # (t_s, text)
    metrics: dict = field(default_factory=dict)


def run(plant, sets: dict | None = None, seconds: int = 4 * 3600, cfg: LoopConfig | None = None,
        start_pos: float = 0.0, trace_every: int = 10) -> Result:
    """One closed-loop run; ``sets`` are the parameter sets ({scenario: fields}) the core runs with."""
    c = cfg or LoopConfig()
    core = fwcore.Core(sets)
    core.request_mode(fwcore.MODE_MPC, 0.0)
    pos = pos_est = target = start_pos      # physical, dead-reckoned, commanded (0..1)
    motor = last_dir = 0
    travel = 0.0
    starts = reversals = 0
    home_until = None
    sens = plant.step(pos * 100, 0)
    res = Result(seconds)
    sp_prev = None
    step_t = 0                  # last setpoint change (or the start)
    reached = []                # seconds from each setpoint change to the band
    waiting = True
    over = 0.0
    sq = n = 0
    sq_ok = n_ok = 0            # only while the supply can deliver (inc >= setpoint + 5 K)
    mode_prev = "mpc"
    fallbacks = 0
    mpc_s = 0
    for s in range(seconds):
        if s and s % int(DT) == 0:
            sens = plant.step(pos * 100, s)
        sp = plant.setpoint(s)
        if sp != sp_prev:
            if sp_prev is not None:
                res.events.append((s, f"setpoint {sp:g}"))
                if waiting:
                    reached.append(None)
            sp_prev, step_t, waiting = sp, s, True
        fwd = _q(sens["t_fwd"], c.sensor_res)
        inc = _q(sens["t_inc"], c.sensor_res)
        ret = _q(sens["t_ret"], c.sensor_res)
        if waiting and abs(fwd - sp) <= c.reach_k:
            reached.append(s - step_t)
            waiting = False
        if not waiting:
            over = max(over, fwd - sp)
            sq += (fwd - sp) ** 2
            n += 1
            if inc >= sp + 5.0:
                sq_ok += (fwd - sp) ** 2
                n_ok += 1
        scenario = plant.scenario(s)
        if s % c.tick_s == 0:
            core.use(scenario)
            if home_until is None and not plant.dhw(s):
                kind, t = core.tick(float(s), pos_est, target, inc, ret, fwd, sp, c.pmin, c.pmax, c.travel_s,
                                    c.overshoot_delta)
                if kind == fwcore.CMD_TARGET:
                    target = t
                elif kind == fwcore.CMD_HOME:
                    home_until = s + c.travel_s + 10
                    target = 0.0
                    res.events.append((s, "home: " + core.status()["anomaly"]))
            if home_until is not None and s >= home_until:
                home_until = None
                pos_est = 0.0
                core.on_homed(float(s))
                res.events.append((s, "homed"))
            mode = core.status()["mode"]
            if mode != mode_prev:
                if mode in ("homing", "cautious") and mode_prev == "mpc":
                    fallbacks += 1
                res.events.append((s, f"mode {mode}"))
                mode_prev = mode
        if mode_prev == "mpc":
            mpc_s += 1
        if trace_every and s % trace_every == 0:
            res.trace.append((s, round(fwd, 3), sp, round(pos * 100, 2), round(target * 100, 2), round(inc, 2),
                              round(ret, 2), mode_prev, scenario))
        # L1 motor (homing: free-run close)
        e = target - pos_est
        want = 0 if abs(e) <= c.deadband_pos else (1 if e > 0 else -1)
        if home_until is not None:
            want = -1
        if want and want != motor:
            starts += 1
            if last_dir and want != last_dir:
                reversals += 1
            last_dir = want
        motor = want
        if motor:
            step = motor / c.travel_s
            newp = min(max(pos + step, 0.0), 1.0)
            travel += abs(newp - pos)
            pos = newp
            pos_est = min(max(pos_est + step, 0.0), 1.0)
    h = seconds / 3600
    st = core.status()
    done = [r for r in reached if r is not None]
    res.metrics = dict(
        time_to_setpoint_s=done[0] if reached and reached[0] is not None else None,
        mean_time_to_setpoint_s=round(sum(done) / len(done)) if done else None,
        setpoints=len(reached) + (1 if waiting else 0),
        never_reached=sum(1 for r in reached if r is None) + (1 if waiting else 0),
        overshoot_k=round(over, 2),
        rmse_k=round(math.sqrt(sq / n), 3) if n else None,
        rmse_supplied_k=round(math.sqrt(sq_ok / n_ok), 3) if n_ok else None,
        travel_pct_h=round(travel * 100 / h, 1),
        starts_h=round(starts / h, 1),
        reversals_h=round(reversals / h, 1),
        fallbacks=fallbacks,
        anomalies=st["anomalies"],
        mpc_share=round(mpc_s / seconds, 3),
        final_mode=st["mode"],
    )
    return res


class SyntheticPlant:
    """The identified house: coupled HX + mixer + slab + room (plant.py) with the observed boiler (boiler.py).

    ``mixer`` replaces the plant's identified mixer (law, delay, sensor lag) with
    another one, e.g. what the observer identified from live data.
    """

    def __init__(self, preset: str = "steady_winter", setpoint=35.0, tout: float = 2.0, seed: int = 0,
                 rad_schedule=None, dhw_schedule=None, mixer: dict | None = None,
                 params: PlantParams | None = None):
        P = params or PlantParams.load()
        if mixer:
            P = PlantParams(**{**P.__dict__})
            if mixer.get("knots_pct") is not None:
                P.knots, P.xk = list(mixer["knots_pct"]), list(mixer["xk"])
            if mixer.get("delay_s") is not None or mixer.get("tau_fwd_s") is not None:
                P.mix = [round(mixer.get("delay_s", P.mix[0] * DT) / DT), mixer.get("tau_fwd_s", P.mix[1]),
                         P.mix[2]]
        self.P = P
        p = PRESETS[preset]
        self.plant = PlantEngine(P, act_mode=0)
        self.plant.set_state(t_inc=p["t_inc"], t_fwd=p["t_fwd"], t_ret=p["t_ret"], t_slab=p["t_slab"],
                             t_room=p["t_room"], park_diverter=p.get("park_diverter", False))
        self.boiler = BoilerEmulator(P.boiler, seed=seed)
        self.boiler.tb = p["t_boiler"]
        self.sp = setpoint if callable(setpoint) else (lambda s, v=setpoint: v)
        self.tout = tout
        self.rad = rad_schedule or []
        self.dhw_sched = dhw_schedule or []
        self._dhw_started = set()

    def setpoint(self, s):
        return self.sp(s)

    def scenario(self, s):
        r1 = r2 = False
        for a0, a1, which in self.rad:
            if a0 <= s < a1:
                r1 |= which in ("rad1", "both")
                r2 |= which in ("rad2", "both")
        return (1 if r1 else 0) + (2 if r2 else 0)

    def dhw(self, s):
        return self.boiler.dhw_on

    def step(self, pos_pct, s):
        for d0, dur in self.dhw_sched:
            if s >= d0 and d0 not in self._dhw_started:
                self._dhw_started.add(d0)
                self.boiler.dhw_start(dur)
        sc = self.scenario(s)
        b = self.boiler.step(True, self.plant.tro + 0.49)
        o = self.plant.step(PlantInputs(b.tb, b.pump_pct, b.burner_pct, b.dhw, bool(sc & 1), bool(sc & 2), True,
                                        pos_pct, self.tout))
        return {"t_inc": o["t_inc_sensor"], "t_ret": o["t_ret"], "t_fwd": o["t_fwd_sensor"]}


def setpoint_steps(first: float, steps) -> callable:
    """[(t_s, value), ...] -> setpoint(t_s)."""
    steps = sorted(steps)

    def f(s):
        v = first
        for t, x in steps:
            if s >= t:
                v = x
        return v
    return f
