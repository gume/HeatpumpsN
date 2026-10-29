// C shim around the firmware MPC core (firmware/src/mpc_core.cpp) for the
// Python closed-loop bench. Built by tools/fw_mpc.py with the host g++.
#include "hpn/mpc_core.h"

using namespace hpn::mpc;

extern "C" {

void* mpc_new() { return new Controller(); }
void mpc_free(void* c) { delete static_cast<Controller*>(c); }

void mpc_set_default(void* c, int scenario) {
  static_cast<Controller*>(c)->set_params(default_params(static_cast<uint8_t>(scenario)));
}

// Overrides a default set: law arrays (kKnots each) may be null.
int mpc_set_params(void* c, int scenario, const float* knots, const float* xk, const float* scalars) {
  ParamSet p = default_params(static_cast<uint8_t>(scenario));
  if (knots) for (int i = 0; i < kKnots; ++i) p.knots_pct[i] = knots[i];
  if (xk) for (int i = 0; i < kKnots; ++i) p.xk[i] = xk[i];
  if (scalars) {
    // delay, tau_fwd, tau_inc, horizon, replan, trigger, lambda, start_cost, margin, bias_alpha, supply
    float* f[] = {&p.delay_s, &p.tau_fwd_s, &p.tau_inc_s, &p.horizon_s, &p.replan_s, &p.trigger_k,
                  &p.lambda_travel, &p.start_cost, &p.margin_k, &p.bias_alpha, &p.supply_margin_k};
    for (int i = 0; i < 11; ++i) *f[i] = scalars[i];
  }
  const char* err = nullptr;
  if (!validate(p, &err)) return -1;
  static_cast<Controller*>(c)->set_params(p);
  return 0;
}

void mpc_request_mode(void* c, int mode, float now) {
  static_cast<Controller*>(c)->request_mode(static_cast<uint8_t>(mode), now);
}
void mpc_on_homed(void* c, float now) { static_cast<Controller*>(c)->on_homed(now); }
void mpc_reset_model(void* c) { static_cast<Controller*>(c)->reset_model(); }

// Returns the command kind; *target receives the target for CMD_TARGET.
int mpc_tick(void* c, float now, float pos, float target, float t_inc, float t_ret, float t_fwd,
             float setpoint, float pmin, float pmax, float travel_s, int overshoot, int scenario,
             float* out_target) {
  Inputs in{};
  in.now_s = now; in.pos = pos; in.target = target;
  in.t_inc = t_inc; in.t_ret = t_ret; in.t_fwd = t_fwd;
  in.inc_valid = in.ret_valid = in.fwd_valid = true;
  in.setpoint = setpoint; in.pmin = pmin; in.pmax = pmax; in.travel_s = travel_s;
  in.overshoot = overshoot != 0; in.scenario = static_cast<uint8_t>(scenario);
  const Command cmd = static_cast<Controller*>(c)->tick(in);
  *out_target = cmd.target;
  return cmd.kind;
}

// mode, anomaly, retries, plans, triggered, anomalies, bias, tinc_water, model_fwd
void mpc_status(void* c, float* f, int* i) {
  const Status s = static_cast<Controller*>(c)->status();
  i[0] = s.mode; i[1] = s.anomaly; i[2] = s.retries; i[3] = static_cast<int>(s.plans);
  i[4] = static_cast<int>(s.triggered); i[5] = static_cast<int>(s.anomalies);
  f[0] = s.bias; f[1] = s.tinc_water; f[2] = s.model_fwd; f[3] = s.plan_cost;
}

}
