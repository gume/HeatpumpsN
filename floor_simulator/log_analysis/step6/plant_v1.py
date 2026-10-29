"""Step 6 v1 - integrated HIL plant model (Python/numba reference).

One deterministic loop at DT = 10 s that chains the identified sub-models:

  Step 2  HX / T_incoming        hx_model_v71.simulate        (23 parameters)
  Step 3+4 mixer                 step4_mixer_dynamics_v2       (law, radiator
                                 multiplier, delay, sensor lag; actuator
                                 100 % / 145 s)
  Step 5  floor loop, slab, room step5_floor_slab_v1

Modes (cfg):
  cfg[0] coupled  0 = component mode: every sub-model receives the measured
                      inputs it was identified with (consistency check);
                  1 = coupled HIL mode: T_return, T_room, T_incoming water and
                      T_forward are passed between the sub-models.
  cfg[1] actuator 0 = `pos` is the valve position (replay);
                  1 = `pos` is the commanded target, the valve travels at
                      100 % / ACT_STROKE_S;
                  2 = the legacy-controller replica computes the target.

Plant-only assumptions (coupled mode, not identified in Steps 2-5):
  * with the floor pump off, the forward sensor relaxes like the return
    manifold (Step 5 tau_off / kappa) because both sit in stagnant pipes;
  * at a floor-pump start the forward delay line is filled with the current
    forward-sensor value (stagnant water) - no re-anchoring to measurements;
  * sub-models exchange values with a one-step (10 s) lag where they feed
    each other (T_return and T_room into the HX, T_return into the mixer).

The C++ header `plant_v1.hpp` mirrors `plant_run` in coupled mode with
actuator modes 0 and 1.
"""
from __future__ import annotations

import numpy as np
from numba import njit

DT = 10.0
MAX_PIPE_N = 64
MAX_MIX_D = 64
MAX_SLAB_D = 128
DIV_CLEAR_S = 30.0
ACT_STROKE_S = 145.0

# legacy controller (OLD-CODE/heatpumps.yaml)
LEG_SP = 35.0
LEG_KP = 0.005
LEG_KD = 0.3
LEG_DB_HI = 0.3      # threshold_high: pv above target within 0.3 K -> no output
LEG_DB_LO = 0.5      # threshold_low: pv below target within 0.5 K -> no output
LEG_PID_PERIOD = 20.0


@njit(cache=True)
def law_x(knots, xk, p):
    K = knots.shape[0]
    if p <= knots[0]:
        return xk[0]
    if p >= knots[K - 1]:
        return xk[K - 1]
    j = 0
    while knots[j + 1] < p:
        j += 1
    w = (p - knots[j]) / (knots[j + 1] - knots[j])
    return xk[j] * (1 - w) + xk[j + 1] * w


