"""Step 2 v7.1 - HX outlet (T_incoming) model library.

v7.1 = v7.0 plus an input-validity array (EMS telemetry outages).

Discrete-time grey-box model at DT = 10 s.  States:

  T   water at the T_incoming sensor (HX secondary outlet / fork)   [degC]
  Tp  primary water entering the HX (lagged boiler supply)          [degC]
  H   HX body + stagnant water around the sensor (heat store)       [degC]
  R   radiator-loop water returning to the HX (latent)              [degC]

Inputs per step: tb (boiler flow sensor), tret (floor return), troom,
pos (mixer position, %), bp (burner %), pmod (boiler primary pump %),
ww (DHW divert flag) and the pump booleans p1, p2 (radiators), p3 (floor).

Secondary flow through the HX/fork (relative units, floor pump with the
mixer fully open = 1):
    F_f = p3 * pos/100           floor branch draws hot water through the fork
    F_r = q1 * p1 + q2 * p2      radiator circuits
    F   = F_f + F_r

Diverter memory `div` (boiler-internal 3-way valve): set to 1 while ww = 1,
cleared by the next space-heating burner firing (bp > 0 and ww = 0).

Modes
  ACT   pmod > 0, ww = 0, div = 0 (space-heating primary flow):
          primary pipe: plug-flow ring of n_pipe = round(L_pipe/DT) cells;
          each ACT step pops the oldest cell and pushes tb
          Tp -> popped cell                                 (tau_p)
          T  -> tret + eps_r (Tp - tret)                    (tau_hx)
          H  -> tret + k_hc (T - tret)                      (tau_hc)
        eps_r by secondary regime: A floor only, B floor + radiator(s),
        C radiator(s) only, N no secondary pump.
  HOLD  ww = 1 and the divert started <= dhw_hold_s ago: all states held.
  OFF   every other sample (primary flow absent or diverted to DHW):
          T  -> Tmix  at rate F / V            (flushing by secondary flow)
          T  -> H     with tau_fn              (stagnant exchange, parallel)
        Tmix = (F_f tret + F_r R) / F.
        H  -> amb + k_b (tb - amb)  (tau_h); k_b = k_b_park while div = 1.
  R (all modes except HOLD): R -> T with tau_rc while a radiator pump runs
  and the primary is active, R -> amb with tau_rd otherwise.

Primary pipe outside ACT: every cell relaxes toward T with tau_pipe (the
stagnant primary water equilibrates with the HX side).
Diverter clearing requires bp > 0 at least DIV_CLEAR_S after the ww falling
edge, so a burner tail at the end of a DHW cycle does not count as a CH
firing.  After clearing, the diverter needs div_travel_s to reach the CH
position; until then ACT is not entered (the sample is treated as OFF).

Sensor: the reported temperature Ts follows T with the thermowell lag
tau_s (Ts is the model output compared with mixer_incoming_temperature).

Telemetry validity: `valid[k]` = False marks an input outage (boiler
telemetry silent).  All states are held during an outage; at the first valid
sample afterwards every temperature state (T, Ts, Tp, H, R, pipe cells) is
re-anchored to the measured T_incoming.  In the HIL `valid` is always True.

amb = troom + d_amb.  Tp = T outside ACT.  The explicit update uses
alpha = DT/(tau + DT) for every first-order step, and for the combined OFF
step  T += (DT*F/V * (Tmix - T) + DT/tau_fn * (H - T)) / (1 + DT*F/V + DT/tau_fn),
which is the implicit-Euler form and is unconditionally stable.
"""
from __future__ import annotations

import numpy as np
from numba import njit

DT = 10.0

PARAM_NAMES = ("tau_p", "tau_hx", "eps_A", "eps_B", "eps_C", "eps_N",
               "tau_hc", "k_hc", "V", "q1", "q2", "tau_fn", "tau_h", "k_b",
               "k_b_park", "tau_rc", "tau_rd", "dhw_hold_s", "d_amb",
               "L_pipe", "tau_pipe", "div_travel_s", "tau_s")
MAX_PIPE_N = 64          # ring capacity (identical in the C++ export)
DIV_CLEAR_S = 30.0

MODE_ACT, MODE_OFF_FLOW, MODE_OFF_STAG, MODE_DHW, MODE_HOLD, MODE_INVALID = 0, 1, 2, 3, 4, 5
MODE_NAMES = ("active", "off_flow", "off_stagnant", "dhw", "dhw_hold", "input_outage")


