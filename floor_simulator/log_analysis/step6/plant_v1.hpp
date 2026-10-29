// plant_v1.hpp - floor-heating HIL plant model, Step 6 v1.
// C++ port of simulation/step6/plant_v1.py::plant_run, coupled mode
// (cfg[0] = 1) with actuator modes 0 (position replay) and 1 (target with
// 100 %/145 s travel).  Parameters come from simulation/plant_parameters.json.
// Header-only, no allocation after construction, deterministic; intended
// to be compiled into the ESP32 emulation backend or a host test bench.
#pragma once
#include <cmath>
#include <array>

namespace hpn_plant {

constexpr double DT = 10.0;
constexpr int MAX_PIPE_N = 64;
constexpr int MAX_MIX_D = 64;
constexpr int MAX_SLAB_D = 128;
constexpr double DIV_CLEAR_S = 30.0;
constexpr double ACT_STROKE_S = 145.0;
constexpr int MAX_KNOTS = 16;

struct Params {
  double hx[23];                 // Step 2 v7.1, order of hx_model_v71.PARAM_NAMES
  int n_knots;
  double knots[MAX_KNOTS];       // Step 4 law knots [%]
  double xk[MAX_KNOTS];          // Step 4 hot fraction at the knots
  double mix[3];                 // delay samples, sensor tau [s], radiator multiplier
  double slab[10];               // tau_r, beta, tau_in, tau_out, tau_off, kappa, d_s, tau_q, r_q, two_node
  double room[3];                // tau_bs, tau_bo, c0
};

struct Inputs {                  // one 10 s sample
  double tb, bp, pmod, ww;       // boiler: flow temp, burner %, primary pump %, DHW flag
  bool p1, p2, p3;               // pump relays: radiator 1, radiator 2, floor
  double pos;                    // valve position [%] (mode 0) or target [%] (mode 1)
  double tout;                   // outdoor temperature
};

struct Outputs {
  double tinc_sensor, tinc_water, tfwd_sensor, tret, slab, room, pos;
  int hx_mode;
};

class Plant {
 public:
  Plant(const Params& p, int actuator_mode, const double init[5]) : P(p), act_mode(actuator_mode) {
    const double* hx = P.hx;
    n_pipe = (int)std::lround(hx[19] / DT);
    if (n_pipe < 1) n_pipe = 1;
    if (n_pipe > MAX_PIPE_N) n_pipe = MAX_PIPE_N;
    a_pipe = DT / (hx[20] + DT);
    a_s = DT / (hx[22] + DT);
    a_p = DT / (hx[0] + DT);
    a_hx = DT / (hx[1] + DT);
    a_hc = DT / (hx[6] + DT);
    a_h = DT / (hx[12] + DT);
    a_rc = DT / (hx[15] + DT);
    a_rd = DT / (hx[16] + DT);
    g_fn = DT / hx[11];
    t = ts = h = rr = tp = init[0];
    for (int i = 0; i < n_pipe; ++i) ring[i] = init[0];
    d_n = (int)P.mix[0];
    a_f = DT / (P.mix[1] + DT);
    s_f = init[1];
    for (int i = 0; i < MAX_MIX_D; ++i) mring[i] = s_f;
    pos = init[4];
    target = init[4];
    a_r = DT / (P.slab[0] + DT);
    a_off = DT / (P.slab[4] + DT);
    dn_s = (int)std::lround(P.slab[6] / DT);
    if (dn_s > MAX_SLAB_D) dn_s = MAX_SLAB_D;
    tr = tsl = tq = init[2];
    for (int i = 0; i < MAX_SLAB_D; ++i) sring[i] = tr;
    tro = init[3];
  }

