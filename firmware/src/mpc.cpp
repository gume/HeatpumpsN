#include "hpn/mpc.h"

#include <Arduino.h>
#include <Preferences.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <math.h>
#include <string.h>

#include "hpn/crc32.h"
#include "hpn/web_portal.h"

namespace hpn {
namespace mpc {
namespace {

constexpr const char* kNamespace = "hpnmpc";
constexpr const char* kKey = "blob";
constexpr uint32_t kMagic = 0x48504E4Du;  // "HPNM"
constexpr uint16_t kVersion = 2;  // no migration: another version loads defaults
constexpr uint32_t kHomingExtraMs = 10000u;   // stroke margin, as the portal's endstop home

struct Blob {
  uint32_t magic;
  uint16_t version;
  uint16_t size;
  uint32_t crc;
  uint8_t mode;          // preferred mode after boot: MODE_MPC or MODE_LEGACY
  uint8_t reserved[3];
  ParamSet sets[SC_COUNT];
};

// --- owned by T_control -----------------------------------------------------
Controller g_ctrl;
double g_now_s = 0.0;
uint32_t g_last_ms = 0;
uint8_t g_scenario = SC_FLOOR;
bool g_homing = false;
uint32_t g_home_until_ms = 0;
uint8_t g_prev_mode = MODE_MPC;

// --- shared, guarded by g_mutex -----------------------------------------------
SemaphoreHandle_t g_mutex = nullptr;
ParamSet g_sets[SC_COUNT];
bool g_pending_set[SC_COUNT] = {};
int g_pending_mode = -1;
uint8_t g_pref_mode = MODE_MPC;
uint32_t g_revision = 1;
Status g_status{};
uint8_t g_status_mode = MODE_MPC;
float g_compute_ms = 0.0f;
bool g_status_homing = false;
double g_status_now_s = 0.0;
char g_ack[192] = {};
bool g_ack_pending = false;

struct Lock {
  Lock() { if (g_mutex) xSemaphoreTake(g_mutex, portMAX_DELAY); }
  ~Lock() { if (g_mutex) xSemaphoreGive(g_mutex); }
};

float clampf(float v, float lo, float hi) { return v < lo ? lo : (v > hi ? hi : v); }

bool reading_ok(const SensorReading& r) { return r.is_valid && isfinite(r.value_c); }

uint32_t blob_crc(Blob& b) {
  const uint32_t saved = b.crc;
  b.crc = 0;
  const uint32_t crc = crc32_ieee(&b, sizeof(b));
  b.crc = saved;
  return crc;
}

// Caller holds the lock (or runs before the tasks exist).
bool save_locked() {
  Blob b{};
  b.magic = kMagic;
  b.version = kVersion;
  b.size = sizeof(Blob);
  b.mode = g_pref_mode;
  memcpy(b.sets, g_sets, sizeof(g_sets));
  b.crc = blob_crc(b);
  Preferences prefs;
  if (!prefs.begin(kNamespace, false)) return false;
  const bool ok = prefs.putBytes(kKey, &b, sizeof(b)) == sizeof(b);
  prefs.end();
  return ok;
}

bool load() {
  Preferences prefs;
  if (!prefs.begin(kNamespace, true)) return false;
  Blob b{};
  const size_t len = prefs.getBytesLength(kKey);
  bool ok = len == sizeof(Blob) && prefs.getBytes(kKey, &b, sizeof(b)) == sizeof(b);
  prefs.end();
  if (!ok || b.magic != kMagic || b.version != kVersion || b.size != sizeof(Blob) ||
      b.crc != blob_crc(b)) {
    return false;
  }
  for (uint8_t i = 0; i < SC_COUNT; ++i) {
    if (!validate(b.sets[i], nullptr)) return false;
  }
  memcpy(g_sets, b.sets, sizeof(g_sets));
  g_pref_mode = b.mode == MODE_LEGACY ? MODE_LEGACY : MODE_MPC;
  return true;
}

void set_ack(bool ok, const char* source, const char* what, const char* err) {
  Lock lock;
  snprintf(g_ack, sizeof(g_ack), "{\"ok\":%s,\"source\":\"%s\",\"what\":\"%s\",\"error\":\"%s\"}",
           ok ? "true" : "false", source ? source : "", what ? what : "", err ? err : "");
  g_ack_pending = true;
}

void start_homing(SystemState& state, const Config& config) {
  if (state.mix_free_run_dir != 0) {
    return;   // a portal sequence owns the valve; retry on the next tick
  }
  const uint32_t travel_ms = config.travel_time_ms > 0 ? config.travel_time_ms : 120000u;
  const uint32_t now = millis();
  // Deadline first: L1 reads these from the same core, but keep the portal's order.
  state.mix_free_run_until_ms = now + travel_ms + kHomingExtraMs;
  state.mix_free_run_dir = -1;
  state.mix_target_pos = 0.0f;
  g_homing = true;
  g_home_until_ms = now + travel_ms + kHomingExtraMs + 1000u;
}

uint8_t scenario_of(const SystemState& state) {
  const bool r1 = state.pump_rad1_requested || state.pump_rad1_phys;
  const bool r2 = state.pump_rad2_requested || state.pump_rad2_phys;
  return static_cast<uint8_t>((r1 ? SC_RAD1 : 0) + (r2 ? SC_RAD2 : 0));
}

void advance_clock() {
  const uint32_t ms = millis();
  if (g_last_ms != 0) g_now_s += static_cast<double>(static_cast<uint32_t>(ms - g_last_ms)) / 1000.0;
  g_last_ms = ms;
}

// Pending observer updates are applied by T_control, which owns g_ctrl.
void sync_pending() {
  int mode = -1;
  bool reload = false;
  ParamSet active{};
  {
    Lock lock;
    mode = g_pending_mode;
    g_pending_mode = -1;
    if (g_pending_set[g_scenario]) reload = true;
    for (bool& p : g_pending_set) p = false;
    active = g_sets[g_scenario];
  }
  if (reload) g_ctrl.set_params(active);
  if (mode >= 0) {
    if (g_homing && mode != MODE_LEGACY) {
      Lock lock;                                   // finish the stroke first
      if (g_pending_mode < 0) g_pending_mode = mode;
      return;
    }
    g_ctrl.request_mode(static_cast<uint8_t>(mode), g_now_s);
  }
}

void publish_status(float compute_ms) {
  Lock lock;
  g_status = g_ctrl.status();
  g_status_mode = g_ctrl.mode();
  g_status_homing = g_homing;
  g_status_now_s = g_now_s;
  if (compute_ms >= 0.0f) g_compute_ms = compute_ms;
}

void log_transition(uint8_t from, uint8_t to, const Status& st) {
  char detail[96];
  if (to == MODE_HOMING) {
    snprintf(detail, sizeof(detail), "MPC anomaly: %s; homing mixer, then cautious control",
             anomaly_name(st.anomaly));
    web::log_event(2, 1340, detail);
  } else if (from == MODE_CAUTIOUS && to == MODE_MPC) {
    snprintf(detail, sizeof(detail), "MPC resumed after stable cautious control (retry %u)",
             static_cast<unsigned>(st.retries));
    web::log_event(1, 1342, detail);
  } else {
    snprintf(detail, sizeof(detail), "Mixer controller mode %s -> %s", mode_name(from), mode_name(to));
    web::log_event(0, 1345, detail);
  }
}

void check_transition() {
  const uint8_t now_mode = g_ctrl.mode();
  if (now_mode != g_prev_mode) {
    log_transition(g_prev_mode, now_mode, g_ctrl.status());
    g_prev_mode = now_mode;
  }
}

// Scalar fields of a parameter set, in one table for JSON in and out.
struct Field {
  const char* name;
  size_t offset;
};
const Field kFields[] = {
    {"delay_s", offsetof(ParamSet, delay_s)},
    {"tau_fwd_s", offsetof(ParamSet, tau_fwd_s)},
    {"tau_inc_s", offsetof(ParamSet, tau_inc_s)},
    {"horizon_s", offsetof(ParamSet, horizon_s)},
    {"replan_s", offsetof(ParamSet, replan_s)},
    {"trigger_k", offsetof(ParamSet, trigger_k)},
    {"lambda_travel", offsetof(ParamSet, lambda_travel)},
    {"start_cost", offsetof(ParamSet, start_cost)},
    {"margin_k", offsetof(ParamSet, margin_k)},
    {"bias_alpha", offsetof(ParamSet, bias_alpha)},
    {"supply_margin_k", offsetof(ParamSet, supply_margin_k)},
    {"boost_s", offsetof(ParamSet, boost_s)},
    {"boost_scale", offsetof(ParamSet, boost_scale)},
};

float& field(ParamSet& p, const Field& f) {
  return *reinterpret_cast<float*>(reinterpret_cast<uint8_t*>(&p) + f.offset);
}

bool read_array(JsonVariantConst v, float* out, const char* name, char* err, size_t err_size) {
  if (v.isNull()) return true;
  JsonArrayConst a = v.as<JsonArrayConst>();
  if (a.isNull() || a.size() != static_cast<size_t>(kKnots)) {
    snprintf(err, err_size, "%s must be an array of %d numbers", name, kKnots);
    return false;
  }
  int i = 0;
  for (JsonVariantConst x : a) {
    if (!x.is<float>()) {
      snprintf(err, err_size, "%s[%d] is not a number", name, i);
      return false;
    }
    out[i++] = x.as<float>();
  }
  return true;
}

}  // namespace

void begin() {
  if (g_mutex == nullptr) g_mutex = xSemaphoreCreateMutex();
  for (uint8_t i = 0; i < SC_COUNT; ++i) g_sets[i] = default_params(i);
  if (!load()) {
    for (uint8_t i = 0; i < SC_COUNT; ++i) g_sets[i] = default_params(i);
    g_pref_mode = MODE_MPC;
    Serial.println("[mpc] no stored parameter sets; identified defaults in use");
  } else {
    Serial.printf("[mpc] parameter sets loaded, preferred mode %s\n", mode_name(g_pref_mode));
  }
  g_scenario = SC_FLOOR;
  g_ctrl.set_params(g_sets[SC_FLOOR]);
  g_ctrl.request_mode(g_pref_mode, 0.0);
  g_prev_mode = g_ctrl.mode();
  publish_status(-1.0f);
}

bool service_homing(const Config& config, SystemState& state) {
  advance_clock();
  sync_pending();
  check_transition();
  if (!g_homing && g_ctrl.mode() == MODE_HOMING) {
    // Entered by command or by an anomaly while the floor loop was idle.
    start_homing(state, config);
  }
  if (!g_homing) return false;
  if (static_cast<int32_t>(millis() - g_home_until_ms) >= 0) {
    state.mix_free_run_dir = 0;
    state.mix_free_run_until_ms = 0;
    state.pos_est_dr = 0.0f;
    state.mix_target_pos = 0.0f;
    g_homing = false;
    g_ctrl.on_homed(g_now_s);
    web::log_event(1, 1341, "MPC fallback: mixer homed, cautious control active");
    g_prev_mode = g_ctrl.mode();   // homing -> cautious is reported by 1341
    publish_status(-1.0f);
    return false;
  }
  publish_status(-1.0f);
  return true;
}

void floor_idle() {
  g_ctrl.reset_model();
  publish_status(-1.0f);
}

bool control(const Config& config, SystemState& state, bool flush_active) {
  if (g_ctrl.mode() == MODE_LEGACY) {
    publish_status(-1.0f);
    return false;
  }
  const uint8_t sc = scenario_of(state);
  if (sc != g_scenario) {
    g_scenario = sc;
    ParamSet p{};
    {
      Lock lock;
      p = g_sets[sc];
    }
    g_ctrl.set_params(p);
  }
  const float ceiling = isfinite(state.mix_ceiling) && state.mix_ceiling > 0.0f
                            ? clampf(state.mix_ceiling, 0.01f, 1.0f)
                            : mixer_ceiling(config);
  Inputs in{};
  in.now_s = g_now_s;
  in.pos = state.pos_est_dr;
  in.target = state.mix_target_pos;
  in.inc_valid = reading_ok(state.T_boiler);
  in.ret_valid = reading_ok(state.T_return_floor);
  in.fwd_valid = reading_ok(state.T_mix_out);
  in.t_inc = state.T_boiler.value_c;
  in.t_ret = state.T_return_floor.value_c;
  in.t_fwd = state.T_mix_out.value_c;
  in.setpoint = config.floor_setpoint_shadow;
  in.pmax = ceiling;
  in.pmin = fminf(mixer_minimum(config), ceiling);
  in.travel_s = (config.travel_time_ms > 0 ? config.travel_time_ms : 120000u) / 1000.0f;
  in.overshoot_delta = config.mix_overshoot_delta_c;
  in.scenario = sc;

  const uint32_t t0 = micros();
  const Command cmd = g_ctrl.tick(in);
  const float compute_ms = static_cast<float>(micros() - t0) / 1000.0f;

  check_transition();

  float target = state.mix_target_pos;
  if (cmd.kind == CMD_HOME) {
    start_homing(state, config);
    publish_status(compute_ms);
    return true;
  }
  if (cmd.kind == CMD_TARGET) target = cmd.target;
  if (flush_active) target = fminf(target, config.flush_max_pos);
  state.mix_target_pos = clampf(target, 0.0f, ceiling);
  publish_status(compute_ms > 0.5f ? compute_ms : -1.0f);
  return true;
}

bool apply_params(JsonVariantConst in, const char* source, char* err, size_t err_size) {
  err[0] = '\0';
  if (!in.is<JsonObjectConst>()) {
    snprintf(err, err_size, "body must be a JSON object");
    set_ack(false, source, "params", err);
    return false;
  }
  const char* set_name = in["set"] | "floor";
  bool targets[SC_COUNT] = {};
  if (strcmp(set_name, "all") == 0) {
    for (bool& t : targets) t = true;
  } else {
    const int sc = scenario_from_name(set_name);
    if (sc < 0) {
      snprintf(err, err_size, "set must be floor, rad1, rad2, both or all");
      set_ack(false, source, "params", err);
      return false;
    }
    targets[sc] = true;
  }
  const bool reset = in["reset"] | false;
  const bool persist = in["persist"] | true;

  ParamSet next[SC_COUNT];
  {
    Lock lock;
    memcpy(next, g_sets, sizeof(next));
  }
  for (uint8_t sc = 0; sc < SC_COUNT; ++sc) {
    if (!targets[sc]) continue;
    ParamSet& p = next[sc];
    const uint32_t rev = p.revision;
    if (reset) p = default_params(sc);
    if (!read_array(in["knots_pct"], p.knots_pct, "knots_pct", err, err_size) ||
        !read_array(in["xk"], p.xk, "xk", err, err_size)) {
      set_ack(false, source, "params", err);
      return false;
    }
    for (const Field& f : kFields) {
      JsonVariantConst v = in[f.name];
      if (v.isNull()) continue;
      if (!v.is<float>()) {
        snprintf(err, err_size, "%s is not a number", f.name);
        set_ack(false, source, "params", err);
        return false;
      }
      field(p, f) = v.as<float>();
    }
    const char* why = nullptr;
    if (!validate(p, &why)) {
      snprintf(err, err_size, "set %s rejected: %s out of bounds", scenario_name(sc), why);
      set_ack(false, source, "params", err);
      web::log_event(1, 1344, err);
      return false;
    }
    p.revision = rev + 1;
  }

  bool saved = true;
  {
    Lock lock;
    for (uint8_t sc = 0; sc < SC_COUNT; ++sc) {
      if (!targets[sc]) continue;
      g_sets[sc] = next[sc];
      g_pending_set[sc] = true;
    }
    g_revision++;
    if (persist) saved = save_locked();
  }
  char detail[96];
  snprintf(detail, sizeof(detail), "MPC parameters updated (%s, %s)%s", set_name, source,
           saved ? "" : "; NVS write failed");
  web::log_event(saved ? 0 : 1, 1343, detail);
  set_ack(saved, source, "params", saved ? "" : "NVS write failed");
  if (!saved) snprintf(err, err_size, "NVS write failed");
  return saved;
}

bool apply_params_json(const char* json, const char* source, char* err, size_t err_size) {
  JsonDocument doc;
  if (deserializeJson(doc, json) != DeserializationError::Ok) {
    snprintf(err, err_size, "invalid JSON");
    set_ack(false, source, "params", err);
    return false;
  }
  return apply_params(doc.as<JsonVariantConst>(), source, err, err_size);
}

bool apply_mode(const char* mode, const char* source, char* err, size_t err_size) {
  err[0] = '\0';
  int m = -1;
  if (strcmp(mode, "mpc") == 0) m = MODE_MPC;
  else if (strcmp(mode, "legacy") == 0) m = MODE_LEGACY;
  else if (strcmp(mode, "cautious") == 0) m = MODE_CAUTIOUS;
  if (m < 0) {
    snprintf(err, err_size, "mode must be mpc, legacy or cautious");
    set_ack(false, source, "mode", err);
    return false;
  }
  bool saved = true;
  {
    Lock lock;
    g_pending_mode = m;
    if (m != MODE_CAUTIOUS && g_pref_mode != m) {
      g_pref_mode = static_cast<uint8_t>(m);
      saved = save_locked();
    }
  }
  set_ack(true, source, "mode", saved ? "" : "preferred mode not saved");
  return true;
}

void status_json(JsonObject out, bool with_trajectory) {
  Status st;
  uint8_t mode, pref;
  float compute_ms;
  bool homing;
  double now_s;
  uint32_t rev;
  {
    Lock lock;
    st = g_status;
    mode = g_status_mode;
    pref = g_pref_mode;
    compute_ms = g_compute_ms;
    homing = g_status_homing;
    now_s = g_status_now_s;
    rev = g_revision;
  }
  out["mode"] = mode_name(mode);
  out["preferredMode"] = mode_name(pref);
  out["scenario"] = scenario_name(st.scenario);
  out["anomaly"] = anomaly_name(st.anomaly);
  out["anomalies"] = st.anomalies;
  out["retries"] = st.retries;
  out["homing"] = homing;
  out["modeForS"] = static_cast<uint32_t>(now_s - st.mode_since_s);
  out["stableS"] = static_cast<uint32_t>(st.stable_s);
  out["bias"] = st.bias;
  out["tincWater"] = st.tinc_water;
  out["modelFwd"] = st.model_fwd;
  out["planCost"] = st.plan_cost;
  out["boost"] = st.boost;
  JsonArray moves = out["movesPct"].to<JsonArray>();
  for (float m : st.plan_moves) moves.add(m * 100.0f);
  out["plans"] = st.plans;
  out["triggered"] = st.triggered;
  out["computeMs"] = compute_ms;
  out["paramsRevision"] = rev;
  if (with_trajectory) {
    out["trajDtS"] = st.traj_dt_s;
    JsonArray traj = out["traj"].to<JsonArray>();
    for (float v : st.traj) traj.add(v);
  }
}

void params_json(uint8_t scenario, JsonObject out) {
  if (scenario >= SC_COUNT) return;
  ParamSet p;
  {
    Lock lock;
    p = g_sets[scenario];
  }
  out["set"] = scenario_name(scenario);
  out["revision"] = p.revision;
  JsonArray k = out["knots_pct"].to<JsonArray>();
  JsonArray x = out["xk"].to<JsonArray>();
  for (int i = 0; i < kKnots; ++i) {
    k.add(p.knots_pct[i]);
    x.add(p.xk[i]);
  }
  for (const Field& f : kFields) out[f.name] = field(p, f);
}

uint32_t params_revision() {
  Lock lock;
  return g_revision;
}

bool take_ack(char* out, size_t out_size) {
  Lock lock;
  if (!g_ack_pending) return false;
  snprintf(out, out_size, "%s", g_ack);
  g_ack_pending = false;
  return true;
}

uint8_t mode() {
  Lock lock;
  return g_status_mode;
}

}  // namespace mpc
}  // namespace hpn
