"""Stateful port of the Step 6 coupled plant (`log_analysis/step6/plant_v1.py`).

`PlantEngine.step()` advances one DT = 10 s tick exactly like one iteration of
`plant_run` in coupled mode (cfg[0] = 1) with actuator mode 0 (the valve
position is an input, e.g. the emulated carrier's physical position) or 1 (the
input is a target and the valve travels at 100 % / ACT_STROKE_S). The legacy
controller replica (mode 2) is not ported: the real ESP32 controller closes the
loop in HIL.

Parameters are read from `log_analysis/plant_parameters.json` in the same order
`step6_plant_validation_v1.load_params` builds them.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

DT = 10.0
MAX_PIPE_N = 64
MAX_MIX_D = 64
MAX_SLAB_D = 128
DIV_CLEAR_S = 30.0
ACT_STROKE_S = 145.0

PARAMS_PATH = Path(__file__).resolve().parent.parent / "log_analysis" / "plant_parameters.json"

HX_NAMES = ("tau_p", "tau_hx", "eps_A", "eps_B", "eps_C", "eps_N",
            "tau_hc", "k_hc", "V", "q1", "q2", "tau_fn", "tau_h", "k_b",
            "k_b_park", "tau_rc", "tau_rd", "dhw_hold_s", "d_amb",
            "L_pipe", "tau_pipe", "div_travel_s", "tau_s")
SLAB_NAMES = ("tau_r", "beta", "tau_in", "tau_out", "tau_off", "kappa", "d_s", "tau_q", "r_q")
ROOM_NAMES = ("tau_bs", "tau_bo", "c0")


@dataclass
class PlantParams:
    hx: list
    knots: list
    xk: list
    mix: list      # [delay cells, tau_fwd_sensor_s, radiator multiplier]
    slab: list     # SLAB_NAMES + [two_node flag]
    room: list
    boiler: dict   # Step 6 boiler emulator statistics
    sha256: str

    @classmethod
    def load(cls, path: Path = PARAMS_PATH) -> "PlantParams":
        raw = path.read_bytes()
        steps = json.loads(raw)["steps"]
        p2 = steps["step2_hx"]["parameters"]
        p4 = steps["step4_mixer_dynamics"]["parameters"]
        p5 = steps["step5_floor_slab"]["parameters"]
        p6 = steps["step6_assembly"]["parameters"]
        return cls(
            hx=[float(p2[n]) for n in HX_NAMES],
            knots=[float(v) for v in p4["knots_pos_pct"]],
            xk=[float(v) for v in p4["x_at_knots"]],
            mix=[p4["transport_delay_s"] / DT, float(p4["tau_fwd_sensor_s"]),
                 float(p4["radiator_x_multiplier"])],
            slab=[float(p5[n]) for n in SLAB_NAMES] + [1.0 if p5["two_node"] else 0.0],
            room=[float(p5[n]) for n in ROOM_NAMES],
            boiler=dict(p6["boiler_emulator"]),
            sha256=hashlib.sha256(raw).hexdigest(),
        )


def law_x(knots, xk, p):
    K = len(knots)
    if p <= knots[0]:
        return xk[0]
    if p >= knots[K - 1]:
        return xk[K - 1]
    j = 0
    while knots[j + 1] < p:
        j += 1
    w = (p - knots[j]) / (knots[j + 1] - knots[j])
    return xk[j] * (1 - w) + xk[j + 1] * w


@dataclass
class PlantInputs:
    tb: float          # primary boiler outgoing water [degC]
    bp: float          # primary pump [%]
    pmod: float        # burner modulation [%]
    ww: bool           # DHW active
    p1: bool           # radiator 1 pump
    p2: bool           # radiator 2 pump
    p3: bool           # floor pump
    pos: float         # valve position (act_mode 0) or target (act_mode 1) [%]
    tout: float        # outdoor [degC]


class PlantEngine:
    """One coupled plant instance; `step()` == one `plant_run` iteration."""

    def __init__(self, params: PlantParams, init=(45.0, 30.0, 30.0, 21.0, 0.0),
                 act_mode: int = 0, t_slab: float | None = None):
        self.P = params
        self.act_mode = act_mode
        hx = params.hx
        self.tau_p, self.tau_hx = hx[0], hx[1]
        self.eA, self.eB, self.eC, self.eN = hx[2], hx[3], hx[4], hx[5]
        self.k_hc, self.V, self.q1, self.q2 = hx[7], hx[8], hx[9], hx[10]
        self.k_b, self.k_b_park = hx[13], hx[14]
        self.dhw_hold, self.d_amb = hx[17], hx[18]
        n_pipe = int(round(hx[19] / DT))
        self.n_pipe = min(max(n_pipe, 1), MAX_PIPE_N)
        self.a_pipe = DT / (hx[20] + DT)
        self.div_travel = hx[21]
        self.a_s = DT / (hx[22] + DT)
        self.a_p = DT / (self.tau_p + DT)
        self.a_hx = DT / (self.tau_hx + DT)
        self.a_hc = DT / (hx[6] + DT)
        self.a_h = DT / (hx[12] + DT)
        self.a_rc = DT / (hx[15] + DT)
        self.a_rd = DT / (hx[16] + DT)
        self.g_fn = DT / hx[11]
        self.d_n = int(params.mix[0])
        self.a_f = DT / (params.mix[1] + DT)
        self.radf = params.mix[2]
        s = params.slab
        self.beta, self.tau_in, self.tau_out = s[1], s[2], s[3]
        self.kappa, self.tau_q, self.r_q = s[5], s[7], s[8]
        self.two = s[9] > 0.5
        self.a_r = DT / (s[0] + DT)
        self.a_off = DT / (s[4] + DT)
        self.dn_s = min(int(round(s[6] / DT)), MAX_SLAB_D)
        self.tau_bs, self.tau_bo, self.c0 = params.room
        self.reset(init, t_slab)

    # ------------------------------------------------------------------
    def reset(self, init, t_slab: float | None = None):
        """init = [T_inc, T_fwd, T_ret, T_room, pos] as in plant_run."""
        T0 = float(init[0])
        self.k = 0
        self.ring = [T0] * MAX_PIPE_N
        self.rpos = 0
        self.since_ww = 1e9
        self.since_clear = 1e9
        self.t = self.ts = self.h = self.rr = self.tp = T0
        self.div = 0
        self.dhw_t = 0.0
        self.m = 2
        self.s_f = float(init[1])
        self.mring = [self.s_f] * MAX_MIX_D
        self.mrp = 0
        self.was_on = False
        self.pos = float(init[4])
        self.target = float(init[4])
        self.tr = float(init[2])
        self.tsl = float(init[2]) if t_slab is None else float(t_slab)
        self.tq = self.tsl
        self.sring = [self.tr] * MAX_SLAB_D
        self.srp = 0
        self.tro = float(init[3])
        self.tmix = None

    def set_state(self, *, t_inc=None, t_fwd=None, t_ret=None, t_slab=None,
                  t_room=None, park_diverter=False):
        """Overwrite selected states (presets). Delay lines are refilled."""
        if t_inc is not None:
            self.t = self.ts = self.h = self.rr = self.tp = float(t_inc)
            self.ring = [float(t_inc)] * MAX_PIPE_N
        if t_fwd is not None:
            self.s_f = float(t_fwd)
            self.mring = [self.s_f] * MAX_MIX_D
            self.was_on = False
        if t_ret is not None:
            self.tr = float(t_ret)
            self.sring = [self.tr] * MAX_SLAB_D
        if t_slab is not None:
            self.tsl = self.tq = float(t_slab)
        if t_room is not None:
            self.tro = float(t_room)
        if park_diverter:
            self.div = 1
            self.since_ww = 0.0

    # ------------------------------------------------------------------
    def step(self, u: PlantInputs) -> dict:
        k = self.k
        p1, p2, p3 = bool(u.p1), bool(u.p2), bool(u.p3)
        # ======== valve position ========
        if self.act_mode == 0:
            self.pos = u.pos
        else:
            self.target = u.pos
            step = 100.0 / ACT_STROKE_S * DT
            if self.target > self.pos + step:
                self.pos += step
            elif self.target < self.pos - step:
                self.pos -= step
            else:
                self.pos = self.target
            self.pos = min(max(self.pos, 0.0), 100.0)
        pos = self.pos

        # ======== HX ========
        tret_hx = self.tr
        troom_hx = self.tro
        if k != 0:
            wwb = u.ww >= 0.5
            if wwb:
                self.div = 1
                self.dhw_t += DT
                self.since_ww = 0.0
            else:
                self.dhw_t = 0.0
                self.since_ww += DT
                if self.div == 1 and u.bp > 0.0 and self.since_ww >= DIV_CLEAR_S:
                    self.div = 0
                    self.since_clear = 0.0
                self.since_clear += DT
            f = p3
            r = p1 or p2
            amb = troom_hx + self.d_amb
            ff = 0.0
            if f:
                ff = max(pos, 0.0) / 100.0
            fr = 0.0
            if p1:
                fr += self.q1
            if p2:
                fr += self.q2
            act = (u.pmod > 0.0) and (not wwb) and (self.div == 0) and (self.since_clear >= self.div_travel)
            if wwb and self.dhw_t <= self.dhw_hold:
                m = 4
            elif act:
                m = 0
                o_ = self.ring[self.rpos]
                self.ring[self.rpos] = u.tb
                self.rpos += 1
                if self.rpos >= self.n_pipe:
                    self.rpos = 0
                self.tp = self.tp + self.a_p * (o_ - self.tp)
                if f and not r:
                    eps = self.eA
                elif f and r:
                    eps = self.eB
                elif r:
                    eps = self.eC
                else:
                    eps = self.eN
                self.t = self.t + self.a_hx * (tret_hx + eps * (self.tp - tret_hx) - self.t)
                self.h = self.h + self.a_hc * (tret_hx + self.k_hc * (self.t - tret_hx) - self.h)
            else:
                self.tp = self.t
                fl = ff + fr
                g_f = DT * fl / self.V
                if fl > 0.0:
                    tmx = (ff * tret_hx + fr * self.rr) / fl
                else:
                    tmx = self.t
                self.t = self.t + (g_f * (tmx - self.t) + self.g_fn * (self.h - self.t)) / (1.0 + g_f + self.g_fn)
                if wwb:
                    m = 3
                elif fl > 0.0:
                    m = 1
                else:
                    m = 2
                kb = self.k_b_park if self.div == 1 else self.k_b
                self.h = self.h + self.a_h * (amb + kb * (u.tb - amb) - self.h)
            if m != 0 and m != 4:
                for i in range(self.n_pipe):
                    self.ring[i] = self.ring[i] + self.a_pipe * (self.t - self.ring[i])
            if m != 4:
                if r and m == 0:
                    self.rr = self.rr + self.a_rc * (self.t - self.rr)
                else:
                    self.rr = self.rr + self.a_rd * (amb - self.rr)
            self.ts = self.ts + self.a_s * (self.t - self.ts)
            self.m = m

        # ======== mixer ========
        tinc_w = self.t
        tret_m = self.tr
        if not p3:
            self.s_f = self.s_f + self.a_off * (self.tsl + self.kappa * (self.tro - self.tsl) - self.s_f)
            self.was_on = False
            self.tmix = None
        else:
            if not self.was_on:
                for i in range(MAX_MIX_D):
                    self.mring[i] = self.s_f
                self.was_on = True
            x = law_x(self.P.knots, self.P.xk, pos)
            if p1 or p2:
                x = min(x * self.radf, 1.0)
            tmix = tret_m + x * (tinc_w - tret_m)
            self.tmix = tmix
            if self.d_n > 0:
                tw = self.mring[self.mrp]
                self.mring[self.mrp] = tmix
                self.mrp += 1
                if self.mrp >= self.d_n:
                    self.mrp = 0
            else:
                tw = tmix
            self.s_f = self.s_f + self.a_f * (tw - self.s_f)

        # ======== slab ========
        tf_in = self.s_f
        tamb = self.tro
        if p3:
            if self.dn_s > 0:
                tfd = self.sring[self.srp]
                self.sring[self.srp] = tf_in
                self.srp += 1
                if self.srp >= self.dn_s:
                    self.srp = 0
            else:
                tfd = tf_in
            target_r = self.tsl + self.beta * (tfd - self.tsl)
            qin = (tfd - self.tr) / self.tau_in
            self.tr = self.tr + self.a_r * (target_r - self.tr)
            self.tsl = self.tsl + DT * (qin - (self.tsl - tamb) / self.tau_out)
        else:
            for i in range(self.dn_s):
                self.sring[i] = self.tr
            self.tr = self.tr + self.a_off * (self.tsl + self.kappa * (tamb - self.tsl) - self.tr)
            self.tsl = self.tsl + DT * (-(self.tsl - tamb) / self.tau_out)
        if self.two:
            dq = DT * (self.tq - self.tsl) / self.tau_q
            self.tsl = self.tsl + dq
            self.tq = self.tq - dq * self.r_q
        # ======== room ========
        self.tro = self.tro + DT * ((self.tsl - self.tro) / self.tau_bs
                                    - (self.tro - u.tout) / self.tau_bo + self.c0)
        self.k += 1
        return self.outputs()

    def outputs(self) -> dict:
        return dict(t_inc_sensor=self.ts, t_inc_water=self.t, t_fwd_sensor=self.s_f,
                    t_fwd_mix=self.tmix, t_ret=self.tr, t_slab=self.tsl, t_room=self.tro,
                    pos=self.pos, hx_mode=self.m, diverter=self.div)
