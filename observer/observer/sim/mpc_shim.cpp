// C shim around the firmware MPC core (firmware/src/mpc_core.cpp) for the
// observer's simulations. Built while the image is built (Dockerfile) or on
// first use (observer/sim/fwcore.py); derived from floor_simulator/tools/mpc_host_shim.cpp.
#include <string.h>

#include "hpn/mpc_core.h"

using namespace hpn::mpc;

namespace {
// delay, tau_fwd, tau_inc, horizon, replan, trigger, lambda, start_cost, margin, bias_alpha,
// supply, boost_s, boost_scale, boost_error_k
constexpr int kScalars = 14;
float* scalar_ptrs(ParamSet& p, int i) {
  float* f[] = {&p.delay_s, &p.tau_fwd_s, &p.tau_inc_s, &p.horizon_s, &p.replan_s, &p.trigger_k,
                &p.lambda_travel, &p.start_cost, &p.margin_k, &p.bias_alpha, &p.supply_margin_k,
                &p.boost_s, &p.boost_scale, &p.boost_error_k};
  return f[i];
}
}  // namespace

extern "C" {

int mpc_knots() { return kKnots; }
int mpc_scalars() { return kScalars; }

void* mpc_new() { return new Controller(); }
void mpc_free(void* c) { delete static_cast<Controller*>(c); }

// The firmware's default set of a scenario.
void mpc_default(int scenario, float* knots, float* xk, float* scalars) {
  ParamSet p = default_params(static_cast<uint8_t>(scenario));
  for (int i = 0; i < kKnots; ++i) { knots[i] = p.knots_pct[i]; xk[i] = p.xk[i]; }
  for (int i = 0; i < kScalars; ++i) scalars[i] = *scalar_ptrs(p, i);
}

// A complete set; 0 when accepted, -1 with the firmware's validate() message in err.
int mpc_set(void* c, int scenario, const float* knots, const float* xk, const float* scalars,
            char* err, int err_len) {
  ParamSet p = default_params(static_cast<uint8_t>(scenario));
  for (int i = 0; i < kKnots; ++i) { p.knots_pct[i] = knots[i]; p.xk[i] = xk[i]; }
  for (int i = 0; i < kScalars; ++i) *scalar_ptrs(p, i) = scalars[i];
  const char* msg = nullptr;
  if (!validate(p, &msg)) {
    if (err && err_len > 0) { strncpy(err, msg ? msg : "invalid", err_len - 1); err[err_len - 1] = 0; }
    return -1;
  }
  if (c) static_cast<Controller*>(c)->set_params(p);
  return 0;
}

void mpc_request_mode(void* c, int mode, double now) {
  static_cast<Controller*>(c)->request_mode(static_cast<uint8_t>(mode), now);
}
void mpc_on_homed(void* c, double now) { static_cast<Controller*>(c)->on_homed(now); }
void mpc_reset_model(void* c) { static_cast<Controller*>(c)->reset_model(); }

// Returns the command kind; *out_target receives the target for CMD_TARGET.
int mpc_tick(void* c, double now, float pos, float target, float t_inc, float t_ret, float t_fwd,
             int inc_valid, int ret_valid, int fwd_valid, float setpoint, float pmin, float pmax,
             float travel_s, float overshoot_delta, int scenario, float* out_target) {
  Inputs in{};
  in.now_s = now; in.pos = pos; in.target = target;
  in.t_inc = t_inc; in.t_ret = t_ret; in.t_fwd = t_fwd;
  in.inc_valid = inc_valid; in.ret_valid = ret_valid; in.fwd_valid = fwd_valid;
  in.setpoint = setpoint; in.pmin = pmin; in.pmax = pmax; in.travel_s = travel_s;
  in.overshoot_delta = overshoot_delta; in.scenario = static_cast<uint8_t>(scenario);
  const Command cmd = static_cast<Controller*>(c)->tick(in);
  *out_target = cmd.target;
  return cmd.kind;
}

// i: mode, anomaly, retries, plans, triggered, anomalies, boost
// f: bias, tinc_water, model_fwd, plan_cost, stable_s
void mpc_status(void* c, float* f, int* i) {
  const Status s = static_cast<Controller*>(c)->status();
  i[0] = s.mode; i[1] = s.anomaly; i[2] = s.retries; i[3] = static_cast<int>(s.plans);
  i[4] = static_cast<int>(s.triggered); i[5] = static_cast<int>(s.anomalies); i[6] = s.boost;
  f[0] = s.bias; f[1] = s.tinc_water; f[2] = s.model_fwd; f[3] = s.plan_cost; f[4] = s.stable_s;
}

}
