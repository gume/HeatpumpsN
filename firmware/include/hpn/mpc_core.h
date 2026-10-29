#pragma once

#include <stddef.h>
#include <stdint.h>

// Predictive mixer control core: model-predictive controller (MPC), anomaly
// supervisor and cautious fallback.
//
// Pure C++ (no Arduino, no RTOS) so the same code runs on the ESP32 and in the
// host closed-loop bench (floor_simulator/tools). The firmware glue in mpc.cpp
// owns persistence, locking, MQTT/REST and homing execution.
//
// Model (identified in floor_simulator/log_analysis, Steps 3-4):
//   T_mix  = T_ret + x(pos) * (T_inc_water - T_ret)   x(): piecewise-linear law
//   T_fwd  = first-order sensor lag of T_mix delayed by the transport delay
//   T_inc_water is estimated from the lagged boiler-role sensor with a lead term.
// The model runs alongside the plant; the filtered gap between measured and
// modelled T_fwd is a bias added to every prediction (offset-free tracking).
//
// Every plan enumerates valve move sequences (one move at t0, t0+60 s and
// t0+180 s, each from kSteps), simulates the horizon for each, and picks the
// cheapest: tracking error + travel + motor starts + soft limit
// T_fwd <= setpoint + margin, checked for the current supply AND for a
// "boiler refires in 60 s" scenario. Only the first move is executed.
namespace hpn {
namespace mpc {

constexpr int kKnots = 11;
constexpr int kMoves = 3;
constexpr int kStepCount = 7;
constexpr int kSequences = kStepCount * kStepCount * kStepCount;  // 343
constexpr int kMaxHorizon = 120;      // model steps (10 s) -> 20 min
constexpr int kMaxDelayCells = 16;
constexpr float kModelDt = 10.0f;
constexpr int kTrajPoints = 16;       // published prediction samples

// Radiator state selects the parameter set. Each scenario has its own valve
// law (the radiator pumps change the effective hot fraction) and may have its
// own timing and weights.
enum Scenario : uint8_t { SC_FLOOR = 0, SC_RAD1, SC_RAD2, SC_BOTH, SC_COUNT };
const char* scenario_name(uint8_t scenario);
int scenario_from_name(const char* name);   // -1 when unknown

struct ParamSet {
  float knots_pct[kKnots];   // valve position knots [%], strictly increasing
  float xk[kKnots];          // hot fraction at the knots, non-decreasing, 0..1
  float delay_s;             // transport delay mixer -> forward sensor
  float tau_fwd_s;           // forward-sensor lag
  float tau_inc_s;           // boiler-role sensor lag (lead compensation)
  float horizon_s;           // prediction window
  float replan_s;            // periodic re-plan interval
  float trigger_k;           // measured vs planned gap forcing an immediate re-plan
  float lambda_travel;       // cost per % of planned travel [K^2 / %]
  float start_cost;          // cost per planned motor start [K^2]
  float margin_k;            // soft limit above setpoint
  float bias_alpha;          // bias filter gain per 10 s model step
  float supply_margin_k;     // supply must exceed setpoint by this to act
  // Heat-up boost: for boost_s after a floor-pump start or a setpoint increase
  // the travel and start costs are multiplied by boost_scale, so the planner
  // may open past the steady position and back off once the loop is warm.
  float boost_s;
  float boost_scale;
  // Also boost when |setpoint - T_fwd| exceeds boost_error_k with a usable
  // supply (e.g. after DHW charging); 0 disables. Not re-armed within boost_s
  // after a boost window ended.
  float boost_error_k;
  uint32_t revision;         // bumped on every accepted update
};

// Identified defaults. `scenario` applies the radiator multiplier (0.7928) to
// the hot fraction for the radiator scenarios.
ParamSet default_params(uint8_t scenario);

// Bounds check before a set may become active. On failure `err` names the
// first offending field.
bool validate(const ParamSet& p, const char** err);

enum Mode : uint8_t {
  MODE_LEGACY = 0,     // firmware PI+FF loop (l2_control.cpp), MPC idle
  MODE_MPC = 1,        // predictive control
  MODE_HOMING = 2,     // serious anomaly: drive fully closed, re-zero
  MODE_CAUTIOUS = 3,   // model-free fallback after homing
};
const char* mode_name(uint8_t mode);

enum Anomaly : uint8_t {
  AN_NONE = 0,
  AN_BIAS,           // model offset beyond kBiasLimit (e.g. wrong valve zero)
  AN_TRACKING,       // measurement kept leaving the plan despite re-plans
  AN_OVERSHOOT,      // forward temperature reached the overshoot latch
  AN_NUMERIC,        // non-finite prediction
  AN_MANUAL,         // forced by command (fallback test)
};
const char* anomaly_name(uint8_t anomaly);

struct Inputs {
  double now_s;           // monotonic seconds (wrap-free, see mpc.cpp)
  float pos;              // dead-reckoned valve position 0..1
  float target;           // current commanded target 0..1
  float t_inc;            // boiler-role sensor (HX outlet)
  float t_ret;
  float t_fwd;
  bool inc_valid, ret_valid, fwd_valid;
  float setpoint;
  float pmin, pmax;       // operating window 0..1 (minimum opening, ceiling)
  float travel_s;         // full stroke time
  float overshoot_delta;  // mix_overshoot_delta_c (overshoot above the reference)
  uint8_t scenario;       // radiator state
};

enum CommandKind : uint8_t { CMD_KEEP = 0, CMD_TARGET, CMD_HOME };

struct Command {
  CommandKind kind;
  float target;           // valid for CMD_TARGET
};

struct Status {
  uint8_t mode;
  uint8_t scenario;
  uint8_t anomaly;          // last anomaly that forced the fallback
  uint8_t retries;          // automatic returns from cautious to MPC
  bool boost;               // heat-up boost window active
  float bias;
  float tinc_water;         // lead-compensated supply estimate
  float model_fwd;          // model forward temperature (without bias)
  float plan_cost;
  float plan_moves[kMoves]; // planned moves [fraction]
  float traj[kTrajPoints];  // predicted T_fwd every horizon/kTrajPoints
  float traj_dt_s;
  uint32_t plans;
  uint32_t triggered;       // plans forced by a prediction miss
  uint32_t anomalies;
  double mode_since_s;
  float stable_s;           // cautious: time within band (exit criterion)
};

class Controller {
 public:
  Controller();