@njit(cache=True)
def plant_run(hx, knots, xk, mix, slab, room, cfg,
              tb, bp, pmod, ww, p1, p2, p3, pos_in, tout,
              m_tinc, m_tinc_w, m_tret, m_tfwd, m_troom, m_valid, m_anchor_ok,
              hx_anchor, init):
    """Run the plant.  Returns a (n, 8) array with columns
    0 T_incoming sensor, 1 T_incoming water, 2 T_forward sensor, 3 T_return,
    4 slab, 5 room, 6 valve position [%], 7 HX mode.

    init = [T_inc, T_fwd, T_ret, T_room, pos] at k = 0.
    """
    coupled = cfg[0] == 1
    act_mode = cfg[1]
    n = tb.shape[0]
    out = np.empty((n, 8))
    # ---------------- HX (Step 2) ----------------
    tau_p = hx[0]; tau_hx = hx[1]
    eA = hx[2]; eB = hx[3]; eC = hx[4]; eN = hx[5]
    tau_hc = hx[6]; k_hc = hx[7]; V = hx[8]; q1 = hx[9]; q2 = hx[10]
    tau_fn = hx[11]; tau_h = hx[12]; k_b = hx[13]; k_b_park = hx[14]
    tau_rc = hx[15]; tau_rd = hx[16]; dhw_hold = hx[17]; d_amb = hx[18]
    n_pipe = int(round(hx[19] / DT))
    if n_pipe < 1:
        n_pipe = 1
    if n_pipe > MAX_PIPE_N:
        n_pipe = MAX_PIPE_N
    a_pipe = DT / (hx[20] + DT)
    div_travel = hx[21]
    a_s = DT / (hx[22] + DT)
    a_p = DT / (tau_p + DT)
    a_hx = DT / (tau_hx + DT)
    a_hc = DT / (tau_hc + DT)
    a_h = DT / (tau_h + DT)
    a_rc = DT / (tau_rc + DT)
    a_rd = DT / (tau_rd + DT)
    g_fn = DT / tau_fn
    T0 = init[0]
    ring = np.empty(MAX_PIPE_N)
    for i in range(n_pipe):
        ring[i] = T0
    rpos = 0
    since_ww = 1e9
    since_clear = 1e9
    t = T0
    ts = T0
    h = T0
    rr = T0
    tp = T0
    div = 0
    dhw_t = 0.0
    was_invalid = False
    m = 2
    # ---------------- mixer (Steps 3-4) ----------------
    d_n = int(mix[0])
    a_f = DT / (mix[1] + DT)
    radf = mix[2]
    mring = np.empty(MAX_MIX_D)
    s_f = init[1]
    for i in range(MAX_MIX_D):
        mring[i] = s_f
    mrp = 0
    was_on = False
    pos = init[4]
    # ---------------- slab / room (Step 5) ----------------
    tau_r = slab[0]; beta = slab[1]; tau_in = slab[2]; tau_out = slab[3]
    tau_off = slab[4]; kappa = slab[5]; d_s = slab[6]; tau_q = slab[7]; r_q = slab[8]
    two = slab[9] > 0.5
    a_r = DT / (tau_r + DT)
    a_off = DT / (tau_off + DT)
    dn_s = int(round(d_s / DT))
    if dn_s > MAX_SLAB_D:
        dn_s = MAX_SLAB_D
    sring = np.empty(MAX_SLAB_D)
    tr = init[2]
    tsl = init[2]
    tq = init[2]
    for i in range(MAX_SLAB_D):
        sring[i] = tr
    srp = 0
    tau_bs = room[0]; tau_bo = room[1]; c0 = room[2]
    tro = init[3]
    # ---------------- legacy controller replica ----------------
    c_min = 0.40
    c_goal = init[4] / 100.0
    c_unsafe = False
    c_res = 0.0
    c_eprev = 0.0
    c_has_prev = False
    c_timer = 0.0
    c_was_on = False
    target = init[4]

    for k in range(n):
        # ======== valve position ========
        if act_mode == 0:
            pos = pos_in[k]
        else:
            if act_mode == 1:
                target = pos_in[k]
            else:
                # legacy replica (floor circuit = pump 3)
                fwd = s_f if coupled else m_tfwd[k]
                inc = ts if coupled else m_tinc[k]
                if p3[k] and not c_was_on:
                    c_has_prev = False
                if (not p3[k]) and c_was_on:
                    c_min = 0.0
                    c_goal = 0.0
                    c_unsafe = False
                c_was_on = p3[k]
                if p3[k]:
                    if inc > 60.0:
                        cmax = 0.70
                    elif inc > 50.0:
                        cmax = 0.75
                    elif inc > 40.0:
                        cmax = 0.80
                    else:
                        cmax = 0.90
                    if fwd > LEG_SP:
                        cmax = 0.60
                    cur = pos / 100.0
                    req = -1.0
                    if cur < c_min - 0.02:
                        req = c_min
                    elif cur > cmax + 0.02:
                        req = cmax
                    if fwd > LEG_SP + 3.0:
                        c_unsafe = True
                        req = c_min
                    if c_unsafe and fwd < LEG_SP - 1.0:
                        c_unsafe = False
                    c_timer += DT
                    if c_timer >= LEG_PID_PERIOD:
                        c_timer = 0.0
                        e = LEG_SP - fwd
                        de = (e - c_eprev) / LEG_PID_PERIOD if c_has_prev else 0.0
                        c_eprev = e
                        c_has_prev = True
                        if -LEG_DB_HI <= e <= LEG_DB_LO:
                            o = 0.0
                        else:
                            o = LEG_KP * e + LEG_KD * de
                        if o > 1.0:
                            o = 1.0
                        if o < -1.0:
                            o = -1.0
                        if o > 0.0:
                            st = o
                            if st < 0.02:
                                c_res += st
                                st = c_res
                            if cur < 0.8 and st > 0.02:
                                c_res = 0.0
                                req = cur + st
                        elif o < 0.0:
                            st = -o
                            if st < 0.02:
                                c_res -= st
                                st = -c_res
                            if cur > 0.2 and st > 0.02:
                                c_res = 0.0
                                req = cur - st
                    if req >= 0.0:
                        v = req
                        if c_unsafe:
                            v = c_min
                        elif v > cmax:
                            v = cmax
                        elif v < c_min:
                            v = c_min
                        if not (v > cur - 0.015 and v < cur + 0.015) and v != c_goal:
                            c_goal = v
                target = c_goal * 100.0
            step = 100.0 / ACT_STROKE_S * DT
            if target > pos + step:
                pos += step
            elif target < pos - step:
                pos -= step
            else:
                pos = target
            if pos < 0.0:
                pos = 0.0
            if pos > 100.0:
                pos = 100.0

        # ======== HX ========
        tret_hx = tr if coupled else m_tret[k]
        troom_hx = tro if coupled else m_troom[k]
        valid = True if coupled else m_valid[k]
        if k == 0:
            pass
        elif not valid:
            was_invalid = True
            m = 5
        else:
            if was_invalid:
                was_invalid = False
                t = m_tinc[k]
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
            amb = troom_hx + d_amb
            ff = 0.0
            if f:
                pp = pos
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
                m = 4
            elif act:
                m = 0
                o_ = ring[rpos]
                ring[rpos] = tb[k]
                rpos += 1
                if rpos >= n_pipe:
                    rpos = 0
                tp = tp + a_p * (o_ - tp)
                if f and not r:
                    eps = eA
                elif f and r:
                    eps = eB
                elif r:
                    eps = eC
                else:
                    eps = eN
                t = t + a_hx * (tret_hx + eps * (tp - tret_hx) - t)
                h = h + a_hc * (tret_hx + k_hc * (t - tret_hx) - h)
            else:
                tp = t
                fl = ff + fr
                g_f = DT * fl / V
                if fl > 0.0:
                    tmx = (ff * tret_hx + fr * rr) / fl
                else:
                    tmx = t
                t = t + (g_f * (tmx - t) + g_fn * (h - t)) / (1.0 + g_f + g_fn)
                if wwb:
                    m = 3
                elif fl > 0.0:
                    m = 1
                else:
                    m = 2
                kb = k_b_park if div == 1 else k_b
                h = h + a_h * (amb + kb * (tb[k] - amb) - h)
            if m != 0 and m != 4:
                for i in range(n_pipe):
                    ring[i] = ring[i] + a_pipe * (t - ring[i])
            if m != 4:
                if r and m == 0:
                    rr = rr + a_rc * (t - rr)
                else:
                    rr = rr + a_rd * (amb - rr)
            ts = ts + a_s * (t - ts)
            if k == hx_anchor:
                t = m_tinc[k]
                ts = t
                if m != 0:
                    tp = t

        # ======== mixer ========
        if coupled:
            tinc_w = t
            tret_m = tr
            anchor_ok = True
        else:
            tinc_w = m_tinc_w[k]
            tret_m = m_tret[k]
            anchor_ok = m_anchor_ok[k]
        if not coupled and (not p3[k] or not anchor_ok):
            s_f = m_tfwd[k]
            for i in range(MAX_MIX_D):
                mring[i] = s_f
            was_on = False
        elif coupled and not p3[k]:
            s_f = s_f + a_off * (tsl + kappa * (tro - tsl) - s_f)
            was_on = False
        else:
            if not was_on:
                if not coupled:
                    s_f = m_tfwd[k]
                for i in range(MAX_MIX_D):
                    mring[i] = s_f
                was_on = True
            x = law_x(knots, xk, pos)
            if p1[k] or p2[k]:
                x = x * radf
                if x > 1.0:
                    x = 1.0
            tmix = tret_m + x * (tinc_w - tret_m)
            if d_n > 0:
                tw = mring[mrp]
                mring[mrp] = tmix
                mrp += 1
                if mrp >= d_n:
                    mrp = 0
            else:
                tw = tmix
            s_f = s_f + a_f * (tw - s_f)

        # ======== slab ========
        tf_in = s_f if coupled else m_tfwd[k]
        tamb = tro if coupled else m_troom[k]
        if p3[k]:
            if dn_s > 0:
                tfd = sring[srp]
                sring[srp] = tf_in
                srp += 1
                if srp >= dn_s:
                    srp = 0
            else:
                tfd = tf_in
            target_r = tsl + beta * (tfd - tsl)
            qin = (tfd - tr) / tau_in
            tr = tr + a_r * (target_r - tr)
            tsl = tsl + DT * (qin - (tsl - tamb) / tau_out)
        else:
            for i in range(dn_s):
                sring[i] = tr
            tr = tr + a_off * (tsl + kappa * (tamb - tsl) - tr)
            tsl = tsl + DT * (-(tsl - tamb) / tau_out)
        if two:
            dq = DT * (tq - tsl) / tau_q
            tsl = tsl + dq
            tq = tq - dq * r_q
        # ======== room ========
        tro = tro + DT * ((tsl - tro) / tau_bs - (tro - tout[k]) / tau_bo + c0)

        out[k, 0] = ts
        out[k, 1] = t
        out[k, 2] = s_f
        out[k, 3] = tr
        out[k, 4] = tsl
        out[k, 5] = tro
        out[k, 6] = pos
        out[k, 7] = m
    return out
