"""Offline closed loop: firmware mixer control (l2_control.cpp) + identified plant.

Replicates `service_mixer_control` (normal floor-demand path, overshoot latch),
L1 motor execution (deadband_pos, dead reckoning) and the HIL sensor path
(10 s plant ticks, slew-limited injection at 1 s, 1 s gradient as in l0_io).
Prints valve travel, reversals and forward-temperature tracking per setting.

    .venv/bin/python -m floor_simulator.tools.mixer_tuning_sim   (from repo root)
"""
from __future__ import annotations

import argparse
import math

from floor_simulator.app.boiler import BoilerEmulator
from floor_simulator.app.plant import DT, PlantEngine, PlantInputs, PlantParams

BASE = dict(kp=0.5, ki=0.02, delay_s=120.0, deadband_temp=2.0, deadband_pos=0.02,
            ff_boiler=0.47, ff_return=0.87, mix_ff_offset=6.75, rad_coupling_ff=-1.5,
            ff_scale=1.0, tick_s=2.0, travel_s=120.0, min_pos=0.20, max_pos=0.85,
            ovs_delta=3.0, ovs_recovery=1.0, ovs_target=0.40, max_step=0.10,
            sensor_res=0.0, grad_filter_s=0.0,
            # ctrl="model": steady-state inverse of the identified mixing law
            ctrl="firmware", avg_s=300.0, trim_tau_s=1800.0, move_db=0.05, supply_margin=3.0,
            fast_up=False, mpc=None,
            # radiator pump schedule [(start_s, end_s, "rad1"|"rad2"|"both")], valve fault
            # injection (time_s, dead-reckoning offset 0..1): the controller's position
            # estimate jumps while the real valve does not (lost steps, slipped coupling)
            rad_schedule=None, pos_fault=None,
            # setpoint steps [(time_s, new_setpoint)]
            sp_schedule=None)


def inverse_law(knots, xk, x):
    """Smallest valve position [0..1] whose mixing fraction reaches x."""
    if x <= xk[0]:
        return knots[0] / 100
    for j in range(len(knots) - 1):
        if xk[j + 1] >= x and xk[j + 1] > xk[j]:
            w = (x - xk[j]) / (xk[j + 1] - xk[j])
            return clamp((knots[j] + w * (knots[j + 1] - knots[j])) / 100, knots[j] / 100, knots[j + 1] / 100)
    return knots[-1] / 100


def clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def run(cfg: dict, hours=8.0, setpoint=35.0, seed=1, preset="steady_winter", tout=2.0, trace=None):
    c = dict(BASE, **cfg)
    P = PlantParams.load()
    plant = PlantEngine(P, act_mode=0)
    from floor_simulator.app.engine import PRESETS
    p = PRESETS[preset]
    plant.set_state(t_inc=p["t_inc"], t_fwd=p["t_fwd"], t_ret=p["t_ret"], t_slab=p["t_slab"], t_room=p["t_room"])
    boiler = BoilerEmulator(P.boiler, seed=seed)
    boiler.tb = p["t_boiler"]
    pos = 0.0           # physical valve 0..1
    pos_est = 0.0       # controller dead reckoning (exact here: same travel time)
    target = 0.0
    motor = 0
    integ = 0.0
    ovs = False
    sens = {k: v for k, v in zip(("boiler", "return", "mix"),
                                 (plant.ts, plant.tr, plant.s_f))}
    goal = dict(sens)
    rate = {k: 0.0 for k in sens}
    prev_mix = sens["mix"]
    grad_f = 0.0
    travel = 0.0
    reversals = 0
    last_dir = 0
    starts = 0
    sq = n = 0
    sq_ok = n_ok = 0
    fwd_max = -1e9
    avg_inc = sens["boiler"]
    avg_ret = sens["return"]
    trim = 0.0
    mpc = None
    if c["ctrl"] == "mpc":
        from floor_simulator.tools.mpc_proto import MixerMPC
        mpc = MixerMPC(P.knots, P.xk, setpoint=setpoint, pmin=c["min_pos"], pmax=c["max_pos"],
                       **(c["mpc"] or {}))
    fw = None
    scenario = 0
    home_until = None
    events = []
    if c["ctrl"] == "fw_mpc":
        from floor_simulator.tools.fw_mpc import FwMpc
        fw = FwMpc()
        fw.set_default(0)
    n_s = int(hours * 3600)
    for s in range(n_s):
        r1 = r2 = False
        for a0, a1, which in (c["rad_schedule"] or []):
            if a0 <= s < a1:
                r1 |= which in ("rad1", "both")
                r2 |= which in ("rad2", "both")
        for ts, new_sp in (c["sp_schedule"] or []):
            if s == int(ts):
                setpoint = new_sp
                events.append((s, f"setpoint {new_sp:g}"))
        if c["pos_fault"] and s == int(c["pos_fault"][0]):
            pos_est = clamp(pos_est + c["pos_fault"][1], 0.0, 1.0)
            events.append((s, "fault"))
        # ---- plant tick every 10 s
        if s % int(DT) == 0:
            b = boiler.step(True, plant.tro + 0.49)
            o = plant.step(PlantInputs(b.tb, b.pump_pct, b.burner_pct, b.dhw, r1, r2, True, pos * 100, tout))
            goal = {"boiler": o["t_inc_sensor"], "return": o["t_ret"], "mix": o["t_fwd_sensor"]}
            for k in goal:
                rate[k] = min(abs(goal[k] - sens[k]) / DT, 0.15)
        for k in sens:  # injector
            d = goal[k] - sens[k]
            sens[k] = goal[k] if abs(d) <= rate[k] else sens[k] + math.copysign(rate[k], d)
        meas = sens["mix"]
        if c["sensor_res"] > 0:
            meas = round(meas / c["sensor_res"]) * c["sensor_res"]
        grad = meas - prev_mix
        prev_mix = meas
        if c["grad_filter_s"] > 0:
            grad_f += (grad - grad_f) / c["grad_filter_s"]
            grad = grad_f
        a = 1.0 / c["avg_s"]
        avg_inc += a * (sens["boiler"] - avg_inc)
        avg_ret += a * (sens["return"] - avg_ret)
        if s > 1800:
            sq += (meas - setpoint) ** 2
            n += 1
            fwd_max = max(fwd_max, meas)
            if sens["boiler"] >= setpoint + 5.0:
                sq_ok += (meas - setpoint) ** 2
                n_ok += 1
        if mpc is not None:
            if s % int(DT) == 0:
                mpc.model_step(pos_est, sens["boiler"], sens["return"], meas)
            if not ovs:
                t = mpc.decide(s, pos_est, meas, supply_ok=sens["boiler"] >= setpoint + c["supply_margin"])
                if t is not None:
                    target = t
        # ---- L2 every tick_s
        if fw is not None:
            if s % int(c["tick_s"]) == 0:
                if not ovs and meas >= setpoint + c["ovs_delta"]:
                    ovs = True
                elif ovs and meas <= setpoint - c["ovs_recovery"]:
                    ovs = False
                sc = (1 if r1 else 0) + (2 if r2 else 0)
                if sc != scenario:
                    scenario = sc
                    fw.set_default(sc)
                if home_until is None:
                    kind, t = fw.tick(float(s), pos_est, target, sens["boiler"], sens["return"], meas,
                                      setpoint, c["min_pos"], c["max_pos"], c["travel_s"], c["ovs_delta"],
                                      scenario)
                    if kind == 1:
                        target = t
                    elif kind == 2:
                        home_until = s + c["travel_s"] + 10
                        target = 0.0
                        events.append((s, "home:" + fw.status()["anomaly"]))
            if home_until is not None and s >= home_until:
                home_until = None
                pos_est = 0.0
                fw.on_homed(float(s))
                events.append((s, "homed"))
        elif mpc is not None:
            if not ovs and meas >= setpoint + c["ovs_delta"]:
                ovs, target = True, c["ovs_target"]
            elif ovs and meas <= setpoint - c["ovs_recovery"]:
                ovs = False
        elif s % int(c["tick_s"]) == 0:
            if not ovs and meas >= setpoint + c["ovs_delta"]:
                ovs = True
            elif ovs and meas <= setpoint - c["ovs_recovery"]:
                ovs = False
            if ovs:
                target = c["ovs_target"]
            elif c["ctrl"] == "model":
                # predicted steady state: which position gives the setpoint with the
                # averaged supply and return? Hold while the supply cannot deliver.
                if sens["boiler"] >= setpoint + c["supply_margin"] and avg_inc > avg_ret + 2.0:
                    # fast_up: a hotter supply is believed at once (closes before the
                    # surge arrives), a cooler one only on average (no chasing ripple)
                    inc = max(avg_inc, sens["boiler"]) if c["fast_up"] else avg_inc
                    x = (setpoint - avg_ret) / (inc - avg_ret)
                    trim = clamp(trim + c["tick_s"] / c["trim_tau_s"] * (setpoint - meas) / (avg_inc - avg_ret),
                                 -0.15, 0.15)
                    want_pos = clamp(inverse_law(P.knots, P.xk, clamp(x + trim, 0.0, 1.0)), c["min_pos"], c["max_pos"])
                    if abs(want_pos - pos_est) > c["move_db"]:
                        target = want_pos
            else:
                boil, ret = sens["boiler"], sens["return"]
                hot = c["ff_boiler"] * boil - c["ff_return"] * ret
                ff = 0.0
                if abs(hot) > 1.0:
                    ff = (setpoint - c["mix_ff_offset"] - c["ff_return"] * ret) / hot
                ff = clamp(ff, 0.0, 1.0) * c["ff_scale"]
                err = setpoint - (meas + grad * c["delay_s"])
                if abs(err) > c["deadband_temp"]:
                    integ = clamp(integ + c["ki"] * err, -0.35, 0.35)
                    t = ff + c["kp"] * err + integ
                    t = clamp(t, pos_est - c["max_step"], pos_est + c["max_step"])
                    target = clamp(max(t, c["min_pos"]), 0.0, c["max_pos"])
                else:
                    target = clamp(max(min(pos_est, c["max_pos"]), c["min_pos"]), 0.0, 1.0)
        if trace is not None and s % 10 == 0:
            mode = fw.status()["mode"] if fw is not None else ""
            trace.append((s, meas, pos * 100, sens["boiler"], sens["return"], target * 100, mode,
                          pos_est * 100, int(r1) + 2 * int(r2)))
        # ---- L1 motor (homing: free-run close, ignores the target)
        e = target - pos_est
        want = 0 if abs(e) <= c["deadband_pos"] else (1 if e > 0 else -1)
        if home_until is not None:
            want = -1
        if want and want != motor:
            starts += 1
            if last_dir and want != last_dir:
                reversals += 1
            last_dir = want
        motor = want
        if motor:
            step = motor / c["travel_s"]
            newp = clamp(pos + step, 0.0, 1.0)
            travel += abs(newp - pos)
            pos = newp
            pos_est = clamp(pos_est + step, 0.0, 1.0)
    h = hours
    if fw is not None:
        st = fw.status()
        return dict(travel_pct_h=travel * 100 / h, starts_h=starts / h, reversals_h=reversals / h,
                    rmse=math.sqrt(sq / max(n, 1)), rmse_ok=math.sqrt(sq_ok / max(n_ok, 1)), fwd_max=fwd_max,
                    mode=st["mode"], anomalies=st["anomalies"], plans=st["plans"], triggered=st["triggered"],
                    events=events)
    if mpc is not None:
        mpc_decisions = mpc.decisions
    return dict(travel_pct_h=travel * 100 / h, starts_h=starts / h, reversals_h=reversals / h,
                rmse=math.sqrt(sq / max(n, 1)), rmse_ok=math.sqrt(sq_ok / max(n_ok, 1)), fwd_max=fwd_max)