  // Active parameter set (copied). Changing the scenario's set re-plans.
  void set_params(const ParamSet& params);
  const ParamSet& params() const { return p_; }

  // Model state is dropped (floor circuit stopped). The mode is kept so a
  // serious anomaly is not forgotten by a demand cycle.
  void reset_model();

  // Mode requests from the glue (commands, persistence).
  void request_mode(uint8_t mode, double now_s);
  void force_anomaly(uint8_t anomaly, double now_s);
  // The glue finished the homing stroke; the valve is at 0.
  void on_homed(double now_s);

  // One control tick (2 s in the firmware).
  Command tick(const Inputs& in);

  Status status() const { return st_; }
  uint8_t mode() const { return st_.mode; }

 private:
  void model_step(const Inputs& in);
  Command plan(const Inputs& in, bool triggered);
  void predict(int seq, float pos0, float hot_tw, int n_steps, float* track_cost, float* over,
               float* out, const Inputs& in) const;
  float law(float pos) const;
  Command cautious(const Inputs& in);
  Command supervise(const Inputs& in);
  void enter(uint8_t mode, double now_s);

  ParamSet p_;
  Status st_;
  // model
  bool init_ = false;
  float ring_[kMaxDelayCells];
  int ring_n_ = 0;
  int ring_pos_ = 0;
  float s_f_ = 0.0f;
  float prev_inc_ = 0.0f;
  float slope_ = 0.0f;
  float t_hot_ = 0.0f;
  double next_model_s_ = 0.0;
  float tw_override_ = -1.0f;   // planning supply while the boiler pauses
  // plan
  double last_plan_s_ = -1e9;
  double plan_t0_ = 0.0;
  float plan_pred_[kMaxHorizon];
  int plan_len_ = 0;
  bool replan_ = true;
  // supervisor
  float bias_bad_s_ = 0.0f;
  float miss_s_ = 0.0f;
  float overshoot_s_ = 0.0f;
  // Supervision reference: follows setpoint increases at once but setpoint
  // decreases only at kRefDropKPerS, so a deliberate setpoint drop is not an
  // overshoot while the valve closes.
  float sp_ref_ = 0.0f;
  bool sp_ref_init_ = false;
  float last_sp_ = 0.0f;
  double boost_until_s_ = -1.0;
  double last_tick_s_ = -1.0;
  double last_anomaly_s_ = -1e9;
  double retry_s_ = -1e9;
  // cautious
  float c_target_ = -1.0f;
  double c_last_move_s_ = 0.0;
  double c_last_close_s_ = -1e9;
  bool retry_blocked_ = false;
};

}  // namespace mpc
}  // namespace hpn