@njit(cache=True)
def simulate(par, tb, tret, troom, pos, bp, pmod, ww, p1, p2, p3,
             T0, H0, R0, anchor, tinc_meas, valid):
    """Simulate one episode.  Returns Ts (sensor), H, R, Tp and the mode.

    `anchor` (index): after the update at that index T is overwritten by the
    measured T_incoming (and Tp outside ACT); < 0 disables.
    """
    tau_p = par[0]; tau_hx = par[1]
    eA = par[2]; eB = par[3]; eC = par[4]; eN = par[5]
    tau_hc = par[6]; k_hc = par[7]; V = par[8]; q1 = par[9]; q2 = par[10]
    tau_fn = par[11]; tau_h = par[12]; k_b = par[13]; k_b_park = par[14]
    tau_rc = par[15]; tau_rd = par[16]; dhw_hold = par[17]; d_amb = par[18]
    n_pipe = int(round(par[19] / DT))
    if n_pipe < 1:
        n_pipe = 1
    if n_pipe > MAX_PIPE_N:
        n_pipe = MAX_PIPE_N
    a_pipe = DT / (par[20] + DT)
    ring = np.empty(MAX_PIPE_N)
    for i in range(n_pipe):
        ring[i] = T0
    rpos = 0
    since_ww = 1e9
    div_travel = par[21]
    since_clear = 1e9
    a_s = DT / (par[22] + DT)
    ts = T0
    a_p = DT / (tau_p + DT)
    a_hx = DT / (tau_hx + DT)
    a_hc = DT / (tau_hc + DT)
    a_h = DT / (tau_h + DT)
    a_rc = DT / (tau_rc + DT)
    a_rd = DT / (tau_rd + DT)
    g_fn = DT / tau_fn
    n = tb.shape[0]
    Ts = np.empty(n)
    Ts[0] = ts
    T = np.empty(n)
    H = np.empty(n)
    R = np.empty(n)
    Tp = np.empty(n)
    mode = np.empty(n, dtype=np.int8)
    t = T0
    h = H0
    rr = R0
    tp = T0
    div = 0
    dhw_t = 0.0
    T[0] = t
    H[0] = h
    R[0] = rr
    Tp[0] = tp
    mode[0] = MODE_OFF_STAG
    was_invalid = False
    for k in range(1, n):
        if not valid[k]:
            was_invalid = True
            T[k] = t
            Ts[k] = ts
            H[k] = h
            R[k] = rr
            Tp[k] = tp
            mode[k] = MODE_INVALID
            continue
        if was_invalid:
            was_invalid = False
            t = tinc_meas[k]
            ts = t
            tp = t
            h = t
            rr = t
            for i in range(n_pipe):
                ring[i] = t
        wwb = ww[k] >= 0.5
        if wwb:
            div = 1
            dhw_t += DT
            since_ww = 0.0
        else:
            dhw_t = 0.0
            since_ww += DT
            if div == 1 and bp[k] > 0.0 and since_ww >= DIV_CLEAR_S:
                div = 0
                since_clear = 0.0
            since_clear += DT
        f = p3[k]
        r1 = p1[k]
        r2 = p2[k]
        r = r1 or r2
        amb = troom[k] + d_amb
        ff = 0.0
        if f:
            pp = pos[k]
            if pp < 0.0:
                pp = 0.0
            ff = pp / 100.0
        fr = 0.0
        if r1:
            fr += q1
        if r2:
            fr += q2
        act = ((pmod[k] > 0.0) and (not wwb) and (div == 0)
               and (since_clear >= div_travel))
        if wwb and dhw_t <= dhw_hold:
            m = MODE_HOLD
        elif act:
            m = MODE_ACT
            out = ring[rpos]
            ring[rpos] = tb[k]
            rpos += 1
            if rpos >= n_pipe:
                rpos = 0
            tp = tp + a_p * (out - tp)
            if f and not r:
                eps = eA
            elif f and r:
                eps = eB
            elif r:
                eps = eC
            else:
                eps = eN
            t = t + a_hx * (tret[k] + eps * (tp - tret[k]) - t)
            h = h + a_hc * (tret[k] + k_hc * (t - tret[k]) - h)
        else:
            tp = t
            fl = ff + fr
            g_f = DT * fl / V
            if fl > 0.0:
                tmix = (ff * tret[k] + fr * rr) / fl
            else:
                tmix = t
            t = t + (g_f * (tmix - t) + g_fn * (h - t)) / (1.0 + g_f + g_fn)
            if wwb:
                m = MODE_DHW
            elif fl > 0.0:
                m = MODE_OFF_FLOW
            else:
                m = MODE_OFF_STAG
            kb = k_b_park if div == 1 else k_b
            h = h + a_h * (amb + kb * (tb[k] - amb) - h)
        if m != MODE_ACT and m != MODE_HOLD:
            for i in range(n_pipe):
                ring[i] = ring[i] + a_pipe * (t - ring[i])
        if m != MODE_HOLD:
            if r and m == MODE_ACT:
                rr = rr + a_rc * (t - rr)
            else:
                rr = rr + a_rd * (amb - rr)
        ts = ts + a_s * (t - ts)
        if k == anchor:
            t = tinc_meas[k]
            ts = t
            if m != MODE_ACT:
                tp = t
        T[k] = t
        Ts[k] = ts
        H[k] = h
        R[k] = rr
        Tp[k] = tp
        mode[k] = m
    return Ts, H, R, Tp, mode