  Outputs step(const Inputs& in) {
    // valve
    if (act_mode == 0) {
      pos = in.pos;
    } else {
      target = in.pos;
      const double st = 100.0 / ACT_STROKE_S * DT;
      if (target > pos + st) pos += st;
      else if (target < pos - st) pos -= st;
      else pos = target;
      if (pos < 0.0) pos = 0.0;
      if (pos > 100.0) pos = 100.0;
    }
    // HX (Step 2)
    const double* hx = P.hx;
    const double tret_hx = tr, troom_hx = tro;
    if (k > 0) {
      const bool wwb = in.ww >= 0.5;
      if (wwb) { div = 1; dhw_t += DT; since_ww = 0.0; }
      else {
        dhw_t = 0.0;
        since_ww += DT;
        if (div == 1 && in.bp > 0.0 && since_ww >= DIV_CLEAR_S) { div = 0; since_clear = 0.0; }
        since_clear += DT;
      }
      const bool f = in.p3, r1 = in.p1, r2 = in.p2, r = r1 || r2;
      const double amb = troom_hx + hx[18];
      double ff = 0.0;
      if (f) { double pp = pos; if (pp < 0.0) pp = 0.0; ff = pp / 100.0; }
      double fr = 0.0;
      if (r1) fr += hx[9];
      if (r2) fr += hx[10];
      const bool act = (in.pmod > 0.0) && !wwb && (div == 0) && (since_clear >= hx[21]);
      if (wwb && dhw_t <= hx[17]) {
        m = 4;
      } else if (act) {
        m = 0;
        const double o = ring[rpos];
        ring[rpos] = in.tb;
        if (++rpos >= n_pipe) rpos = 0;
        tp = tp + a_p * (o - tp);
        double eps;
        if (f && !r) eps = hx[2];
        else if (f && r) eps = hx[3];
        else if (r) eps = hx[4];
        else eps = hx[5];
        t = t + a_hx * (tret_hx + eps * (tp - tret_hx) - t);
        h = h + a_hc * (tret_hx + hx[7] * (t - tret_hx) - h);
      } else {
        tp = t;
        const double fl = ff + fr;
        const double g_f = DT * fl / hx[8];
        const double tmx = fl > 0.0 ? (ff * tret_hx + fr * rr) / fl : t;
        t = t + (g_f * (tmx - t) + g_fn * (h - t)) / (1.0 + g_f + g_fn);
        if (wwb) m = 3; else if (fl > 0.0) m = 1; else m = 2;
        const double kb = div == 1 ? hx[14] : hx[13];
        h = h + a_h * (amb + kb * (in.tb - amb) - h);
      }
      if (m != 0 && m != 4)
        for (int i = 0; i < n_pipe; ++i) ring[i] = ring[i] + a_pipe * (t - ring[i]);
      if (m != 4) {
        if (r && m == 0) rr = rr + a_rc * (t - rr);
        else rr = rr + a_rd * (amb - rr);
      }
      ts = ts + a_s * (t - ts);
    }
    // mixer (Steps 3-4)
    const double tinc_w = t, tret_m = tr;
    if (!in.p3) {
      s_f = s_f + a_off * (tsl + P.slab[5] * (tro - tsl) - s_f);
      was_on = false;
    } else {
      if (!was_on) {
        for (int i = 0; i < MAX_MIX_D; ++i) mring[i] = s_f;
        was_on = true;
      }
      double x = law_x(pos);
      if (in.p1 || in.p2) { x = x * P.mix[2]; if (x > 1.0) x = 1.0; }
      const double tmix = tret_m + x * (tinc_w - tret_m);
      double tw;
      if (d_n > 0) { tw = mring[mrp]; mring[mrp] = tmix; if (++mrp >= d_n) mrp = 0; }
      else tw = tmix;
      s_f = s_f + a_f * (tw - s_f);
    }
    // slab (Step 5)
    const double tf_in = s_f, tamb = tro;
    const double* sl = P.slab;
    if (in.p3) {
      double tfd;
      if (dn_s > 0) { tfd = sring[srp]; sring[srp] = tf_in; if (++srp >= dn_s) srp = 0; }
      else tfd = tf_in;
      const double target_r = tsl + sl[1] * (tfd - tsl);
      const double qin = (tfd - tr) / sl[2];
      tr = tr + a_r * (target_r - tr);
      tsl = tsl + DT * (qin - (tsl - tamb) / sl[3]);
    } else {
      for (int i = 0; i < dn_s; ++i) sring[i] = tr;
      tr = tr + a_off * (tsl + sl[5] * (tamb - tsl) - tr);
      tsl = tsl + DT * (-(tsl - tamb) / sl[3]);
    }
    if (sl[9] > 0.5) {
      const double dq = DT * (tq - tsl) / sl[7];
      tsl = tsl + dq;
      tq = tq - dq * sl[8];
    }
    // room
    tro = tro + DT * ((tsl - tro) / P.room[0] - (tro - in.tout) / P.room[1] + P.room[2]);
    ++k;
    return Outputs{ts, t, s_f, tr, tsl, tro, pos, m};
  }

 private:
  double law_x(double p) const {
    const int K = P.n_knots;
    if (p <= P.knots[0]) return P.xk[0];
    if (p >= P.knots[K - 1]) return P.xk[K - 1];
    int j = 0;
    while (P.knots[j + 1] < p) ++j;
    const double w = (p - P.knots[j]) / (P.knots[j + 1] - P.knots[j]);
    return P.xk[j] * (1 - w) + P.xk[j + 1] * w;
  }
  Params P;
  int act_mode;
  long k = 0;
  // HX
  int n_pipe, rpos = 0, div = 0, m = 2;
  double a_pipe, a_s, a_p, a_hx, a_hc, a_h, a_rc, a_rd, g_fn;
  double t, ts, h, rr, tp, since_ww = 1e9, since_clear = 1e9, dhw_t = 0.0;
  std::array<double, MAX_PIPE_N> ring{};
  // mixer
  int d_n, mrp = 0;
  double a_f, s_f, pos, target;
  bool was_on = false;
  std::array<double, MAX_MIX_D> mring{};
  // slab / room
  int dn_s, srp = 0;
  double a_r, a_off, tr, tsl, tq, tro;
  std::array<double, MAX_SLAB_D> sring{};
};

}  // namespace hpn_plant
