#include "hpn/mpc_core.h"

#include <math.h>
#include <string.h>

namespace hpn {
namespace mpc {
namespace {

constexpr float kSteps[kStepCount] = {-0.10f, -0.05f, -0.02f, 0.0f, 0.02f, 0.05f, 0.10f};
constexpr int kMoveAt[kMoves] = {0, 6, 18};   // model steps: t0, +60 s, +180 s
constexpr int kSettleSteps = 6;               // end-of-window weight (settled by then)
constexpr float kSettleWeight = 5.0f;
constexpr float kLimitPenalty = 1000.0f;
constexpr int kRefireStep = 6;                // "boiler refires in 60 s" scenario
constexpr float kHotDecayKPerS = 5.0f / 1800.0f;
// The bias only learns in quasi-steady supply: during a burner transient the
// model error is a timing error, not an offset, and learning it would shift
// every prediction the wrong way once the transient is over.
constexpr float kBiasFreezeSlopeKPerS = 0.05f;

// Supervisor limits. Deliberately compiled in: they guard against a bad
// parameter set, so an observer update must not be able to relax them.
constexpr float kBiasLimitK = 5.0f;
constexpr float kBiasPersistS = 120.0f;
constexpr float kMissLimitK = 2.0f;
constexpr float kMissPersistS = 300.0f;
constexpr float kOvershootPersistS = 10.0f;
constexpr float kSevereOverK = 6.0f;          // above setpoint: no persistence wait
constexpr float kResumeGapS = 10.0f;

// Cautious fallback. Model-free and parameter-free on purpose: it uses only
// the measured forward temperature, the setpoint and the human-owned opening
// window. Opens slowly, closes faster, waits out the loop's dead time.
constexpr float kCautiousWaitS = 240.0f;
constexpr float kCautiousOpenStep = 0.03f;
constexpr float kCautiousCloseStep = 0.04f;
constexpr float kCautiousHotStep = 0.08f;
constexpr float kCautiousHotGapS = 60.0f;
constexpr float kCautiousMax = 0.70f;
constexpr float kCautiousOpenBand = 1.5f;
constexpr float kCautiousCloseBand = 1.0f;
constexpr float kCautiousHotK = 2.5f;
constexpr float kCautiousSupplyK = 3.0f;
constexpr float kCautiousStableBand = 1.5f;
constexpr float kCautiousStableS = 1800.0f;
constexpr float kRetryAfterAnomalyS = 3600.0f;
constexpr float kRetryProbationS = 7200.0f;

// Identified mixing law (Step 4 v2) and timing.
constexpr float kDefaultKnots[kKnots] = {0.0f, 25.0f, 50.0f, 52.5f, 55.0f, 57.5f,
                                         60.0f, 65.0f, 70.0f, 80.0f, 90.0f};
constexpr float kDefaultXk[kKnots] = {0.019709f, 0.087974f, 0.195720f, 0.214395f,
                                      0.243511f, 0.275100f, 0.289054f, 0.289054f,
                                      0.289054f, 0.511466f, 1.0f};
constexpr float kRadiatorMultiplier = 0.792789f;

float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

}  // namespace

const char* scenario_name(uint8_t scenario) {
  switch (scenario) {
    case SC_FLOOR: return "floor";
    case SC_RAD1: return "rad1";
    case SC_RAD2: return "rad2";
    case SC_BOTH: return "both";
    default: return "unknown";
  }
}

int scenario_from_name(const char* name) {
  if (name == nullptr) return -1;
  for (uint8_t i = 0; i < SC_COUNT; ++i) {
    if (strcmp(name, scenario_name(i)) == 0) return i;
  }
  return -1;
}

const char* mode_name(uint8_t mode) {
  switch (mode) {
    case MODE_LEGACY: return "legacy";
    case MODE_MPC: return "mpc";
    case MODE_HOMING: return "homing";
    case MODE_CAUTIOUS: return "cautious";
    default: return "unknown";
  }
}

const char* anomaly_name(uint8_t anomaly) {
  switch (anomaly) {
    case AN_NONE: return "none";
    case AN_BIAS: return "model_bias";
    case AN_TRACKING: return "prediction_miss";
    case AN_OVERSHOOT: return "overshoot";
    case AN_NUMERIC: return "numeric";
    case AN_MANUAL: return "manual";
    default: return "unknown";
  }
}

ParamSet default_params(uint8_t scenario) {
  ParamSet p{};
  const float mult = scenario == SC_FLOOR ? 1.0f : kRadiatorMultiplier;
  for (int i = 0; i < kKnots; ++i) {
    p.knots_pct[i] = kDefaultKnots[i];
    p.xk[i] = clampf(kDefaultXk[i] * mult, 0.0f, 1.0f);
  }
  p.delay_s = 40.0f;
  p.tau_fwd_s = 34.64f;
  p.tau_inc_s = 75.27f;
  p.horizon_s = 900.0f;
  p.replan_s = 30.0f;
  p.trigger_k = 0.5f;
  p.lambda_travel = 0.1f;
  p.start_cost = 1.0f;
  p.margin_k = 2.5f;
  p.bias_alpha = 0.1f;
  p.supply_margin_k = 3.0f;
  p.revision = 0;
  return p;
}

bool validate(const ParamSet& p, const char** err) {
  const char* e = nullptr;
  for (int i = 0; i < kKnots && e == nullptr; ++i) {
    if (!isfinite(p.knots_pct[i]) || p.knots_pct[i] < 0.0f || p.knots_pct[i] > 100.0f) e = "knots_pct";
    else if (i > 0 && p.knots_pct[i] <= p.knots_pct[i - 1]) e = "knots_pct (not increasing)";
    else if (!isfinite(p.xk[i]) || p.xk[i] < 0.0f || p.xk[i] > 1.0f) e = "xk";
    else if (i > 0 && p.xk[i] < p.xk[i - 1]) e = "xk (decreasing)";
  }
  if (e == nullptr && p.xk[kKnots - 1] - p.xk[0] < 0.05f) e = "xk (no authority)";
  auto bad = [](float v, float lo, float hi) { return !isfinite(v) || v < lo || v > hi; };
  if (e == nullptr) {
    if (bad(p.delay_s, 0.0f, (kMaxDelayCells - 1) * kModelDt)) e = "delay_s";
    else if (bad(p.tau_fwd_s, 1.0f, 600.0f)) e = "tau_fwd_s";
    else if (bad(p.tau_inc_s, 0.0f, 600.0f)) e = "tau_inc_s";
    else if (bad(p.horizon_s, 300.0f, kMaxHorizon * kModelDt)) e = "horizon_s";
    else if (bad(p.replan_s, 4.0f, 300.0f)) e = "replan_s";
    else if (bad(p.trigger_k, 0.1f, 5.0f)) e = "trigger_k";
    else if (bad(p.lambda_travel, 0.0f, 10.0f)) e = "lambda_travel";
    else if (bad(p.start_cost, 0.0f, 50.0f)) e = "start_cost";
    else if (bad(p.margin_k, 0.5f, 10.0f)) e = "margin_k";
    else if (bad(p.bias_alpha, 0.0f, 1.0f)) e = "bias_alpha";
    else if (bad(p.supply_margin_k, 0.0f, 20.0f)) e = "supply_margin_k";
  }
  if (err != nullptr) *err = e;
  return e == nullptr;
}

Controller::Controller() : p_(default_params(SC_FLOOR)) {
  memset(&st_, 0, sizeof(st_));
  st_.mode = MODE_MPC;
  memset(ring_, 0, sizeof(ring_));
  memset(plan_pred_, 0, sizeof(plan_pred_));
}

void Controller::set_params(const ParamSet& params) {
  const bool timing_changed = params.delay_s != p_.delay_s;
  p_ = params;
  if (timing_changed) {
    init_ = false;   // delay line length changed: re-seed from the next measurement
  }
  replan_ = true;
}

void Controller::reset_model() {
  init_ = false;
  plan_len_ = 0;
  replan_ = true;
  bias_bad_s_ = miss_s_ = overshoot_s_ = 0.0f;
  st_.bias = 0.0f;
}

void Controller::enter(uint8_t mode, double now_s) {
  st_.mode = mode;
  st_.mode_since_s = now_s;
  st_.stable_s = 0.0f;
  bias_bad_s_ = miss_s_ = overshoot_s_ = 0.0f;
  plan_len_ = 0;
  replan_ = true;
  if (mode == MODE_CAUTIOUS) {
    c_target_ = -1.0f;
    c_last_move_s_ = now_s;
    c_last_close_s_ = -1e9;
  }
}

void Controller::request_mode(uint8_t mode, double now_s) {
  switch (mode) {
    case MODE_MPC:
      retry_blocked_ = false;
      st_.anomaly = AN_NONE;
      reset_model();
      enter(MODE_MPC, now_s);
      break;
    case MODE_LEGACY:
      enter(MODE_LEGACY, now_s);
      break;
    case MODE_CAUTIOUS:
    case MODE_HOMING:
      // The fallback is always entered through homing.
      force_anomaly(AN_MANUAL, now_s);
      break;
    default:
      break;
  }
}

void Controller::force_anomaly(uint8_t anomaly, double now_s) {
  st_.anomaly = anomaly;
  st_.anomalies++;
  if (now_s - retry_s_ < kRetryProbationS) {
    retry_blocked_ = true;   // failed again soon after an automatic retry
  }
  last_anomaly_s_ = now_s;
  enter(MODE_HOMING, now_s);
}

void Controller::on_homed(double now_s) {
  if (st_.mode == MODE_HOMING) {
    reset_model();
    enter(MODE_CAUTIOUS, now_s);
  }
}

float Controller::law(float pos) const {
  const float pct = pos * 100.0f;
  if (pct <= p_.knots_pct[0]) return p_.xk[0];
  if (pct >= p_.knots_pct[kKnots - 1]) return p_.xk[kKnots - 1];
  int j = 0;
  while (j < kKnots - 2 && p_.knots_pct[j + 1] < pct) ++j;
  const float w = (pct - p_.knots_pct[j]) / (p_.knots_pct[j + 1] - p_.knots_pct[j]);
  return p_.xk[j] * (1.0f - w) + p_.xk[j + 1] * w;
}

void Controller::model_step(const Inputs& in) {
  if (!init_) {
    ring_n_ = static_cast<int>(lroundf(p_.delay_s / kModelDt));
    if (ring_n_ > kMaxDelayCells) ring_n_ = kMaxDelayCells;
    for (int i = 0; i < kMaxDelayCells; ++i) ring_[i] = in.t_fwd;
    ring_pos_ = 0;
    s_f_ = in.t_fwd;
    prev_inc_ = in.t_inc;
    slope_ = 0.0f;
    t_hot_ = in.t_inc;
    st_.bias = 0.0f;
    init_ = true;
  }
  slope_ += 0.3f * ((in.t_inc - prev_inc_) / kModelDt - slope_);
  prev_inc_ = in.t_inc;
  const float tw = in.t_inc + p_.tau_inc_s * slope_;
  t_hot_ = fmaxf(tw, t_hot_ - kModelDt * kHotDecayKPerS);
  const float tmix = in.t_ret + law(in.pos) * (tw - in.t_ret);
  float delayed = tmix;
  if (ring_n_ > 0) {
    delayed = ring_[ring_pos_];
    ring_[ring_pos_] = tmix;
    ring_pos_ = (ring_pos_ + 1) % ring_n_;
  }
  const float a_f = kModelDt / (p_.tau_fwd_s + kModelDt);
  s_f_ += a_f * (delayed - s_f_);
  if (fabsf(slope_) < kBiasFreezeSlopeKPerS) {
    st_.bias += p_.bias_alpha * ((in.t_fwd - s_f_) - st_.bias);
  }
  st_.tinc_water = tw;
  st_.model_fwd = s_f_;
}

// Simulate one move sequence. `hot_tw` < 0 disables the refire scenario.
// Returns the weighted tracking cost; `over` receives the largest excess over
// the soft limit; `out` (optional) the predicted forward temperatures.
void Controller::predict(int seq, float pos0, float hot_tw, int n_steps, float* track_cost,
                         float* over, float* out, const Inputs& in) const {
  const int s0 = seq / (kStepCount * kStepCount);
  const int s1 = (seq / kStepCount) % kStepCount;
  const int s2 = seq % kStepCount;
  const float steps[kMoves] = {kSteps[s0], kSteps[s1], kSteps[s2]};
  const float vstep = kModelDt / (in.travel_s > 1.0f ? in.travel_s : 120.0f);
  const float limit = in.setpoint + p_.margin_k;
  float ring[kMaxDelayCells];
  for (int i = 0; i < ring_n_; ++i) ring[i] = ring_[(ring_pos_ + i) % ring_n_];  // oldest first
  int rp = 0;
  float s_f = s_f_;
  float pos = pos0;
  float tgt = pos0;
  float cost = 0.0f;
  float worst = 0.0f;
  for (int k = 0; k < n_steps; ++k) {
    for (int j = 0; j < kMoves; ++j) {
      if (k == kMoveAt[j]) tgt = clampf(tgt + steps[j], in.pmin, in.pmax);
    }
    pos += clampf(tgt - pos, -vstep, vstep);
    const float base = tw_override_ >= 0.0f ? tw_override_ : st_.tinc_water;
    const float tw = (hot_tw >= 0.0f && k >= kRefireStep) ? hot_tw : base;
    const float tmix = in.t_ret + law(pos) * (tw - in.t_ret);
    float delayed = tmix;
    if (ring_n_ > 0) {
      delayed = ring[rp];
      ring[rp] = tmix;
      rp = (rp + 1) % ring_n_;
    }
    s_f += (kModelDt / (p_.tau_fwd_s + kModelDt)) * (delayed - s_f);
    const float y = s_f + st_.bias;
    const float e = y - in.setpoint;
    cost += e * e * (k >= n_steps - kSettleSteps ? kSettleWeight : 1.0f);
    // The limit is checked pessimistically: a negative bias may lower the
    // tracking estimate but never loosens the over-temperature margin.
    const float y_safe = s_f + fmaxf(st_.bias, 0.0f);
    if (y_safe - limit > worst) worst = y_safe - limit;
    if (out != nullptr) out[k] = y;
  }
  *track_cost = cost / static_cast<float>(n_steps);
  *over = worst;
}

Command Controller::plan(const Inputs& in, bool triggered) {
  last_plan_s_ = in.now_s;
  replan_ = false;
  st_.plans++;
  if (triggered) st_.triggered++;

  const float pos0 = clampf(in.pos, 0.0f, 1.0f);
  const bool paused = in.t_inc < in.setpoint + p_.supply_margin_k;
  if (paused && t_hot_ < in.setpoint + p_.supply_margin_k) {
    // No hot supply seen recently: nothing to plan for. Hold.
    plan_len_ = 0;
    st_.plan_cost = 0.0f;
    return Command{CMD_TARGET, clampf(pos0, 0.0f, in.pmax)};
  }

  int n = static_cast<int>(p_.horizon_s / kModelDt);
  if (n > kMaxHorizon) n = kMaxHorizon;
  if (n < kSettleSteps + kMoveAt[kMoves - 1]) n = kSettleSteps + kMoveAt[kMoves - 1];
  // While the boiler pauses the forward temperature cannot be held anyway.
  // Opening into the cold supply would only store up an overshoot for the
  // next burner start, so plan for the supply that will come back instead and
  // pre-position the valve for it.
  tw_override_ = paused ? t_hot_ : -1.0f;
  const bool refire = !paused && t_hot_ > st_.tinc_water + 5.0f;

  int best = -1;
  float best_cost = 0.0f;
  for (int seq = 0; seq < kSequences; ++seq) {
    const int s0 = seq / (kStepCount * kStepCount);
    const int s1 = (seq / kStepCount) % kStepCount;
    const int s2 = seq % kStepCount;
    float travel = fabsf(kSteps[s0]) + fabsf(kSteps[s1]) + fabsf(kSteps[s2]);
    int starts = (kSteps[s0] != 0.0f) + (kSteps[s1] != 0.0f) + (kSteps[s2] != 0.0f);
    float track = 0.0f, over = 0.0f;
    predict(seq, pos0, -1.0f, n, &track, &over, nullptr, in);
    float j = track + p_.lambda_travel * travel * 100.0f + p_.start_cost * starts +
              kLimitPenalty * over * over;
    if (refire) {
      float t2 = 0.0f, over2 = 0.0f;
      predict(seq, pos0, t_hot_, n, &t2, &over2, nullptr, in);
      j += kLimitPenalty * over2 * over2;
    }
    if (!isfinite(j)) continue;
    if (best < 0 || j < best_cost) {
      best = seq;
      best_cost = j;
    }
  }
  if (best < 0) {
    tw_override_ = -1.0f;
    force_anomaly(AN_NUMERIC, in.now_s);
    return Command{CMD_HOME, 0.0f};
  }

  float track = 0.0f, over = 0.0f;
  predict(best, pos0, -1.0f, n, &track, &over, plan_pred_, in);
  // A plan made for a hypothetical supply is not a prediction of the measured
  // temperature, so it must not raise prediction-miss triggers.
  plan_len_ = paused ? 0 : n;
  tw_override_ = -1.0f;
  plan_t0_ = in.now_s;
  st_.plan_cost = best_cost;
  st_.plan_moves[0] = kSteps[best / (kStepCount * kStepCount)];
  st_.plan_moves[1] = kSteps[(best / kStepCount) % kStepCount];
  st_.plan_moves[2] = kSteps[best % kStepCount];
  st_.traj_dt_s = n * kModelDt / kTrajPoints;
  for (int i = 0; i < kTrajPoints; ++i) {
    int k = ((i + 1) * n) / kTrajPoints - 1;
    st_.traj[i] = plan_pred_[k < 0 ? 0 : k];
  }
  return Command{CMD_TARGET, clampf(pos0 + st_.plan_moves[0], in.pmin, in.pmax)};
}

Command Controller::supervise(const Inputs& in) {
  const float dt = last_tick_s_ < 0.0 ? 0.0f : static_cast<float>(in.now_s - last_tick_s_);
  overshoot_s_ = in.overshoot ? overshoot_s_ + dt : 0.0f;
  bias_bad_s_ = fabsf(st_.bias) > kBiasLimitK ? bias_bad_s_ + dt : 0.0f;
  bool miss = false;
  if (plan_len_ > 0) {
    const int k = static_cast<int>((in.now_s - plan_t0_) / kModelDt);
    if (k >= 0 && k < plan_len_) {
      const float gap = fabsf(in.t_fwd - plan_pred_[k]);
      if (gap > p_.trigger_k) replan_ = true;
      miss = gap > kMissLimitK;
    }
  }
  miss_s_ = miss ? miss_s_ + dt : 0.0f;

  uint8_t an = AN_NONE;
  if (overshoot_s_ >= kOvershootPersistS || in.t_fwd >= in.setpoint + kSevereOverK) an = AN_OVERSHOOT;
  else if (bias_bad_s_ >= kBiasPersistS) an = AN_BIAS;
  else if (miss_s_ >= kMissPersistS) an = AN_TRACKING;
  if (an != AN_NONE) {
    force_anomaly(an, in.now_s);
    return Command{CMD_HOME, 0.0f};
  }
  return Command{CMD_KEEP, 0.0f};
}

Command Controller::cautious(const Inputs& in) {
  const float dt = last_tick_s_ < 0.0 ? 0.0f : static_cast<float>(in.now_s - last_tick_s_);
  const float ceiling = fminf(in.pmax, kCautiousMax);
  if (c_target_ < 0.0f) {
    c_target_ = clampf(in.pmin, 0.0f, ceiling);
    return Command{CMD_TARGET, c_target_};
  }
  if (!in.fwd_valid) return Command{CMD_KEEP, 0.0f};
  const float e = in.setpoint - in.t_fwd;
  const bool supply_ok = in.inc_valid && in.t_inc >= in.setpoint + kCautiousSupplyK;
  bool moved = false;
  if (-e > kCautiousHotK && in.now_s - c_last_close_s_ >= kCautiousHotGapS) {
    c_target_ = clampf(c_target_ - kCautiousHotStep, 0.0f, ceiling);
    c_last_close_s_ = in.now_s;
    moved = true;
  } else if (in.now_s - c_last_move_s_ >= kCautiousWaitS) {
    if (e > kCautiousOpenBand && supply_ok) {
      c_target_ = clampf(fmaxf(c_target_, in.pmin) + kCautiousOpenStep, 0.0f, ceiling);
      moved = true;
    } else if (e < -kCautiousCloseBand) {
      c_target_ = clampf(c_target_ - kCautiousCloseStep, fminf(in.pmin, c_target_), ceiling);
      moved = true;
    }
  }
  if (moved) c_last_move_s_ = in.now_s;

  st_.stable_s = fabsf(e) < kCautiousStableBand ? st_.stable_s + dt : 0.0f;
  if (!retry_blocked_ && st_.stable_s >= kCautiousStableS &&
      in.now_s - last_anomaly_s_ >= kRetryAfterAnomalyS) {
    st_.retries++;
    retry_s_ = in.now_s;
    reset_model();
    enter(MODE_MPC, in.now_s);
    return Command{CMD_KEEP, 0.0f};
  }
  return Command{CMD_TARGET, c_target_};
}

Command Controller::tick(const Inputs& in) {
  st_.scenario = in.scenario;
  if (last_tick_s_ >= 0.0 && in.now_s - last_tick_s_ > kResumeGapS) {
    // Control was suspended (DHW, flush, no demand): the plan is stale.
    plan_len_ = 0;
    replan_ = true;
    bias_bad_s_ = miss_s_ = overshoot_s_ = 0.0f;
    next_model_s_ = in.now_s;
    last_tick_s_ = in.now_s;
  }
  Command cmd{CMD_KEEP, 0.0f};
  const bool sensors_ok = in.inc_valid && in.ret_valid && in.fwd_valid;
  if (sensors_ok && st_.mode != MODE_LEGACY && in.now_s >= next_model_s_) {
    next_model_s_ = (next_model_s_ <= 0.0 ? in.now_s : next_model_s_) + kModelDt;
    if (next_model_s_ < in.now_s) next_model_s_ = in.now_s + kModelDt;
    model_step(in);
  }
  switch (st_.mode) {
    case MODE_MPC:
      if (!sensors_ok || !init_) break;   // L1 owns sensor faults
      cmd = supervise(in);
      if (cmd.kind == CMD_HOME) break;
      if (replan_ || in.now_s - last_plan_s_ >= p_.replan_s) {
        cmd = plan(in, replan_ && in.now_s - last_plan_s_ < p_.replan_s);
      }
      break;
    case MODE_HOMING:
      cmd = Command{CMD_HOME, 0.0f};
      break;
    case MODE_CAUTIOUS:
      cmd = cautious(in);
      break;
    default:
      break;
  }
  last_tick_s_ = in.now_s;
  return cmd;
}

}  // namespace mpc
}  // namespace hpn