CANDIDATES = {
    "current (kp .5 ki .02 delay 120 db 2)": {},
    "delay_s 0": dict(delay_s=0.0),
    "delay_s 0, overshoot delta 2 -> 25 %": dict(delay_s=0.0, ovs_delta=2.0, ovs_target=0.25),
    "delay_s 30": dict(delay_s=30.0),
    "delay_s 30, kp .2 ki .005": dict(delay_s=30.0, kp=0.2, ki=0.005),
    "delay_s 30, kp .2 ki .005, ovs 2 -> 25 %": dict(delay_s=30.0, kp=0.2, ki=0.005, ovs_delta=2.0, ovs_target=0.25),
    "delay_s 60, kp .1 ki .002": dict(delay_s=60.0, kp=0.1, ki=0.002),
    "kp .05 ki .002 delay 0": dict(kp=0.05, ki=0.002, delay_s=0.0),
    "MODEL (fw change)": dict(ctrl="model"),
    "MODEL + fast_up (fw change)": dict(ctrl="model", fast_up=True),
    "MODEL + fast_up, avg 600 s (fw change)": dict(ctrl="model", fast_up=True, avg_s=600.0),
}

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=8.0)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--sensor-res", type=float, default=0.0, help="e.g. 0.0625 for DS18B20")
    a = ap.parse_args()
    print(f"{'setting':42s} {'travel %/h':>10s} {'starts/h':>9s} {'rev/h':>7s} {'RMSE':>6s} {'RMSEok':>6s} {'max':>5s}")
    for name, cfg in CANDIDATES.items():
        rs = [run(dict(cfg, sensor_res=a.sensor_res), hours=a.hours, seed=s) for s in range(a.seeds)]
        avg = {k: sum(r[k] for r in rs) / len(rs) for k in rs[0]}
        print(f"{name:42s} {avg['travel_pct_h']:10.0f} {avg['starts_h']:9.0f} {avg['reversals_h']:7.0f}"
              f" {avg['rmse']:6.2f} {avg['rmse_ok']:6.2f} {avg['fwd_max']:5.1f}")
