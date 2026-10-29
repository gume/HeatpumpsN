"""Prototype multi-move MPC for the floor mixer (what an ESP32 L2 could run).

Internal model (only what the controller can know):
  * valve: moves at 100 % / travel_s, identified mixing law x(pos) (Step 3/4);
  * T_mix = T_ret + x * (T_inc_water - T_ret), 40 s transport delay,
    35 s forward-sensor lag (Step 4);
  * T_inc_water estimated from the lagged boiler-role sensor with a lead term.
The model runs in parallel with the plant (Smith-predictor style); the gap
between measured and modelled T_fwd is a filtered bias added to every
prediction (offset-free), and a large gap triggers an immediate re-plan.

Decision: enumerate valve move sequences - one move at t0, t0+60 s, t0+180 s,
each from STEPS - simulate the horizon for every sequence at once (numpy;
on the ESP32 a plain loop), cost = tracking error + travel penalty + soft
constraint T_fwd <= setpoint + margin, for the nominal supply AND a
"boiler refires now" scenario. Only the first move is executed (receding horizon).
"""
from __future__ import annotations

import itertools

import numpy as np

DT = 10.0
STEPS = (-0.10, -0.05, -0.02, 0.0, 0.02, 0.05, 0.10)
MOVE_AT = (0, 6, 18)          # move instants in 10 s model steps


class MixerMPC:
    def __init__(self, knots, xk, *, setpoint=35.0, horizon_s=900.0, travel_s=120.0, delay_s=40.0,
                 tau_sensor=34.64, tau_inc_sensor=75.27, lam=0.03, margin=2.5, replan_s=30.0,
                 trigger_k=0.5, pmin=0.20, pmax=0.85, scenario=True, xk_scale=1.0, start_cost=0.0):
        self.knots = np.asarray(knots) / 100.0
        self.xk = np.minimum(np.asarray(xk) * xk_scale, 1.0)   # xk_scale: model-mismatch tests
        self.sp = setpoint
        self.N = int(horizon_s / DT)
        self.vstep = DT / travel_s
        self.d_n = int(round(delay_s / DT))
        self.a_f = DT / (tau_sensor + DT)
        self.tau_inc = tau_inc_sensor
        self.lam, self.margin = lam, margin
        self.replan_s, self.trigger_k = replan_s, trigger_k
        self.pmin, self.pmax = pmin, pmax
        self.scenario = scenario
        self.start_cost = start_cost      # per motor start (relay wear), K^2
        self.seqs = np.array(list(itertools.product(STEPS, repeat=len(MOVE_AT))))   # (343, 3)
        # model state
        self.ring = None
        self.s_f = None
        self.bias = 0.0
        self.prev_inc = None
        self.slope = 0.0
        self.t_hot = None
        self.last_plan = -1e9
        self.plan_pred = None      # predicted sensor values per model step since last plan
        self.plan_k = 0
        self.target = None
        self.decisions = 0

    # -- parallel model, every 10 s ------------------------------------
    def _tinc_water(self, t_inc_sensor):
        if self.prev_inc is None:
            self.prev_inc = t_inc_sensor
        self.slope += 0.3 * ((t_inc_sensor - self.prev_inc) / DT - self.slope)
        self.prev_inc = t_inc_sensor
        return t_inc_sensor + self.tau_inc * self.slope

    def model_step(self, pos, t_inc_sensor, t_ret, meas):
        tw = self._tinc_water(t_inc_sensor)
        self.t_hot = tw if self.t_hot is None else max(tw, self.t_hot - DT / 1800.0 * 5.0)
        tmix = t_ret + np.interp(pos, self.knots, self.xk) * (tw - t_ret)
        if self.ring is None:
            self.ring = [meas] * self.d_n
            self.s_f = meas
        self.ring.append(tmix)
        self.s_f += self.a_f * (self.ring.pop(0) - self.s_f)
        self.bias += 0.1 * ((meas - self.s_f) - self.bias)
        self.tw, self.tret = tw, t_ret
        self.plan_k += 1

    # -- prediction for all sequences ---------------------------------
    def _predict(self, pos0, tw_path):
        n = len(self.seqs)
        pos = np.full(n, pos0)
        tgt = np.full(n, pos0)
        ring = np.tile(np.array(self.ring), (n, 1))
        s_f = np.full(n, self.s_f)
        out = np.empty((n, self.N))
        rp = 0
        for k in range(self.N):
            for j, at in enumerate(MOVE_AT):
                if k == at:
                    tgt = np.clip(tgt + self.seqs[:, j], self.pmin, self.pmax)
            pos = pos + np.clip(tgt - pos, -self.vstep, self.vstep)
            tmix = self.tret + np.interp(pos, self.knots, self.xk) * (tw_path[k] - self.tret)
            if self.d_n:
                old = ring[:, rp].copy()
                ring[:, rp] = tmix
                rp = (rp + 1) % self.d_n
            else:
                old = tmix
            s_f = s_f + self.a_f * (old - s_f)
            out[:, k] = s_f
        return out + self.bias

    def decide(self, t_s, pos_est, meas, supply_ok=True):
        """Called every second; returns a valve target or None (keep)."""
        if self.ring is None:
            return None
        due = t_s - self.last_plan >= self.replan_s
        if self.plan_pred is not None and self.plan_k < len(self.plan_pred):
            if abs(meas - self.plan_pred[self.plan_k]) > self.trigger_k:
                due = True                    # the world left the prediction: intervene now
        if not due:
            return None
        self.last_plan = t_s
        self.decisions += 1
        if not supply_ok:                      # nothing to gain from opening into cold supply
            self.plan_pred = None
            return pos_est
        nominal = self._predict(pos_est, np.full(self.N, self.tw))
        err = nominal - self.sp
        w = np.ones(self.N)
        w[-6:] = 5.0                           # settle by the end of the window
        J = (err ** 2 * w).mean(axis=1)
        J += self.lam * np.abs(self.seqs).sum(axis=1) * 100.0
        J += self.start_cost * (self.seqs != 0.0).sum(axis=1)
        over = np.maximum(nominal - (self.sp + self.margin), 0.0)
        J += 1000.0 * (over ** 2).max(axis=1)
        if self.scenario and self.t_hot is not None and self.t_hot > self.tw + 5.0:
            path = np.full(self.N, self.tw)
            path[6:] = self.t_hot             # boiler refires in 60 s
            hot = self._predict(pos_est, path)
            J += 1000.0 * (np.maximum(hot - (self.sp + self.margin), 0.0) ** 2).max(axis=1)
        best = int(np.argmin(J))
        self.plan_pred = nominal[best]
        self.plan_k = 0
        return float(np.clip(pos_est + self.seqs[best, 0], self.pmin, self.pmax))
