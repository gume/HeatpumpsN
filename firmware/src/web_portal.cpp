// Third-party/Arduino headers first: lwIP defines an ERR_OK enum member, and
// hpn/state.h defines an ERR_OK macro for the controller error codes. Including
// the network stack before state.h keeps the two names from colliding.
#include <Arduino.h>
#include <ArduinoJson.h>
#include <AsyncJson.h>
#include <AsyncWebSocket.h>
#include <ESPAsyncWebServer.h>
#include <LittleFS.h>
#include <Preferences.h>
#include <Update.h>
#include <WiFi.h>
#include <esp_system.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <vector>

#include "hpn/web_portal.h"
#include "hpn/config_store.h"
#include "hpn/crc32.h"
#include "hpn/l0_io.h"
#include "hpn/logger.h"
#include "hpn/mqtt_client.h"
#include "hpn/sensor_roles.h"
#include "hpn/wifi_client.h"

#if defined(HPN_IO_EMULATION)
#include "hpn/sim.h"
#endif

namespace hpn {
namespace web {
namespace {

// ---------------------------------------------------------------------------
// Constants and small data structures
// ---------------------------------------------------------------------------

constexpr uint32_t kBroadcastIntervalMs = 250;
constexpr size_t kAccessLogSize = 64;

struct AccessEntry {
  uint32_t seq;
  uint32_t ms;
  char ip[16];
  char method[8];
  char path[64];
  int status;
};

struct CounterEntry {
  uint32_t cycles;
  uint32_t runtime_s;
  uint32_t last_change_ms;
  bool last_state;
  bool initialized;
};

enum CounterId : uint8_t {
  CTR_HEATER = 0,
  CTR_RAD1,
  CTR_RAD2,
  CTR_FLOOR,
  CTR_MIX_OPEN,
  CTR_MIX_CLOSE,
  CTR_COUNT,
};

constexpr const char* kCounterNamespace = "hpnctr";
constexpr const char* kCounterKey = "counters";
constexpr uint32_t kCounterMagic = 0x48504E54u;  // "HPNT"
constexpr uint16_t kCounterVersion = 2;
constexpr uint32_t kCounterSaveIntervalMs = 3600000u;

struct CounterBlobPrefix {
  uint32_t magic;
  uint16_t version;
  uint16_t size;
};

struct CounterBlob {
  uint32_t magic;
  uint16_t version;
  uint16_t size;
  uint32_t crc;
  uint32_t cycles[CTR_COUNT];
  uint32_t runtime_s[CTR_COUNT];
};

struct Snapshot {
  SystemState state;
  uint32_t captured_ms;
  bool wifi_connected;
  int8_t wifi_rssi;
  char wifi_ip[16];
  char wifi_state[16];
  bool mqtt_enabled;
  bool mqtt_connected;
  char mqtt_state[20];
  char mqtt_broker[64];
  uint16_t mqtt_port;
  uint32_t mqtt_connect_count;
  uint32_t mqtt_disconnect_count;
  uint32_t mqtt_last_disconnect_ms;
  uint32_t mqtt_errors;
};

enum class Sequence : uint8_t { None = 0, AntiSeize, EndstopHome, MixerSweep };

// ---------------------------------------------------------------------------
// Globals
// ---------------------------------------------------------------------------

AsyncWebServer g_server(80);
AsyncWebSocket g_ws("/ws");

const Config* g_config = nullptr;
SystemState* g_state = nullptr;

portMUX_TYPE g_access_mux = portMUX_INITIALIZER_UNLOCKED;
AccessEntry g_access[kAccessLogSize] = {};
uint32_t g_access_head = 0;
uint32_t g_access_count = 0;
uint32_t g_access_seq = 0;

portMUX_TYPE g_counter_mux = portMUX_INITIALIZER_UNLOCKED;
CounterEntry g_counters[CTR_COUNT] = {};
uint32_t g_counter_runtime_ms = 0;
bool g_counters_dirty = false;
bool g_counters_persisted = false;
bool g_counter_save_requested = false;
uint32_t g_last_counter_save_ms = 0;

constexpr size_t kWsIpSlots = 4;
struct WsIpEntry {
  uint32_t id;
  char ip[16];
};
WsIpEntry g_ws_ips[kWsIpSlots] = {};

uint32_t g_last_broadcast_ms = 0;
uint32_t g_last_cleanup_ms = 0;
bool g_last_wifi_connected = false;

bool g_test_active = false;
uint32_t g_test_end_ms = 0;
Sequence g_sequence = Sequence::None;
uint8_t g_sequence_step = 0;
uint32_t g_sequence_next_ms = 0;

bool g_reboot_requested = false;
uint32_t g_reboot_at_ms = 0;

bool g_ota_ok = false;
String g_ota_error;

// ---------------------------------------------------------------------------
// Small helpers
// ---------------------------------------------------------------------------

void copy_str(char* dst, size_t n, const char* src) {
  if (n == 0) {
    return;
  }
  snprintf(dst, n, "%s", src == nullptr ? "" : src);
}

// Reset cause latched by the SoC for the current boot. Reading it does not
// clear it, so it is safe to query from the status handler at any time.
const char* reset_reason_name() {
  switch (esp_reset_reason()) {
    case ESP_RST_POWERON:
      return "power-on";
    case ESP_RST_EXT:
      return "external";
    case ESP_RST_SW:
      return "software";
    case ESP_RST_PANIC:
      return "panic";
    case ESP_RST_INT_WDT:
      return "interrupt-watchdog";
    case ESP_RST_TASK_WDT:
      return "task-watchdog";
    case ESP_RST_WDT:
      return "other-watchdog";
    case ESP_RST_DEEPSLEEP:
      return "deep-sleep";
    case ESP_RST_BROWNOUT:
      return "brownout";
    case ESP_RST_SDIO:
      return "sdio";
    default:
      return "unknown";
  }
}

const char* mode_name(OperationalMode mode) {
  switch (mode) {
    case MODE_HEAT:
      return "heat";
    case MODE_SUMMER:
      return "summer";
    case MODE_ANTI_SEIZE:
      return "anti_seize";
    case MODE_TEST_OVERRIDE:
      return "test";
    default:
      return "unknown";
  }
}

const char* divergence_name(DivergenceReason reason) {
  switch (reason) {
    case DIV_NONE:
      return "none";
    case DIV_FLOOR_DELTA_T_COLLAPSED:
      return "floor_delta_t_collapsed";
    case DIV_OVERTEMP_PAUSE:
      return "overtemp_pause";
    case DIV_LONG_PAUSE:
      return "long_pause";
    case DIV_ANTI_SHORT_CYCLE:
      return "anti_short_cycle";
    case DIV_DEMAND_LOSS:
      return "demand_loss";
    case DIV_AFTER_RUN:
      return "after_run";
    case DIV_DHW_PRIORITY_ACTIVE:
      return "dhw_priority_active";
    default:
      return "unknown";
  }
}

const char* flush_name(FlushState state) {
  switch (state) {
    case FLUSH_ACTIVE:
      return "active";
    case FLUSH_PAUSED_DELTA_T:
      return "paused_delta_t";
    default:
      return "idle";
  }
}

const char* sequence_name() {
  return g_sequence == Sequence::AntiSeize ? "anti-seize" : "none";
}

void add_alarm_strings(JsonArray out, uint32_t alarms) {
  if (alarms & ALARM_OVERTEMP_HARD) out.add("overtemp_hard");
  if (alarms & WARN_OVERTEMP_HIGH) out.add("overtemp_high");
  if (alarms & WARN_GRADIENT_CUTOFF) out.add("gradient_cutoff");
  if (alarms & ALARM_SENSOR_FAULT_MIX) out.add("sensor_fault_mix");
  if (alarms & WARN_SENSOR_FAULT_RET) out.add("sensor_fault_return");
  if (alarms & WARN_SENSOR_FAULT_BOILER) out.add("sensor_fault_boiler");
  if (alarms & WARN_HEATER_NO_PUMP) out.add("heater_no_pump");
  if (alarms & WARN_ENCODER_STALE) out.add("encoder_stale");
  if (alarms & WARN_ENCODER_DIVERGED) out.add("encoder_diverged");
  if (alarms & WARN_PUMP_DRY_RUN) out.add("pump_dry_run");
  if (alarms & ALARM_WELDED_RELAY) out.add("welded_relay");
  if (alarms & WARN_NVS_CORRUPTED) out.add("nvs_corrupted");
  if (alarms & WARN_MIX_MOTION_OVERRUN) out.add("mix_motion_overrun");
  if (alarms & WARN_MIX_NO_RESPONSE) out.add("mix_no_response");
  if (alarms & WARN_MIX_OVERSHOOT) out.add("mix_overshoot");
  if (alarms & WARN_SENSOR_ROLE_SUSPECT) out.add("sensor_role_suspect");
  if (alarms & WARN_DHW_INFER_TIMEOUT) out.add("dhw_infer_timeout");
}

void add_temp_json(JsonObject temps, JsonObject health, const char* key, const SensorReading& reading,
                   uint32_t now_ms) {
  if (isfinite(reading.value_c)) {
    temps[key] = reading.value_c;
  } else {
    temps[key] = nullptr;
  }

  JsonObject h = health[key].to<JsonObject>();
  h["valid"] = reading.is_valid;
  h["errors"] = reading.consecutive_errors;
  h["age"] = reading.last_valid_ms == 0 ? -1 : static_cast<long>((now_ms - reading.last_valid_ms) / 1000);
  if (isfinite(reading.value_c)) {
    h["lastValue"] = reading.value_c;
  } else {
    h["lastValue"] = nullptr;
  }
}

void add_sensor_json(JsonArray sensors, const char* id, const char* label, const SensorReading& reading,
                     uint8_t error_threshold, uint32_t now_ms) {
  JsonObject s = sensors.add<JsonObject>();
  s["id"] = id;
  s["label"] = label;
  if (isfinite(reading.value_c)) {
    s["val"] = reading.value_c;
  } else {
    s["val"] = nullptr;
  }
  s["age"] = reading.last_valid_ms == 0 ? -1 : static_cast<long>((now_ms - reading.last_valid_ms) / 1000);
  s["valid"] = reading.is_valid;
  s["errors"] = reading.consecutive_errors;

  const char* health = "ok";
  if (!reading.is_valid) {
    if (reading.last_valid_ms == 0) {
      health = "disconnected";
    } else if (reading.consecutive_errors >= error_threshold) {
      health = "error";
    } else {
      health = "stale";
    }
  }
  s["state"] = health;
}

JsonObject add_circuit_json(JsonObject circuits, const char* key, bool intended, bool demand,
                            bool phys, uint32_t demand_age_s, uint32_t after_run_age_s,
                            uint32_t after_run_limit_s) {
  JsonObject c = circuits[key].to<JsonObject>();
  c["intended"] = intended ? 1 : 0;
  c["demand"] = demand ? 1 : 0;
  c["cmd"] = demand ? 1 : 0;  // compatibility with older portal builds
  c["phys"] = phys ? 1 : 0;
  c["demandAge"] = demand_age_s;
  const bool after_run = !demand && (after_run_limit_s > after_run_age_s);
  c["afterRun"] = after_run ? after_run_limit_s - after_run_age_s : 0;
  return c;
}

// ---------------------------------------------------------------------------
// Logging
// ---------------------------------------------------------------------------

void log_access_raw(const char* ip, const char* method, const char* path, int status) {
  AccessEntry entry{};
  entry.ms = millis();
  entry.status = status;
  copy_str(entry.ip, sizeof(entry.ip), ip);
  copy_str(entry.method, sizeof(entry.method), method);
  copy_str(entry.path, sizeof(entry.path), path);

  taskENTER_CRITICAL(&g_access_mux);
  entry.seq = ++g_access_seq;
  g_access[g_access_head] = entry;
  g_access_head = (g_access_head + 1) % kAccessLogSize;
  if (g_access_count < kAccessLogSize) {
    g_access_count++;
  }
  taskEXIT_CRITICAL(&g_access_mux);

  Serial.printf("[web] %s %s from %s -> %d\n", entry.method, entry.path, entry.ip, entry.status);
}

void log_access_request(AsyncWebServerRequest* request, int status) {
  if (request == nullptr) {
    return;
  }
  // The emulation page polls /api/sim continuously; excluding it keeps the
  // serial console and the in-RAM access log readable.
  if (request->url().startsWith("/api/sim")) {
    return;
  }
  String ip = request->client() != nullptr ? request->client()->remoteIP().toString() : String("?");
  log_access_raw(ip.c_str(), request->methodToString(), request->url().c_str(), status);
}

// ---------------------------------------------------------------------------
// Snapshot and live state JSON
// ---------------------------------------------------------------------------

void get_snapshot(Snapshot& out) {
  out = Snapshot{};
  // The real-time core publishes a coherent copy; reading g_state in place from
  // this task could capture a half-written SensorReading or wall clock.
  if (!state_snapshot(out.state) && g_state != nullptr) {
    // No publish yet: the owning tasks are not running, so a direct copy is
    // safe and keeps the portal usable during early bring-up.
    out.state = *g_state;
  }
  out.captured_ms = millis();

  const wifi::Status& ws = wifi::status();
  out.wifi_connected = ws.connected;
  out.wifi_rssi = ws.rssi;
  copy_str(out.wifi_ip, sizeof(out.wifi_ip), WiFi.localIP().toString().c_str());
  copy_str(out.wifi_state, sizeof(out.wifi_state), wifi::state_name());

  const mqtt::Status& ms = mqtt::status();
  out.mqtt_enabled = ms.enabled;
  out.mqtt_connected = ms.connected;
  copy_str(out.mqtt_state, sizeof(out.mqtt_state), ms.state);
  copy_str(out.mqtt_broker, sizeof(out.mqtt_broker), ms.broker);
  out.mqtt_port = ms.port;
  out.mqtt_connect_count = ms.connect_count;
  out.mqtt_disconnect_count = ms.disconnect_count;
  out.mqtt_last_disconnect_ms = ms.last_disconnect_ms;
  out.mqtt_errors = ms.errors;
}

// Published state for request handlers that only need to read. The real-time
// core keeps writing, so handlers must go through this instead of g_state.
SystemState published_state() {
  SystemState state;
  if (!state_snapshot(state) && g_state != nullptr) {
    state = *g_state;
  }
  return state;
}

void build_state_json(const Snapshot& s, JsonDocument& doc) {
  const SystemState& st = s.state;
  const Config& cfg = *g_config;
  const uint32_t now_ms = s.captured_ms;

  doc["ts"] = now_ms;
  doc["uptime"] = now_ms / 1000;

  JsonObject mode = doc["mode"].to<JsonObject>();
  mode["current"] = mode_name(st.mode);
  mode["source"] = st.mode == MODE_TEST_OVERRIDE ? "test" : "local";

  JsonObject mqtt = doc["mqtt"].to<JsonObject>();
  mqtt["enabled"] = s.mqtt_enabled;
  mqtt["connected"] = s.mqtt_connected;
  mqtt["state"] = s.mqtt_state;
  mqtt["broker"] = s.mqtt_broker;
  mqtt["port"] = s.mqtt_port;
  mqtt["connectCount"] = s.mqtt_connect_count;
  mqtt["disconnectCount"] = s.mqtt_disconnect_count;
  mqtt["lastDisconnect"] = s.mqtt_last_disconnect_ms;
  mqtt["errors"] = s.mqtt_errors;

  JsonObject circuits = doc["circuits"].to<JsonObject>();
  add_circuit_json(circuits, "rad1", st.pump_rad1_intended, st.pump_rad1_demand,
                   st.pump_rad1_phys, st.demand_age_s[CIRCUIT_RAD1],
                   st.after_run_age_s[CIRCUIT_RAD1], cfg.pump_stop_after_heating_s);
  add_circuit_json(circuits, "rad2", st.pump_rad2_intended, st.pump_rad2_demand,
                   st.pump_rad2_phys, st.demand_age_s[CIRCUIT_RAD2],
                   st.after_run_age_s[CIRCUIT_RAD2], cfg.pump_stop_after_heating_s);
  JsonObject floor =
      add_circuit_json(circuits, "floor", st.pump_floor_intended, st.pump_floor_demand,
                       st.pump_floor_phys, st.demand_age_s[CIRCUIT_FLOOR],
                       st.after_run_age_s[CIRCUIT_FLOOR], cfg.pump_stop_after_heating_s);
  floor["mixerPos"] = st.pos_est_dr * 100.0f;
  floor["mixerTarget"] = st.mix_target_pos * 100.0f;
  floor["mixerMaxPct"] = (isfinite(st.mix_ceiling) ? st.mix_ceiling : mixer_ceiling(cfg)) * 100.0f;
  floor["mixerMaxAbsPct"] = cfg.mix_max_open_pct;
  floor["mixerMinPct"] = cfg.mix_min_open_pct;
  floor["mixerMaxDynamic"] = cfg.mix_max_dynamic;
  floor["mixerOvershoot"] = st.mix_overshoot;
  floor["mixerMotion"] = st.mix_motion;
  floor["mixerDir"] = st.mix_dir;
  floor["mixerEmergency"] = st.mix_emergency;
  floor["encoderPos"] = st.pos_encoder * 100.0f;
  floor["encoderValid"] = st.pos_encoder_valid;

  JsonObject temps = doc["temps"].to<JsonObject>();
  JsonObject health = doc["tempHealth"].to<JsonObject>();
  add_temp_json(temps, health, "boilerSupply", st.T_boiler, now_ms);
  add_temp_json(temps, health, "mixerHotIn", st.T_boiler, now_ms);
  add_temp_json(temps, health, "mixerColdIn", st.T_return_floor, now_ms);
  add_temp_json(temps, health, "mixerForward", st.T_mix_out, now_ms);
  add_temp_json(temps, health, "return", st.T_return_floor, now_ms);

  JsonObject heater = doc["heater"].to<JsonObject>();
  heater["automatic"] = true;
  heater["cmd"] = st.heater_intended ? 1 : 0;
  heater["requested"] = st.heater_requested ? 1 : 0;
  heater["phys"] = st.heater_phys ? 1 : 0;
  const bool any_demand = st.pump_floor_demand || st.pump_rad1_demand || st.pump_rad2_demand;
  if (!st.heater_requested) {
    heater["reason"] = "no_demand";
  } else if (st.heater_phys) {
    heater["reason"] = "heating";
  } else if (!any_demand || (st.active_alarms & WARN_HEATER_NO_PUMP)) {
    heater["reason"] = "pump_required";
  } else if (st.divergence_reason == DIV_ANTI_SHORT_CYCLE) {
    heater["reason"] = "anti_short_cycle";
  } else {
    heater["reason"] = "starting";
  }

  JsonObject safety = doc["safety"].to<JsonObject>();
  JsonArray flags = safety["flags"].to<JsonArray>();
  add_alarm_strings(flags, st.active_alarms);
  safety["latched"].to<JsonArray>();
  safety["errorCode"] = st.system_error_code;
  char error_text[160];
  format_error_text(st, error_text, sizeof(error_text));
  safety["errorText"] = error_text;
  safety["alarmCount"] = flags.size();

  JsonArray sensors = doc["sensors"].to<JsonArray>();
  add_sensor_json(sensors, cfg.sensor_id_boiler[0] != '\0' ? cfg.sensor_id_boiler : "boiler",
                  "Mixer hot in", st.T_boiler, cfg.sensor_error_threshold, now_ms);
  add_sensor_json(sensors, cfg.sensor_id_ret[0] != '\0' ? cfg.sensor_id_ret : "return",
                  "Mixer cold in / return", st.T_return_floor, cfg.sensor_error_threshold,
                  now_ms);
  add_sensor_json(sensors, cfg.sensor_id_mix[0] != '\0' ? cfg.sensor_id_mix : "mix",
                  "Mixer forward", st.T_mix_out, cfg.sensor_error_threshold, now_ms);

  JsonObject test = doc["test"].to<JsonObject>();
  test["active"] = g_test_active;
  test["remaining"] = st.test_mode_rem_s;
  test["sequence"] = sequence_name();

  JsonObject system = doc["system"].to<JsonObject>();
  system["freeHeap"] = ESP.getFreeHeap();
  system["errorCode"] = st.system_error_code;
  system["errorText"] = error_text;
  system["divergence"] = divergence_name(st.divergence_reason);
  system["flush"] = flush_name(st.flush_state);
  system["rssi"] = s.wifi_rssi;
  system["hostname"] = cfg.hostname;
  system["epoch"] = st.wall_time_epoch_s;
  system["ntpSynced"] = st.wall_time_epoch_s > 1600000000ULL;
  system["dhwActive"] = st.dhw_active;
  system["dhwInferred"] = st.dhw_inferred;
  system["dhwMeterActive"] = st.dhw_meter_active;
  system["pumpPowerCheck"] = cfg.pump_power_check_enabled;
  system["pumpPowerSenseAvailable"] = l0::power_measurement_available(cfg);
  system["mixerIntegrator"] = st.integrator_val;

  JsonObject wifi_obj = doc["wifi"].to<JsonObject>();
  wifi_obj["configured"] = wifi::status().configured;
  wifi_obj["connected"] = s.wifi_connected;
  wifi_obj["rssi"] = s.wifi_rssi;
  wifi_obj["ip"] = s.wifi_ip;
  wifi_obj["state"] = s.wifi_state;
  wifi_obj["ssid"] = cfg.wifi_ssid;

  JsonObject power = doc["power"].to<JsonObject>();
  power["floor"] = st.pump_power_w[0];
  power["rad1"] = st.pump_power_w[1];
  power["rad2"] = st.pump_power_w[2];
  power["mixer"] = st.mixer_power_w;
  // Per-channel meter presence (CurrentChannel order: floor, rad1, rad2,
  // mixer), so the portal can mark a reading as "not fitted" instead of
  // showing a bare 0 W. senseAvailable says whether the front end can measure
  // at all, which is what gates dry-run/welded protection.
  JsonArray meter_enabled = power["enabled"].to<JsonArray>();
  for (uint8_t i = 0; i < kCurrentChannelCount; ++i) {
    meter_enabled.add(power_meter_present(cfg, i));
  }
  power["senseAvailable"] = l0::power_measurement_available(cfg);

#if defined(HPN_IO_EMULATION)
  const sim::Snapshot sim_state = sim::snapshot();
  JsonObject sim_obj = doc["sim"].to<JsonObject>();
  sim_obj["enabled"] = true;
  sim_obj["revision"] = sim_state.revision;
#else
  JsonObject sim_obj = doc["sim"].to<JsonObject>();
  sim_obj["enabled"] = false;
  sim_obj["revision"] = 0;
#endif
}

// ---------------------------------------------------------------------------
// Configuration JSON
// ---------------------------------------------------------------------------

void fill_network(JsonObject o, const Config& c, bool include_secrets = false) {
  o["wifi_ssid"] = c.wifi_ssid;
  o["ap_ssid"] = c.ap_ssid;
  o["mqtt_host"] = c.mqtt_host;
  o["mqtt_port"] = c.mqtt_port;
  o["mqtt_user"] = c.mqtt_user;
  o["mqtt_base_topic"] = c.mqtt_base_topic;
  o["mqtt_log_enabled"] = c.mqtt_log_enabled;
  o["mqtt_log_slow_topic"] = c.mqtt_log_slow_topic;
  o["mqtt_log_event_topic"] = c.mqtt_log_event_topic;
  o["ntp_enabled"] = c.ntp_enabled;
  o["ntp_server"] = c.ntp_server;
  o["timezone"] = c.timezone;
  o["mixer_meter_device"] = c.mixer_meter_device;
  o["mixer_meter_field"] = c.mixer_meter_field;
  o["mixer_meter_scale"] = c.mixer_meter_scale;
  o["mixer_meter_topic"] = c.mixer_meter_topic;
  o["dhw_meter_device"] = c.dhw_meter_device;
  o["dhw_meter_entity"] = c.dhw_meter_entity;
  o["dhw_meter_field"] = c.dhw_meter_field;
  o["dhw_meter_topic"] = c.dhw_meter_topic;
  if (include_secrets) {
    // Configuration backups must be restorable, so the file carries the
    // credentials verbatim. The normal GET path only advertises their presence.
    o["wifi_password"] = c.wifi_password;
    o["ap_password"] = c.ap_password;
    o["mqtt_pass"] = c.mqtt_pass;
  } else {
    o["has_wifi_password"] = c.wifi_password[0] != '\0';
    o["has_ap_password"] = c.ap_password[0] != '\0';
    o["has_mqtt_password"] = c.mqtt_pass[0] != '\0';
  }
}

void fill_safety(JsonObject o, const Config& c) {
  o["t_max_hard"] = c.t_max_hard;
  o["t_max_warn"] = c.t_max_warn;
  o["t_overtemp_hysteresis"] = c.t_overtemp_hysteresis;
  o["grad_limit"] = c.grad_limit;
  o["mix_safe_pos"] = c.mix_safe_pos;
  o["mix_max_open_pct"] = c.mix_max_open_pct;
  o["mix_min_open_pct"] = c.mix_min_open_pct;
  o["mix_max_dynamic"] = c.mix_max_dynamic;
  o["mix_max_below_pct"] = c.mix_max_below_pct;
  o["mix_max_above_pct"] = c.mix_max_above_pct;
  o["mix_max_switch_c"] = c.mix_max_switch_c;
  o["mix_max_hysteresis_c"] = c.mix_max_hysteresis_c;
  o["mix_overshoot_delta_c"] = c.mix_overshoot_delta_c;
  o["mix_overshoot_recovery_c"] = c.mix_overshoot_recovery_c;
  o["mix_overshoot_target_pct"] = c.mix_overshoot_target_pct;
  o["sensor_role_check_enabled"] = c.sensor_role_check_enabled;
  o["sensor_role_tolerance_c"] = c.sensor_role_tolerance_c;
  o["dhw_infer_enabled"] = c.dhw_infer_enabled;
  o["dhw_infer_drop_c"] = c.dhw_infer_drop_c;
  o["dhw_infer_window_s"] = c.dhw_infer_window_s;
  o["dhw_infer_recovery_c"] = c.dhw_infer_recovery_c;
  o["dhw_infer_max_s"] = c.dhw_infer_max_s;
  o["boiler_min_off_ms"] = c.boiler_min_off_ms;
  o["demand_timeout_s"] = c.demand_timeout_s;
  o["pump_stop_after_heating_s"] = c.pump_stop_after_heating_s;
  o["long_pause_s"] = c.long_pause_s;
  o["anti_seize_interval_s"] = c.anti_seize_interval_s;
  o["test_mode_max_s"] = c.test_mode_max_s;
  o["sensor_error_threshold"] = c.sensor_error_threshold;
}

void fill_hardware(JsonObject o, const Config& c) {
  o["setpoint_default"] = c.setpoint_default;
  o["deadband_pos"] = c.deadband_pos;
  o["deadband_temp"] = c.deadband_temp;
  o["travel_time_ms"] = c.travel_time_ms;
  o["tick_ms"] = c.tick_ms;
  o["temp_poll_interval_ms"] = c.temp_poll_interval_ms;
  o["recalc_threshold"] = c.recalc_threshold;
  o["encoder_check_interval_s"] = c.encoder_check_interval_s;
  o["encoder_check_pulses"] = c.encoder_check_pulses;
  o["encoder_staleness_s"] = c.encoder_staleness_s;
  o["pos_source"] = c.pos_source;
  o["pump_current_w_per_v"] = c.pump_current_w_per_v;
  o["pump_current_offset_v"] = c.pump_current_offset_v;
  o["pump_power_check_enabled"] = c.pump_power_check_enabled;

  o["gpio_pump_floor"] = c.gpio_pump_floor;
  o["gpio_pump_rad1"] = c.gpio_pump_rad1;
  o["gpio_pump_rad2"] = c.gpio_pump_rad2;
  o["gpio_heater"] = c.gpio_heater;
  o["gpio_mix_open"] = c.gpio_mix_open;
  o["gpio_mix_close"] = c.gpio_mix_close;
  o["mixer_wiring"] = c.mixer_wiring;
  o["gpio_onewire"] = c.gpio_onewire;
  o["gpio_led_data"] = c.gpio_led_data;
  o["num_leds"] = c.num_leds;
  // One flag per power meter: three pumps plus the mixer. A meter that is not
  // fitted is never sampled and reports 0 W.
  JsonArray pump_power = o["has_pump_power"].to<JsonArray>();
  for (uint8_t i = 0; i < 3; ++i) {
    pump_power.add(c.has_pump_power[i]);
  }
  o["has_mixer_power"] = c.has_mixer_power;
  o["i2c_sda"] = c.i2c_sda;
  o["i2c_scl"] = c.i2c_scl;
  o["output_expander"] = c.output_expander;
  o["expander_address"] = c.expander_address;
  o["expander_data_pin"] = c.expander_data_pin;
  o["expander_clock_pin"] = c.expander_clock_pin;
  o["expander_latch_pin"] = c.expander_latch_pin;
  o["expander_oe_pin"] = c.expander_oe_pin;
  o["expander_oe_active_low"] = c.expander_oe_active_low;
  o["expander_bit_heater"] = c.expander_bit_heater;
  o["expander_bit_pump_rad1"] = c.expander_bit_pump_rad1;
  o["expander_bit_pump_rad2"] = c.expander_bit_pump_rad2;
  o["expander_bit_pump_floor"] = c.expander_bit_pump_floor;
  o["expander_bit_mix_direction"] = c.expander_bit_mix_direction;
  o["expander_bit_mix_motion"] = c.expander_bit_mix_motion;
  o["expander_bit_mux_s0"] = c.expander_bit_mux_s0;
  o["expander_bit_mux_s1"] = c.expander_bit_mux_s1;
  o["expander_bit_mix_fault"] = c.expander_bit_mix_fault;
  o["mix_fault_active_low"] = c.mix_fault_active_low;
  o["output_active_low_heater"] = c.output_active_low_heater;
  o["output_active_low_pump_rad1"] = c.output_active_low_pump_rad1;
  o["output_active_low_pump_rad2"] = c.output_active_low_pump_rad2;
  o["output_active_low_pump_floor"] = c.output_active_low_pump_floor;
  o["output_active_low_mix_direction"] = c.output_active_low_mix_direction;
  o["output_active_low_mix_motion"] = c.output_active_low_mix_motion;
  o["current_sense"] = c.current_sense;
  o["ads1115_address"] = c.ads1115_address;
  o["tlv9064_present"] = c.tlv9064_present;
  o["sensor_id_boiler"] = c.sensor_id_boiler;
  o["sensor_id_ret"] = c.sensor_id_ret;
  o["sensor_id_mix"] = c.sensor_id_mix;

  // Observer-owned control-loop tuning (plan 01 Cat 3). Exposed for bench
  // tuning until the observer node owns these over MQTT.
  o["delay_s"] = c.delay_s;
  o["mix_ff_offset"] = c.mix_ff_offset;
  o["ff_boiler"] = c.ff_boiler;
  o["ff_return"] = c.ff_return;
  o["rad_coupling_ff"] = c.rad_coupling_ff;
  o["kp"] = c.kp;
  o["ki"] = c.ki;
  o["start_flush_ms"] = c.start_flush_ms;
  o["flush_max_pos"] = c.flush_max_pos;
  o["flush_delta_t_ok"] = c.flush_delta_t_ok;
  o["min_useful_delta_t"] = c.min_useful_delta_t;
  o["floor_pause_duration_s"] = c.floor_pause_duration_s;
}

void fill_system(JsonObject o, const Config& c) {
  o["summer_mode"] = c.summer_mode;
  o["hostname"] = c.hostname;
}

void fill_runtime(JsonObject o, const Config& c) {
  JsonObject ha = o["ha"].to<JsonObject>();
  ha["floor_setpoint_shadow"] = c.floor_setpoint_shadow;
  ha["summer_mode"] = c.summer_mode;
  ha["last_received_ms"] = 0;
  ha["source"] = "local";

  JsonObject observer = o["observer"].to<JsonObject>();
  observer["delay_s"] = c.delay_s;
  observer["gain"] = c.gain;
  observer["ff_boiler"] = c.ff_boiler;
  observer["ff_return"] = c.ff_return;
  observer["kp"] = c.kp;
  observer["ki"] = c.ki;
  observer["rad_coupling_ff"] = c.rad_coupling_ff;
  observer["start_flush_ms"] = c.start_flush_ms;
  observer["flush_max_pos"] = c.flush_max_pos;
  observer["flush_delta_t_ok"] = c.flush_delta_t_ok;
  observer["min_useful_delta_t"] = c.min_useful_delta_t;
  observer["floor_pause_duration_s"] = c.floor_pause_duration_s;
  observer["version"] = "unavailable";
  observer["healthy"] = false;
}

// ---------------------------------------------------------------------------
// Configuration validation helpers
// ---------------------------------------------------------------------------

bool json_float(JsonObject obj, const char* key, float& out, float min_value, float max_value,
                String& err) {
  JsonVariant v = obj[key];
  if (v.isNull()) {
    return true;
  }
  if (!v.is<float>()) {
    err = String(key) + " must be a number";
    return false;
  }
  const float value = v.as<float>();
  if (!isfinite(value) || value < min_value || value > max_value) {
    err = String(key) + " out of range";
    return false;
  }
  out = value;
  return true;
}

bool json_uint(JsonObject obj, const char* key, uint32_t& out, uint32_t min_value, uint32_t max_value,
               String& err) {
  JsonVariant v = obj[key];
  if (v.isNull()) {
    return true;
  }
  if (!v.is<uint32_t>()) {
    err = String(key) + " must be an unsigned integer";
    return false;
  }
  const uint32_t value = v.as<uint32_t>();
  if (value < min_value || value > max_value) {
    err = String(key) + " out of range";
    return false;
  }
  out = value;
  return true;
}

bool json_uint8(JsonObject obj, const char* key, uint8_t& out, uint8_t min_value, uint8_t max_value,
                String& err) {
  uint32_t value = out;
  if (!json_uint(obj, key, value, min_value, max_value, err)) {
    return false;
  }
  out = static_cast<uint8_t>(value);
  return true;
}

bool json_bool(JsonObject obj, const char* key, bool& out, String& err) {
  JsonVariant v = obj[key];
  if (v.isNull()) {
    return true;
  }
  if (!v.is<bool>()) {
    err = String(key) + " must be true or false";
    return false;
  }
  out = v.as<bool>();
  return true;
}

// Fixed-length array of booleans, used for the per-channel power-meter flags.
// An absent key leaves the stored values untouched; a present key must hold
// exactly `count` entries so a stale client cannot drop a channel.
bool json_bool_array(JsonObject obj, const char* key, bool* out, size_t count, String& err) {
  JsonVariant v = obj[key];
  if (v.isNull()) {
    return true;
  }
  if (!v.is<JsonArray>()) {
    err = String(key) + " must be an array of true/false values";
    return false;
  }
  JsonArray array = v.as<JsonArray>();
  if (array.size() != count) {
    err =
        String(key) + " must contain exactly " + String(static_cast<unsigned>(count)) + " entries";
    return false;
  }
  size_t i = 0;
  for (JsonVariant entry : array) {
    if (!entry.is<bool>()) {
      err = String(key) + " entries must be true or false";
      return false;
    }
    out[i++] = entry.as<bool>();
  }
  return true;
}

bool json_string(JsonObject obj, const char* key, char* dst, size_t dst_size, bool allow_empty,
                 String& err) {
  JsonVariant v = obj[key];
  if (v.isNull()) {
    return true;
  }
  if (!v.is<const char*>()) {
    err = String(key) + " must be a string";
    return false;
  }
  const char* value = v.as<const char*>();
  if (value == nullptr) {
    value = "";
  }
  if (!allow_empty && value[0] == '\0') {
    err = String(key) + " must not be empty";
    return false;
  }
  if (strlen(value) >= dst_size) {
    err = String(key) + " is too long";
    return false;
  }
  copy_str(dst, dst_size, value);
  return true;
}

bool json_password(JsonObject obj, const char* key, char* dst, size_t dst_size, String& err) {
  JsonVariant v = obj[key];
  if (v.isNull()) {
    return true;
  }
  if (!v.is<const char*>()) {
    err = String(key) + " must be a string";
    return false;
  }
  const char* value = v.as<const char*>();
  if (value == nullptr || value[0] == '\0') {
    return true;  // empty means "keep the stored password"
  }
  if (strlen(value) >= dst_size) {
    err = String(key) + " is too long";
    return false;
  }
  copy_str(dst, dst_size, value);
  return true;
}

bool json_pin(JsonObject obj, const char* key, uint8_t& out, String& err) {
  JsonVariant v = obj[key];
  if (v.isNull()) {
    return true;
  }
  if (!v.is<uint32_t>()) {
    err = String(key) + " must be a GPIO number or 255";
    return false;
  }
  const uint32_t value = v.as<uint32_t>();
  if (value != HPN_GPIO_UNASSIGNED && value > 48) {
    err = String(key) + " out of range";
    return false;
  }
  out = static_cast<uint8_t>(value);
  return true;
}

// Expander bit that may also be "not connected" (255).
bool json_optional_bit(JsonObject obj, const char* key, uint8_t& out, String& err) {
  JsonVariant v = obj[key];
  if (v.isNull()) {
    return true;
  }
  if (!v.is<uint32_t>()) {
    err = String(key) + " must be a bit number (0-7) or 255";
    return false;
  }
  const uint32_t value = v.as<uint32_t>();
  if (value != HPN_GPIO_UNASSIGNED && value > 7) {
    err = String(key) + " must be a bit number (0-7) or 255";
    return false;
  }
  out = static_cast<uint8_t>(value);
  return true;
}

bool apply_network(JsonObject in, Config& c, String& err) {
  if (!json_string(in, "wifi_ssid", c.wifi_ssid, sizeof(c.wifi_ssid), true, err)) return false;
  if (!json_password(in, "wifi_password", c.wifi_password, sizeof(c.wifi_password), err)) return false;
  if (!json_string(in, "ap_ssid", c.ap_ssid, sizeof(c.ap_ssid), true, err)) return false;
  if (!json_password(in, "ap_password", c.ap_password, sizeof(c.ap_password), err)) return false;
  if (!json_string(in, "mqtt_host", c.mqtt_host, sizeof(c.mqtt_host), true, err)) return false;
  uint32_t port = c.mqtt_port;
  if (!json_uint(in, "mqtt_port", port, 1, 65535, err)) return false;
  c.mqtt_port = static_cast<uint16_t>(port);
  if (!json_string(in, "mqtt_user", c.mqtt_user, sizeof(c.mqtt_user), true, err)) return false;
  if (!json_password(in, "mqtt_pass", c.mqtt_pass, sizeof(c.mqtt_pass), err)) return false;
  if (!json_string(in, "mqtt_base_topic", c.mqtt_base_topic, sizeof(c.mqtt_base_topic), true, err))
    return false;
  if (!json_bool(in, "mqtt_log_enabled", c.mqtt_log_enabled, err)) return false;
  if (!json_string(in, "mqtt_log_slow_topic", c.mqtt_log_slow_topic,
                   sizeof(c.mqtt_log_slow_topic), true, err))
    return false;
  if (!json_string(in, "mqtt_log_event_topic", c.mqtt_log_event_topic,
                   sizeof(c.mqtt_log_event_topic), true, err))
    return false;
  if (!json_bool(in, "ntp_enabled", c.ntp_enabled, err)) return false;
  if (!json_string(in, "ntp_server", c.ntp_server, sizeof(c.ntp_server), true, err)) return false;
  if (!json_string(in, "timezone", c.timezone, sizeof(c.timezone), true, err)) return false;
  if (!json_string(in, "mixer_meter_device", c.mixer_meter_device,
                   sizeof(c.mixer_meter_device), true, err))
    return false;
  if (!json_string(in, "mixer_meter_field", c.mixer_meter_field, sizeof(c.mixer_meter_field), true,
                   err))
    return false;
  if (!json_uint8(in, "mixer_meter_scale", c.mixer_meter_scale, 0, 1, err)) return false;
  if (!json_string(in, "mixer_meter_topic", c.mixer_meter_topic, sizeof(c.mixer_meter_topic), true,
                   err))
    return false;
  if (!json_string(in, "dhw_meter_device", c.dhw_meter_device, sizeof(c.dhw_meter_device), true,
                   err))
    return false;
  if (!json_string(in, "dhw_meter_entity", c.dhw_meter_entity, sizeof(c.dhw_meter_entity), true,
                   err))
    return false;
  if (!json_string(in, "dhw_meter_field", c.dhw_meter_field, sizeof(c.dhw_meter_field), true, err))
    return false;
  if (!json_string(in, "dhw_meter_topic", c.dhw_meter_topic, sizeof(c.dhw_meter_topic), true, err))
    return false;
  if (c.ntp_enabled && c.ntp_server[0] == '\0') {
    err = "ntp_server must not be empty when NTP is enabled";
    return false;
  }
  return true;
}

bool apply_safety(JsonObject in, Config& c, String& err) {
  if (!json_float(in, "t_max_hard", c.t_max_hard, 20.0f, 90.0f, err)) return false;
  if (!json_float(in, "t_max_warn", c.t_max_warn, 20.0f, 90.0f, err)) return false;
  if (!json_float(in, "t_overtemp_hysteresis", c.t_overtemp_hysteresis, 0.5f, 10.0f, err))
    return false;
  if (!json_float(in, "grad_limit", c.grad_limit, 0.05f, 5.0f, err)) return false;
  if (!json_float(in, "mix_safe_pos", c.mix_safe_pos, 0.0f, 1.0f, err)) return false;
  if (!json_uint8(in, "mix_max_open_pct", c.mix_max_open_pct, 10, 100, err)) return false;
  if (!json_uint8(in, "mix_min_open_pct", c.mix_min_open_pct, 0, 100, err)) return false;
  if (!json_bool(in, "mix_max_dynamic", c.mix_max_dynamic, err)) return false;
  if (!json_uint8(in, "mix_max_below_pct", c.mix_max_below_pct, 10, 100, err)) return false;
  if (!json_uint8(in, "mix_max_above_pct", c.mix_max_above_pct, 10, 100, err)) return false;
  if (!json_float(in, "mix_max_switch_c", c.mix_max_switch_c, 0.0f, 90.0f, err)) return false;
  if (!json_float(in, "mix_max_hysteresis_c", c.mix_max_hysteresis_c, 0.5f, 10.0f, err))
    return false;
  if (!json_float(in, "mix_overshoot_delta_c", c.mix_overshoot_delta_c, 0.5f, 10.0f, err))
    return false;
  if (!json_float(in, "mix_overshoot_recovery_c", c.mix_overshoot_recovery_c, 0.0f, 10.0f, err))
    return false;
  if (!json_uint8(in, "mix_overshoot_target_pct", c.mix_overshoot_target_pct, 0, 100, err))
    return false;
  if (!json_bool(in, "sensor_role_check_enabled", c.sensor_role_check_enabled, err)) return false;
  if (!json_float(in, "sensor_role_tolerance_c", c.sensor_role_tolerance_c, 0.5f, 20.0f, err))
    return false;
  if (!json_bool(in, "dhw_infer_enabled", c.dhw_infer_enabled, err)) return false;
  if (!json_float(in, "dhw_infer_drop_c", c.dhw_infer_drop_c, 1.0f, 20.0f, err)) return false;
  if (!json_uint(in, "dhw_infer_window_s", c.dhw_infer_window_s, 10, 3600, err)) return false;
  if (!json_float(in, "dhw_infer_recovery_c", c.dhw_infer_recovery_c, 0.0f, 20.0f, err))
    return false;
  if (!json_uint(in, "dhw_infer_max_s", c.dhw_infer_max_s, 60, 86400, err)) return false;
  if (!json_uint(in, "boiler_min_off_ms", c.boiler_min_off_ms, 0, 3600000, err)) return false;
  if (!json_uint(in, "demand_timeout_s", c.demand_timeout_s, 10, 3600, err)) return false;
  if (!json_uint(in, "pump_stop_after_heating_s", c.pump_stop_after_heating_s, 0, 3600, err))
    return false;
  if (!json_uint(in, "long_pause_s", c.long_pause_s, 0, 86400, err)) return false;
  if (!json_uint(in, "anti_seize_interval_s", c.anti_seize_interval_s, 3600, 7776000, err))
    return false;
  if (!json_uint(in, "test_mode_max_s", c.test_mode_max_s, 60, 7200, err)) return false;
  if (!json_uint8(in, "sensor_error_threshold", c.sensor_error_threshold, 1, 10, err)) return false;

  if (c.t_max_warn >= c.t_max_hard) {
    err = "t_max_warn must be below t_max_hard";
    return false;
  }
  if (c.mix_min_open_pct > c.mix_max_open_pct) {
    err = "mix_min_open_pct must not exceed mix_max_open_pct";
    return false;
  }
  if (c.mix_max_dynamic) {
    if (c.mix_min_open_pct > c.mix_max_below_pct || c.mix_min_open_pct > c.mix_max_above_pct) {
      err = "mix_min_open_pct must not exceed either dynamic maximum";
      return false;
    }
  }
  if (c.dhw_infer_max_s < c.dhw_infer_window_s) {
    err = "dhw_infer_max_s must be at least dhw_infer_window_s";
    return false;
  }
  return true;
}

bool apply_hardware(JsonObject in, Config& c, String& err) {
  if (!json_float(in, "setpoint_default", c.setpoint_default, 5.0f, 80.0f, err)) return false;
  if (!json_float(in, "deadband_pos", c.deadband_pos, 0.0f, 0.2f, err)) return false;
  if (!json_float(in, "deadband_temp", c.deadband_temp, 0.1f, 5.0f, err)) return false;
  if (!json_uint(in, "travel_time_ms", c.travel_time_ms, 1000, 600000, err)) return false;
  if (!json_uint(in, "tick_ms", c.tick_ms, 100, 60000, err)) return false;
  if (!json_uint(in, "temp_poll_interval_ms", c.temp_poll_interval_ms, 100, 60000, err))
    return false;
  if (!json_float(in, "recalc_threshold", c.recalc_threshold, 0.01f, 1.0f, err)) return false;
  if (!json_uint(in, "encoder_check_interval_s", c.encoder_check_interval_s, 60, 86400, err))
    return false;
  if (!json_uint(in, "encoder_check_pulses", c.encoder_check_pulses, 1, 10000, err)) return false;
  if (!json_uint(in, "encoder_staleness_s", c.encoder_staleness_s, 10, 86400, err)) return false;
  if (!json_uint8(in, "pos_source", c.pos_source, 0, 1, err)) return false;
  if (!json_float(in, "pump_current_w_per_v", c.pump_current_w_per_v, 1.0f, 5000.0f, err))
    return false;
  if (!json_float(in, "pump_current_offset_v", c.pump_current_offset_v, 0.0f, 1.0f, err))
    return false;
  if (!json_bool(in, "pump_power_check_enabled", c.pump_power_check_enabled, err)) return false;

  if (!json_pin(in, "gpio_pump_floor", c.gpio_pump_floor, err)) return false;
  if (!json_pin(in, "gpio_pump_rad1", c.gpio_pump_rad1, err)) return false;
  if (!json_pin(in, "gpio_pump_rad2", c.gpio_pump_rad2, err)) return false;
  if (!json_pin(in, "gpio_heater", c.gpio_heater, err)) return false;
  if (!json_pin(in, "gpio_mix_open", c.gpio_mix_open, err)) return false;
  if (!json_pin(in, "gpio_mix_close", c.gpio_mix_close, err)) return false;
  if (!json_uint8(in, "mixer_wiring", c.mixer_wiring, 0, 1, err)) return false;
  if (!json_pin(in, "gpio_onewire", c.gpio_onewire, err)) return false;
  if (!json_pin(in, "gpio_led_data", c.gpio_led_data, err)) return false;
  if (!json_pin(in, "i2c_sda", c.i2c_sda, err)) return false;
  if (!json_pin(in, "i2c_scl", c.i2c_scl, err)) return false;

  if (!json_uint8(in, "output_expander", c.output_expander, OUTPUT_EXPANDER_NONE,
                  OUTPUT_EXPANDER_PCF8574, err))
    return false;
  if (!json_uint8(in, "expander_address", c.expander_address, 0x08, 0x77, err)) return false;
  if (!json_pin(in, "expander_data_pin", c.expander_data_pin, err)) return false;
  if (!json_pin(in, "expander_clock_pin", c.expander_clock_pin, err)) return false;
  if (!json_pin(in, "expander_latch_pin", c.expander_latch_pin, err)) return false;
  if (!json_pin(in, "expander_oe_pin", c.expander_oe_pin, err)) return false;
  if (!json_bool(in, "expander_oe_active_low", c.expander_oe_active_low, err)) return false;
  if (!json_bool(in, "output_active_low_heater", c.output_active_low_heater, err)) return false;
  if (!json_bool(in, "output_active_low_pump_rad1", c.output_active_low_pump_rad1, err))
    return false;
  if (!json_bool(in, "output_active_low_pump_rad2", c.output_active_low_pump_rad2, err))
    return false;
  if (!json_bool(in, "output_active_low_pump_floor", c.output_active_low_pump_floor, err))
    return false;
  if (!json_bool(in, "output_active_low_mix_direction", c.output_active_low_mix_direction, err))
    return false;
  if (!json_bool(in, "output_active_low_mix_motion", c.output_active_low_mix_motion, err))
    return false;
  if (!json_uint8(in, "expander_bit_heater", c.expander_bit_heater, 0, 7, err)) return false;
  if (!json_uint8(in, "expander_bit_pump_rad1", c.expander_bit_pump_rad1, 0, 7, err))
    return false;
  if (!json_uint8(in, "expander_bit_pump_rad2", c.expander_bit_pump_rad2, 0, 7, err))
    return false;
  if (!json_uint8(in, "expander_bit_pump_floor", c.expander_bit_pump_floor, 0, 7, err))
    return false;
  if (!json_uint8(in, "expander_bit_mix_direction", c.expander_bit_mix_direction, 0, 7, err))
    return false;
  if (!json_uint8(in, "expander_bit_mix_motion", c.expander_bit_mix_motion, 0, 7, err))
    return false;
  if (!json_optional_bit(in, "expander_bit_mux_s0", c.expander_bit_mux_s0, err)) return false;
  if (!json_optional_bit(in, "expander_bit_mux_s1", c.expander_bit_mux_s1, err)) return false;
  if (!json_optional_bit(in, "expander_bit_mix_fault", c.expander_bit_mix_fault, err))
    return false;
  if (!json_bool(in, "mix_fault_active_low", c.mix_fault_active_low, err)) return false;

  if (!json_uint8(in, "current_sense", c.current_sense, CURRENT_SENSE_NONE,
                  CURRENT_SENSE_ADS1115, err))
    return false;
  if (!json_uint8(in, "ads1115_address", c.ads1115_address, 0x08, 0x77, err)) return false;
  if (!json_bool(in, "tlv9064_present", c.tlv9064_present, err)) return false;
  // Power meters are optional per channel: three pumps plus the mixer. An
  // absent entry keeps the stored flags, so older clients still save.
  if (!json_bool_array(in, "has_pump_power", c.has_pump_power, 3, err)) return false;
  if (!json_bool(in, "has_mixer_power", c.has_mixer_power, err)) return false;

  // Only assigned bits participate in the uniqueness check. 255 means "not
  // connected", which the v1.x board uses for the absent MUX select lines.
  const uint8_t expander_bits[] = {
      c.expander_bit_heater,      c.expander_bit_pump_rad1,
      c.expander_bit_pump_rad2,   c.expander_bit_pump_floor,
      c.expander_bit_mix_direction, c.expander_bit_mix_motion,
      c.expander_bit_mux_s0,      c.expander_bit_mux_s1,
      c.expander_bit_mix_fault,
  };
  for (size_t i = 0; i < sizeof(expander_bits); ++i) {
    if (expander_bits[i] > 7) {
      continue;
    }
    for (size_t j = i + 1; j < sizeof(expander_bits); ++j) {
      if (expander_bits[i] == expander_bits[j]) {
        err = "expander bit assignments must be unique";
        return false;
      }
    }
  }

  if (c.output_expander == OUTPUT_EXPANDER_74HC595) {
    if (c.expander_data_pin == HPN_GPIO_UNASSIGNED ||
        c.expander_clock_pin == HPN_GPIO_UNASSIGNED ||
        c.expander_latch_pin == HPN_GPIO_UNASSIGNED) {
      err = "74HC595 needs data, clock and latch pins";
      return false;
    }
  } else if (c.output_expander == OUTPUT_EXPANDER_PCF8574) {
    if (c.i2c_sda == HPN_GPIO_UNASSIGNED || c.i2c_scl == HPN_GPIO_UNASSIGNED) {
      err = "PCF8574 needs I2C SDA and SCL pins";
      return false;
    }
  }

  // Current measurement is independent of the output driver, but each front
  // end needs its own wiring.
  if (c.current_sense == CURRENT_SENSE_MUX_ADC) {
    if (c.output_expander == OUTPUT_EXPANDER_NONE) {
      err = "the analogue current MUX needs an output expander for its select lines";
      return false;
    }
    if (c.expander_bit_mux_s0 > 7 || c.expander_bit_mux_s1 > 7) {
      err = "the analogue current MUX needs two expander bits";
      return false;
    }
  } else if (c.current_sense == CURRENT_SENSE_ADS1115) {
    if (c.i2c_sda == HPN_GPIO_UNASSIGNED || c.i2c_scl == HPN_GPIO_UNASSIGNED) {
      err = "ADS1115 needs I2C SDA and SCL pins";
      return false;
    }
  }

  // The LED count stays fixed by the carrier profile and is not parsed here.
  // The four power-meter flags (has_pump_power[], has_mixer_power) are parsed
  // above because they are per-installation, not per-carrier.

  if (!json_string(in, "sensor_id_boiler", c.sensor_id_boiler, sizeof(c.sensor_id_boiler), true, err))
    return false;
  if (!json_string(in, "sensor_id_ret", c.sensor_id_ret, sizeof(c.sensor_id_ret), true, err))
    return false;
  if (!json_string(in, "sensor_id_mix", c.sensor_id_mix, sizeof(c.sensor_id_mix), true, err))
    return false;

  if (!json_float(in, "delay_s", c.delay_s, 0.0f, 600.0f, err)) return false;
  if (!json_float(in, "mix_ff_offset", c.mix_ff_offset, -20.0f, 20.0f, err)) return false;
  if (!json_float(in, "ff_boiler", c.ff_boiler, -2.0f, 2.0f, err)) return false;
  if (!json_float(in, "ff_return", c.ff_return, -2.0f, 2.0f, err)) return false;
  if (!json_float(in, "rad_coupling_ff", c.rad_coupling_ff, -10.0f, 10.0f, err)) return false;
  if (!json_float(in, "kp", c.kp, 0.0f, 5.0f, err)) return false;
  if (!json_float(in, "ki", c.ki, 0.0f, 1.0f, err)) return false;
  if (!json_uint(in, "start_flush_ms", c.start_flush_ms, 0, 600000, err)) return false;
  if (!json_float(in, "flush_max_pos", c.flush_max_pos, 0.0f, 1.0f, err)) return false;
  if (!json_float(in, "flush_delta_t_ok", c.flush_delta_t_ok, 0.0f, 20.0f, err)) return false;
  if (!json_float(in, "min_useful_delta_t", c.min_useful_delta_t, 0.0f, 20.0f, err))
    return false;
  if (!json_uint(in, "floor_pause_duration_s", c.floor_pause_duration_s, 0, 86400, err))
    return false;

  return true;
}

bool network_requires_reboot(const Config& a, const Config& b) {
  // Wi-Fi, fallback AP and SNTP settings are latched when the radio and time
  // services start. MQTT is refreshed by the MQTT task on the next tick.
  return strcmp(a.wifi_ssid, b.wifi_ssid) != 0 ||
         strcmp(a.wifi_password, b.wifi_password) != 0 ||
         strcmp(a.ap_ssid, b.ap_ssid) != 0 ||
         strcmp(a.ap_password, b.ap_password) != 0 ||
         a.ntp_enabled != b.ntp_enabled ||
         strcmp(a.ntp_server, b.ntp_server) != 0 ||
         strcmp(a.timezone, b.timezone) != 0;
}

bool hardware_requires_reboot(const Config& a, const Config& b) {
  // GPIO assignment, carrier wiring and task cadence are latched at startup.
  // Mixer constants, deadbands and sensor IDs are consumed by the next poll or
  // control cycle and therefore do not need a reboot.
  return a.gpio_pump_floor != b.gpio_pump_floor ||
         a.gpio_pump_rad1 != b.gpio_pump_rad1 ||
         a.gpio_pump_rad2 != b.gpio_pump_rad2 ||
         a.gpio_heater != b.gpio_heater ||
         a.gpio_mix_open != b.gpio_mix_open ||
         a.gpio_mix_close != b.gpio_mix_close ||
         a.gpio_onewire != b.gpio_onewire ||
         a.gpio_led_data != b.gpio_led_data ||
         a.i2c_sda != b.i2c_sda || a.i2c_scl != b.i2c_scl ||
         a.mixer_wiring != b.mixer_wiring || a.tick_ms != b.tick_ms ||
         a.temp_poll_interval_ms != b.temp_poll_interval_ms ||
         a.output_expander != b.output_expander ||
         a.expander_address != b.expander_address ||
         a.expander_data_pin != b.expander_data_pin ||
         a.expander_clock_pin != b.expander_clock_pin ||
         a.expander_latch_pin != b.expander_latch_pin ||
         a.expander_oe_pin != b.expander_oe_pin ||
         a.expander_oe_active_low != b.expander_oe_active_low ||
         a.output_active_low_heater != b.output_active_low_heater ||
         a.output_active_low_pump_rad1 != b.output_active_low_pump_rad1 ||
         a.output_active_low_pump_rad2 != b.output_active_low_pump_rad2 ||
         a.output_active_low_pump_floor != b.output_active_low_pump_floor ||
         a.output_active_low_mix_direction != b.output_active_low_mix_direction ||
         a.output_active_low_mix_motion != b.output_active_low_mix_motion ||
         a.expander_bit_heater != b.expander_bit_heater ||
         a.expander_bit_pump_rad1 != b.expander_bit_pump_rad1 ||
         a.expander_bit_pump_rad2 != b.expander_bit_pump_rad2 ||
         a.expander_bit_pump_floor != b.expander_bit_pump_floor ||
         a.expander_bit_mix_direction != b.expander_bit_mix_direction ||
         a.expander_bit_mix_motion != b.expander_bit_mix_motion ||
         a.expander_bit_mux_s0 != b.expander_bit_mux_s0 ||
         a.expander_bit_mux_s1 != b.expander_bit_mux_s1 ||
         a.expander_bit_mix_fault != b.expander_bit_mix_fault ||
         a.mix_fault_active_low != b.mix_fault_active_low ||
         a.current_sense != b.current_sense ||
         a.ads1115_address != b.ads1115_address ||
         a.tlv9064_present != b.tlv9064_present;
}

bool hostname_valid(const char* hostname) {
  const size_t len = strlen(hostname);
  if (len == 0 || len > 32) {
    return false;
  }
  if (hostname[0] == '-' || hostname[len - 1] == '-') {
    return false;
  }
  for (size_t i = 0; i < len; ++i) {
    const char c = hostname[i];
    const bool ok = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
                    (c >= '0' && c <= '9') || c == '-' || c == '.';
    if (!ok) {
      return false;
    }
  }
  return true;
}

bool apply_system(JsonObject in, Config& c, String& err) {
  if (!json_bool(in, "summer_mode", c.summer_mode, err)) return false;
  if (!json_string(in, "hostname", c.hostname, sizeof(c.hostname), false, err)) return false;
  if (!hostname_valid(c.hostname)) {
    err = "hostname must be 1-32 characters (letters, digits, hyphen or dot)";
    return false;
  }
  return true;
}

// ---------------------------------------------------------------------------
// HTTP response helpers
// ---------------------------------------------------------------------------

void send_json(AsyncWebServerRequest* request, int code, const JsonDocument& doc) {
  // Size the response buffer up front. AsyncResponseStream grows by one byte at
  // a time past its initial 1460-byte ring buffer (each growth reallocates and
  // copies the whole payload), so serialising the 10 KB event/slow-log
  // responses took thousands of allocations inside the network task. That
  // throttled the HTTP server and could run the heap out mid-payload, leaving a
  // truncated body on the wire. measureJson() is a cheap, allocation-free pass
  // and lets the stream allocate exactly once.
  size_t size = measureJson(doc);
  if (size == 0) {
    size = 1;
  }
  AsyncResponseStream* response = request->beginResponseStream("application/json", size);
  response->setCode(code);
  response->addHeader("Cache-Control", "no-store");
  serializeJson(doc, *response);
  log_access_request(request, code);
  request->send(response);
}

void send_error(AsyncWebServerRequest* request, int code, const char* message) {
  JsonDocument doc;
  doc["ok"] = false;
  doc["error"] = message;
  send_json(request, code, doc);
}

// ---------------------------------------------------------------------------
// Relay counters, slow log and Test Mode state
// ---------------------------------------------------------------------------

void flag_nvs_corruption() {
  if (g_state == nullptr) {
    return;
  }
  g_state->active_alarms |= WARN_NVS_CORRUPTED;
  if (g_state->system_error_code == ERR_OK) {
    g_state->system_error_code = ERR_NVS_DATA_CORRUPTED;
  }
}

#if !defined(HPN_IO_EMULATION)
uint32_t counter_blob_crc(CounterBlob& blob) {
  const uint32_t saved = blob.crc;
  blob.crc = 0;
  const uint32_t crc = crc32_ieee(&blob, sizeof(blob));
  blob.crc = saved;
  return crc;
}
#endif

int counter_index(const char* name) {
  if (strcmp(name, "heater") == 0) return CTR_HEATER;
  if (strcmp(name, "rad1") == 0) return CTR_RAD1;
  if (strcmp(name, "rad2") == 0) return CTR_RAD2;
  if (strcmp(name, "floor") == 0) return CTR_FLOOR;
  if (strcmp(name, "mix_open") == 0) return CTR_MIX_OPEN;
  if (strcmp(name, "mix_close") == 0) return CTR_MIX_CLOSE;
  return -1;
}

bool counter_persistence_enabled() {
#if defined(HPN_IO_EMULATION)
  return false;
#else
  return true;
#endif
}

void counters_load() {
#if !defined(HPN_IO_EMULATION)
  Preferences prefs;
  if (!prefs.begin(kCounterNamespace, false)) {
    Serial.println("[counters] NVS open failed; starting from zero");
    return;
  }

  const size_t stored = prefs.getBytesLength(kCounterKey);
  CounterBlob blob{};
  bool ok = false;
  bool stale = false;
  if (stored != 0) {
    // Preferences has no partial read; read the whole blob. The current format
    // is larger than the legacy one, so a larger blob cannot be current.
    if (stored < sizeof(CounterBlobPrefix)) {
      // Too short to be any known format.
    } else if (stored > sizeof(blob)) {
      // Larger than any recorded format: an old/foreign blob.
      stale = true;
    } else if (prefs.getBytes(kCounterKey, &blob, stored) != stored) {
      // Read failed despite the length check: treat as damage.
    } else if (blob.magic != kCounterMagic || blob.version != kCounterVersion) {
      // Old-format counters; discard without migration and without an alarm.
      stale = true;
    } else if (stored == sizeof(blob) && blob.size == sizeof(blob) &&
               blob.crc == counter_blob_crc(blob)) {
      ok = true;
    }
  }
  prefs.end();

  if (!ok) {
    if (stale) {
      Serial.printf("[counters] legacy NVS format (%u bytes); counters reset\n",
                    static_cast<unsigned>(stored));
    } else if (stored != 0) {
      Serial.printf("[counters] NVS CRC/format check failed (%u bytes); counters reset\n",
                    static_cast<unsigned>(stored));
      flag_nvs_corruption();
    }
    return;
  }

  taskENTER_CRITICAL(&g_counter_mux);
  for (uint8_t i = 0; i < CTR_COUNT; ++i) {
    g_counters[i] = CounterEntry{};
    g_counters[i].cycles = blob.cycles[i];
    g_counters[i].runtime_s = blob.runtime_s[i];
  }
  g_counter_runtime_ms = millis();
  g_counters_dirty = false;
  g_counters_persisted = true;
  taskEXIT_CRITICAL(&g_counter_mux);

  Serial.println("[counters] loaded relay counters from NVS");
#endif
}

void counters_save() {
#if !defined(HPN_IO_EMULATION)
  CounterBlob blob{};
  blob.magic = kCounterMagic;
  blob.version = kCounterVersion;
  blob.size = sizeof(blob);

  taskENTER_CRITICAL(&g_counter_mux);
  for (uint8_t i = 0; i < CTR_COUNT; ++i) {
    blob.cycles[i] = g_counters[i].cycles;
    blob.runtime_s[i] = g_counters[i].runtime_s;
  }
  g_counters_dirty = false;
  taskEXIT_CRITICAL(&g_counter_mux);

  blob.crc = 0;
  blob.crc = counter_blob_crc(blob);

  Preferences prefs;
  if (!prefs.begin(kCounterNamespace, false)) {
    Serial.println("[counters] NVS open failed; counters not saved");
    taskENTER_CRITICAL(&g_counter_mux);
    g_counters_dirty = true;
    taskEXIT_CRITICAL(&g_counter_mux);
    return;
  }
  const size_t written = prefs.putBytes(kCounterKey, &blob, sizeof(blob));
  prefs.end();

  if (written == sizeof(blob)) {
    g_counters_persisted = true;
    g_last_counter_save_ms = millis();
    Serial.println("[counters] saved relay counters to NVS");
  } else {
    taskENTER_CRITICAL(&g_counter_mux);
    g_counters_dirty = true;
    taskEXIT_CRITICAL(&g_counter_mux);
    Serial.println("[counters] NVS write failed");
  }
#else
  g_counters_dirty = false;
  g_counters_persisted = false;
#endif
}

void reset_counter_locked(CounterId id) {
  g_counters[id] = CounterEntry{};
  g_counters_dirty = true;
  g_counters_persisted = false;
  g_counter_save_requested = true;
}

void reset_all_counters() {
  taskENTER_CRITICAL(&g_counter_mux);
  for (uint8_t i = 0; i < CTR_COUNT; ++i) {
    reset_counter_locked(static_cast<CounterId>(i));
  }
  g_counter_runtime_ms = millis();
  g_counters_dirty = true;
  g_counter_save_requested = true;
  taskEXIT_CRITICAL(&g_counter_mux);
}

void update_counter_locked(CounterId id, bool on, uint32_t now_ms) {
  CounterEntry& counter = g_counters[id];
  if (!counter.initialized) {
    counter.initialized = true;
    counter.last_state = on;
    counter.last_change_ms = now_ms;
    return;
  }
  if (counter.last_state != on) {
    counter.last_state = on;
    counter.cycles++;
    counter.last_change_ms = now_ms;
    g_counters_dirty = true;
  }
}

void update_counters(const SystemState& st, uint32_t now_ms) {
  taskENTER_CRITICAL(&g_counter_mux);
  update_counter_locked(CTR_HEATER, st.heater_phys, now_ms);
  update_counter_locked(CTR_RAD1, st.pump_rad1_phys, now_ms);
  update_counter_locked(CTR_RAD2, st.pump_rad2_phys, now_ms);
  update_counter_locked(CTR_FLOOR, st.pump_floor_phys, now_ms);
  update_counter_locked(CTR_MIX_OPEN, st.mix_motion && st.mix_dir > 0, now_ms);
  update_counter_locked(CTR_MIX_CLOSE, st.mix_motion && st.mix_dir < 0, now_ms);

  if (g_counter_runtime_ms == 0) {
    g_counter_runtime_ms = now_ms;
  }
  if (now_ms - g_counter_runtime_ms >= 1000) {
    const uint32_t seconds = (now_ms - g_counter_runtime_ms) / 1000;
    g_counter_runtime_ms += seconds * 1000;
    if (st.heater_phys) {
      g_counters[CTR_HEATER].runtime_s += seconds;
      g_counters_dirty = true;
    }
    if (st.pump_rad1_phys) {
      g_counters[CTR_RAD1].runtime_s += seconds;
      g_counters_dirty = true;
    }
    if (st.pump_rad2_phys) {
      g_counters[CTR_RAD2].runtime_s += seconds;
      g_counters_dirty = true;
    }
    if (st.pump_floor_phys) {
      g_counters[CTR_FLOOR].runtime_s += seconds;
      g_counters_dirty = true;
    }
  }
  taskEXIT_CRITICAL(&g_counter_mux);
}

void activate_test() {
  if (g_config == nullptr || g_state == nullptr) {
    return;
  }
  const uint32_t max_s = g_config->test_mode_max_s > 0 ? g_config->test_mode_max_s : 7200;
  g_test_active = true;
  g_test_end_ms = millis() + max_s * 1000u;
  g_state->mode = MODE_TEST_OVERRIDE;
  g_state->test_mode_rem_s = max_s;
  log_event(1, 1001, "Test Mode activated");
}

void deactivate_test(const char* reason) {
  if (!g_test_active) {
    return;
  }
  g_test_active = false;
  g_test_end_ms = 0;
  g_sequence = Sequence::None;
  g_sequence_step = 0;
  g_sequence_next_ms = 0;

  if (g_state != nullptr) {
    g_state->test_mode_rem_s = 0;
    g_state->mode = (g_config != nullptr && g_config->summer_mode) ? MODE_SUMMER : MODE_HEAT;
    g_state->heater_intended = false;
    g_state->pump_floor_intended = false;
    g_state->pump_rad1_intended = false;
    g_state->pump_rad2_intended = false;
    g_state->mix_motion = false;
    g_state->mix_dir = 0;
    g_state->mix_free_run_dir = 0;
    g_state->mix_free_run_until_ms = 0;
    float pos = g_state->pos_est_dr;
    if (pos < 0.0f) pos = 0.0f;
    if (pos > 1.0f) pos = 1.0f;
    g_state->mix_target_pos = pos;
  }

  log_event(0, 1002, reason);
}

void tick_sequence(uint32_t now_ms) {
  if (g_sequence == Sequence::None || g_state == nullptr) {
    return;
  }
  if (static_cast<int32_t>(now_ms - g_sequence_next_ms) < 0) {
    return;
  }

  if (g_sequence == Sequence::AntiSeize) {
    switch (g_sequence_step) {
      case 0:
        g_state->pump_floor_intended = true;
        log_event(0, 1400, "Anti-seize sequence: floor pump on");
        g_sequence_step = 1;
        g_sequence_next_ms = now_ms + 30000u;
        break;
      case 1:
        g_state->pump_floor_intended = false;
        g_state->pump_rad1_intended = true;
        log_event(0, 1401, "Anti-seize sequence: rad1 pump on");
        g_sequence_step = 2;
        g_sequence_next_ms = now_ms + 30000u;
        break;
      case 2:
        g_state->pump_rad1_intended = false;
        g_state->pump_rad2_intended = true;
        log_event(0, 1402, "Anti-seize sequence: rad2 pump on");
        g_sequence_step = 3;
        g_sequence_next_ms = now_ms + 30000u;
        break;
      default:
        g_state->pump_rad2_intended = false;
        g_sequence = Sequence::None;
        g_sequence_step = 0;
        log_event(0, 1403, "Anti-seize sequence complete");
        break;
    }
    return;
  }

  const uint32_t travel_ms =
      g_config != nullptr && g_config->travel_time_ms > 0 ? g_config->travel_time_ms : 120000;
  const uint32_t stroke_ms = travel_ms + 10000u;

  if (g_sequence == Sequence::EndstopHome) {
    switch (g_sequence_step) {
      case 0:
        // Deadline first: L1 reads these fields from the real-time core, so a
        // direction with a stale deadline would cancel the run immediately.
        g_state->mix_free_run_until_ms = now_ms + stroke_ms;
        g_state->mix_free_run_dir = -1;
        g_state->mix_target_pos = 0.0f;
        log_event(0, 1420, "Endstop home: driving mixer fully closed");
        g_sequence_step = 1;
        g_sequence_next_ms = now_ms + stroke_ms + 1000u;
        break;
      default:
        g_state->mix_free_run_dir = 0;
        g_state->mix_free_run_until_ms = 0;
        g_state->pos_est_dr = 0.0f;
        g_state->mix_target_pos = 0.0f;
        g_sequence = Sequence::None;
        g_sequence_step = 0;
        log_event(0, 1421, "Endstop home complete; dead reckoning zeroed");
        break;
    }
    return;
  }

  if (g_sequence == Sequence::MixerSweep) {
    switch (g_sequence_step) {
      case 0:
        g_state->mix_free_run_until_ms = now_ms + stroke_ms;
        g_state->mix_free_run_dir = 1;
        g_state->mix_target_pos =
            g_config != nullptr ? mixer_ceiling(*g_config) : 1.0f;
        log_event(0, 1430, "Mixer sweep: opening to the configured limit");
        g_sequence_step = 1;
        g_sequence_next_ms = now_ms + stroke_ms + 1000u;
        break;
      case 1:
        g_state->mix_free_run_until_ms = now_ms + stroke_ms;
        g_state->mix_free_run_dir = -1;
        g_state->mix_target_pos = 0.0f;
        log_event(0, 1431, "Mixer sweep: driving fully closed");
        g_sequence_step = 2;
        g_sequence_next_ms = now_ms + stroke_ms + 1000u;
        break;
      default:
        g_state->mix_free_run_dir = 0;
        g_state->mix_free_run_until_ms = 0;
        g_state->pos_est_dr = 0.0f;
        g_state->mix_target_pos = 0.0f;
        g_sequence = Sequence::None;
        g_sequence_step = 0;
        log_event(0, 1432, "Mixer sweep complete");
        break;
    }
    return;
  }
}

// ---------------------------------------------------------------------------
// Anti-seize schedule (NVS, deferred persistence)
// ---------------------------------------------------------------------------

constexpr const char* kAntiSeizeNamespace = "hpnanti";
constexpr const char* kAntiSeizeKey = "next_day";
constexpr uint32_t kAntiSeizeMagic = 0x48504E41u;  // "HPNA"
constexpr uint32_t kAntiSeizeWriteIntervalMs = 300000;  // 5 min

struct AntiSeizeBlob {
  uint32_t magic;
  uint32_t next_day;
  uint32_t crc;
};

uint32_t g_anti_seize_next_day = 0;
uint32_t g_anti_seize_last_write_ms = 0;
bool g_anti_seize_dirty = false;
bool g_anti_seize_loaded = false;

uint32_t anti_seize_blob_crc(AntiSeizeBlob& blob) {
  const uint32_t saved = blob.crc;
  blob.crc = 0;
  const uint32_t crc = crc32_ieee(&blob, sizeof(blob));
  blob.crc = saved;
  return crc;
}

void anti_seize_load() {
  Preferences prefs;
  g_anti_seize_next_day = 0;
  if (prefs.begin(kAntiSeizeNamespace, false)) {
    // The previous firmware stored this key as a UInt, so check the type rather
    // than just the key's presence. A scalar is a legacy value and is dropped
    // without migration; no alarm is raised because the data is not damaged.
    const PreferenceType type = prefs.getType(kAntiSeizeKey);
    if (type == PT_INVALID) {
      prefs.end();
      g_anti_seize_loaded = true;
      return;
    }
    if (type != PT_BLOB) {
      // Drop the legacy scalar now so the next boot is clean even if summer
      // mode never schedules a new value.
      prefs.remove(kAntiSeizeKey);
      prefs.end();
      Serial.println("[anti-seize] legacy NVS scalar discarded; schedule reset");
      g_anti_seize_loaded = true;
      return;
    }
    const size_t stored = prefs.getBytesLength(kAntiSeizeKey);
    AntiSeizeBlob blob{};
    const bool ok = stored == sizeof(blob) &&
                    prefs.getBytes(kAntiSeizeKey, &blob, sizeof(blob)) == sizeof(blob) &&
                    blob.magic == kAntiSeizeMagic && blob.crc == anti_seize_blob_crc(blob);
    prefs.end();
    if (ok) {
      g_anti_seize_next_day = blob.next_day;
    } else if (stored == sizeof(blob)) {
      Serial.printf("[anti-seize] NVS CRC/format check failed (%u bytes); schedule reset\n",
                    static_cast<unsigned>(stored));
      flag_nvs_corruption();
    } else if (stored != 0) {
      Serial.printf("[anti-seize] legacy NVS format (%u bytes); schedule reset\n",
                    static_cast<unsigned>(stored));
    }
  }
  g_anti_seize_loaded = true;
}

void anti_seize_flush(uint32_t now_ms) {
  if (!g_anti_seize_dirty) {
    return;
  }
  if (now_ms - g_anti_seize_last_write_ms < kAntiSeizeWriteIntervalMs) {
    return;
  }
  AntiSeizeBlob blob{};
  blob.magic = kAntiSeizeMagic;
  blob.next_day = g_anti_seize_next_day;
  blob.crc = 0;
  blob.crc = anti_seize_blob_crc(blob);

  Preferences prefs;
  if (prefs.begin(kAntiSeizeNamespace, false)) {
    // NVS refuses a blob write over a key stored with another type; the
    // previous firmware used a UInt. Remove that legacy scalar first.
    const PreferenceType type = prefs.getType(kAntiSeizeKey);
    if (type != PT_INVALID && type != PT_BLOB) {
      prefs.remove(kAntiSeizeKey);
    }
    const size_t written = prefs.putBytes(kAntiSeizeKey, &blob, sizeof(blob));
    prefs.end();
    if (written == sizeof(blob)) {
      g_anti_seize_dirty = false;
      g_anti_seize_last_write_ms = now_ms;
    } else {
      Serial.println("[anti-seize] NVS write failed; will retry");
    }
  }
}

void anti_seize_flush_now(uint32_t now_ms) {
  g_anti_seize_last_write_ms = 0;
  anti_seize_flush(now_ms);
}

void anti_seize_service(const Config& config, const SystemState& state, uint32_t now_ms) {
  if (!g_anti_seize_loaded) {
    anti_seize_load();
  }
  const uint64_t epoch = state.wall_time_epoch_s;
  if (epoch < 1600000000ULL) {
    return;
  }
  const uint32_t today = static_cast<uint32_t>(epoch / 86400ULL);
  uint32_t interval_days = config.anti_seize_interval_s / 86400u;
  if (interval_days == 0) {
    interval_days = 1;
  }

  if (!config.summer_mode) {
    if (g_anti_seize_next_day != 0) {
      g_anti_seize_next_day = 0;
      g_anti_seize_dirty = true;
    }
    anti_seize_flush(now_ms);
    return;
  }

  if (g_anti_seize_next_day == 0) {
    g_anti_seize_next_day = today + interval_days;
    g_anti_seize_dirty = true;
    log_event(0, 1440, "Anti-seize schedule created");
  } else if (today >= g_anti_seize_next_day) {
    if (g_sequence == Sequence::None && !g_test_active) {
      g_sequence = Sequence::AntiSeize;
      g_sequence_step = 0;
      g_sequence_next_ms = now_ms;
      log_event(0, 1441, "Scheduled anti-seize sequence started");
    }
    g_anti_seize_next_day = today + interval_days;
    g_anti_seize_dirty = true;
  }
  anti_seize_flush(now_ms);
}

// ---------------------------------------------------------------------------
// CPU load sampling (recent one-second window, not boot average)
// ---------------------------------------------------------------------------

float g_cpu_load_pct = 0.0f;
uint32_t g_cpu_last_sample_ms = 0;
uint32_t g_cpu_last_total = 0;
uint32_t g_cpu_last_idle = 0;
// Raw run-time counter deltas from the last window. Exposed in /api/status so
// the load figure can be validated against the FreeRTOS counters.
uint32_t g_cpu_total_delta = 0;
uint32_t g_cpu_idle_delta = 0;

void update_cpu_load(uint32_t now_ms) {
#if defined(configUSE_TRACE_FACILITY) && configUSE_TRACE_FACILITY
  if (now_ms - g_cpu_last_sample_ms < 1000u) {
    return;
  }
  g_cpu_last_sample_ms = now_ms;

  static TaskStatus_t task_status[24];
  uint32_t total = 0;
  UBaseType_t wanted = uxTaskGetNumberOfTasks();
  if (wanted > 24) wanted = 24;
  const UBaseType_t got = uxTaskGetSystemState(task_status, wanted, &total);
  uint32_t idle = 0;
  for (UBaseType_t i = 0; i < got; ++i) {
    if (task_status[i].pcTaskName != nullptr &&
        strncmp(task_status[i].pcTaskName, "IDLE", 4) == 0) {
      idle += task_status[i].ulRunTimeCounter;
    }
  }

  if (g_cpu_last_total != 0 && total > g_cpu_last_total) {
    const uint32_t total_delta = total - g_cpu_last_total;
    const uint32_t idle_delta = idle - g_cpu_last_idle;
    g_cpu_total_delta = total_delta;
    g_cpu_idle_delta = idle_delta;
    if (total_delta > 0) {
      float load = 100.0f * (1.0f - static_cast<float>(idle_delta) /
                                       static_cast<float>(total_delta));
      if (load < 0.0f) load = 0.0f;
      if (load > 100.0f) load = 100.0f;
      g_cpu_load_pct = load;
    }
  }
  g_cpu_last_total = total;
  g_cpu_last_idle = idle;
#else
  (void)now_ms;
#endif
}

// ---------------------------------------------------------------------------
// Configuration handlers
// ---------------------------------------------------------------------------

void handle_config_get(AsyncWebServerRequest* request, const char* section) {
  if (g_config == nullptr) {
    send_error(request, 503, "configuration not ready");
    return;
  }

  JsonDocument doc;
  JsonObject root = doc.to<JsonObject>();

  if (strcmp(section, "all") == 0) {
    JsonObject network = root["network"].to<JsonObject>();
    fill_network(network, *g_config);
    JsonObject safety = root["safety"].to<JsonObject>();
    fill_safety(safety, *g_config);
    JsonObject hardware = root["hardware"].to<JsonObject>();
    fill_hardware(hardware, *g_config);
    JsonObject system = root["system"].to<JsonObject>();
    fill_system(system, *g_config);
    JsonObject runtime = root["runtime"].to<JsonObject>();
    fill_runtime(runtime, *g_config);
  } else if (strcmp(section, "network") == 0) {
    fill_network(root, *g_config);
  } else if (strcmp(section, "safety") == 0) {
    fill_safety(root, *g_config);
  } else if (strcmp(section, "hardware") == 0) {
    fill_hardware(root, *g_config);
  } else if (strcmp(section, "system") == 0) {
    fill_system(root, *g_config);
  } else if (strcmp(section, "runtime") == 0) {
    fill_runtime(root, *g_config);
  } else {
    send_error(request, 404, "unknown configuration section");
    return;
  }

  send_json(request, 200, doc);
}

void handle_config_put(AsyncWebServerRequest* request, JsonVariant& json, const char* section) {
  if (g_config == nullptr) {
    send_error(request, 503, "configuration not ready");
    return;
  }
  if (!json.is<JsonObject>()) {
    send_error(request, 400, "request body must be a JSON object");
    return;
  }
  if (strcmp(section, "runtime") == 0) {
    send_error(request, 403, "runtime parameters are read-only");
    return;
  }

  JsonObject in = json.as<JsonObject>();
  Config& active = ::config_mutable();
  Config next = active;
  String err;
  bool ok = false;
  bool reboot_required = false;

  if (strcmp(section, "network") == 0) {
    ok = apply_network(in, next, err);
    reboot_required = ok && network_requires_reboot(active, next);
  } else if (strcmp(section, "safety") == 0) {
    ok = apply_safety(in, next, err);
  } else if (strcmp(section, "hardware") == 0) {
    ok = apply_hardware(in, next, err);
    reboot_required = ok && hardware_requires_reboot(active, next);
  } else if (strcmp(section, "system") == 0) {
    ok = apply_system(in, next, err);
    reboot_required = strcmp(active.hostname, next.hostname) != 0;
  } else {
    send_error(request, 404, "unknown configuration section");
    return;
  }

  if (!ok) {
    JsonDocument doc;
    doc["ok"] = false;
    doc["error"] = err;
    send_json(request, 400, doc);
    return;
  }

  const Config backup = active;
  active = next;
  if (!config_save(active)) {
    active = backup;
    send_error(request, 500, "NVS write failed");
    return;
  }
  anti_seize_flush_now(millis());

  if (strcmp(section, "system") == 0 && !g_test_active && g_state != nullptr) {
    g_state->mode = active.summer_mode ? MODE_SUMMER : MODE_HEAT;
  }

  char detail[96];
  snprintf(detail, sizeof(detail), "Settings saved: %s", section);
  log_event(0, 1200, detail);

  JsonDocument doc;
  doc["ok"] = true;
  doc["section"] = section;
  doc["reboot_required"] = reboot_required;
  send_json(request, 200, doc);
}

void handle_config_export(AsyncWebServerRequest* request) {
  if (g_config == nullptr) {
    send_error(request, 503, "configuration not ready");
    return;
  }

  JsonDocument doc;
  JsonObject root = doc.to<JsonObject>();
  root["format"] = "heatpumpsn.config";
  root["version"] = 1;
  JsonObject network = root["network"].to<JsonObject>();
  fill_network(network, *g_config, true);
  JsonObject safety = root["safety"].to<JsonObject>();
  fill_safety(safety, *g_config);
  JsonObject hardware = root["hardware"].to<JsonObject>();
  fill_hardware(hardware, *g_config);
  JsonObject system = root["system"].to<JsonObject>();
  fill_system(system, *g_config);

  AsyncResponseStream* response = request->beginResponseStream("application/json");
  response->setCode(200);
  response->addHeader("Cache-Control", "no-store");
  response->addHeader("Content-Disposition",
                      "attachment; filename=\"heatpumpsn-config.json\"");
  serializeJsonPretty(doc, *response);
  log_access_request(request, 200);
  request->send(response);
}

void handle_config_import(AsyncWebServerRequest* request, JsonVariant& json) {
  if (g_config == nullptr) {
    send_error(request, 503, "configuration not ready");
    return;
  }
  if (!json.is<JsonObject>()) {
    send_error(request, 400, "request body must be a JSON object");
    return;
  }

  JsonObject in = json.as<JsonObject>();
  if (!in["format"].isNull()) {
    if (!in["format"].is<const char*>() ||
        strcmp(in["format"].as<const char*>(), "heatpumpsn.config") != 0) {
      send_error(request, 400, "unsupported configuration format");
      return;
    }
  }
  if (!in["version"].isNull()) {
    if (!in["version"].is<int>() || in["version"].as<int>() > 1) {
      send_error(request, 400, "unsupported configuration version");
      return;
    }
  }

  Config& active = ::config_mutable();
  Config next = active;
  String err;
  bool any = false;

  if (in["network"].is<JsonObject>()) {
    JsonObject net = in["network"].as<JsonObject>();
    // The normal save path treats an empty password as "keep the stored one".
    // A configuration restore is explicit, so first copy any password fields
    // verbatim (including empty) and let apply_network leave them untouched.
    if (!json_string(net, "wifi_password", next.wifi_password, sizeof(next.wifi_password), true,
                     err) ||
        !json_string(net, "ap_password", next.ap_password, sizeof(next.ap_password), true, err) ||
        !json_string(net, "mqtt_pass", next.mqtt_pass, sizeof(next.mqtt_pass), true, err)) {
      send_error(request, 400, err.c_str());
      return;
    }
    if (!apply_network(net, next, err)) {
      send_error(request, 400, err.c_str());
      return;
    }
    any = true;
  }
  if (in["safety"].is<JsonObject>()) {
    if (!apply_safety(in["safety"].as<JsonObject>(), next, err)) {
      send_error(request, 400, err.c_str());
      return;
    }
    any = true;
  }
  if (in["hardware"].is<JsonObject>()) {
    if (!apply_hardware(in["hardware"].as<JsonObject>(), next, err)) {
      send_error(request, 400, err.c_str());
      return;
    }
    any = true;
  }
  if (in["system"].is<JsonObject>()) {
    if (!apply_system(in["system"].as<JsonObject>(), next, err)) {
      send_error(request, 400, err.c_str());
      return;
    }
    any = true;
  }

  if (!any) {
    send_error(request, 400, "no configuration sections found");
    return;
  }

  const Config backup = active;
  active = next;
  if (!config_save(active)) {
    active = backup;
    send_error(request, 500, "NVS write failed");
    return;
  }
  anti_seize_flush_now(millis());

  if (!g_test_active && g_state != nullptr) {
    g_state->mode = active.summer_mode ? MODE_SUMMER : MODE_HEAT;
  }

  const bool reboot_required = network_requires_reboot(backup, active) ||
                               hardware_requires_reboot(backup, active) ||
                               strcmp(backup.hostname, active.hostname) != 0;
  log_event(0, 1201, "Configuration imported");

  JsonDocument doc;
  doc["ok"] = true;
  doc["reboot_required"] = reboot_required;
  send_json(request, 200, doc);
}

// ---------------------------------------------------------------------------
// Log and counter handlers
// ---------------------------------------------------------------------------

void handle_log_access(AsyncWebServerRequest* request) {
  const uint32_t after = static_cast<uint32_t>(request->arg("after").toInt());
  std::vector<AccessEntry> entries;
  entries.reserve(kAccessLogSize);
  uint32_t seq = 0;

  taskENTER_CRITICAL(&g_access_mux);
  seq = g_access_seq;
  const uint32_t start = (g_access_head + kAccessLogSize - g_access_count) % kAccessLogSize;
  for (uint32_t i = 0; i < g_access_count; ++i) {
    const AccessEntry& entry = g_access[(start + i) % kAccessLogSize];
    if (entry.seq > after) {
      entries.push_back(entry);
    }
  }
  taskEXIT_CRITICAL(&g_access_mux);

  JsonDocument doc;
  doc["seq"] = seq;
  doc["count"] = entries.size();
  JsonArray arr = doc["entries"].to<JsonArray>();
  for (const AccessEntry& entry : entries) {
    JsonObject o = arr.add<JsonObject>();
    o["seq"] = entry.seq;
    o["ts"] = entry.ms;
    o["ip"] = entry.ip;
    o["method"] = entry.method;
    o["path"] = entry.path;
    o["status"] = entry.status;
  }
  send_json(request, 200, doc);
}

void handle_log_events(AsyncWebServerRequest* request) {
  const uint32_t after = static_cast<uint32_t>(request->arg("after").toInt());
  std::vector<logger::Event> entries;
  entries.reserve(logger::kEventCapacity);

  uint32_t cursor = after;
  logger::Event batch[8];
  for (;;) {
    const size_t copied = logger::events_after(cursor, batch, 8);
    for (size_t i = 0; i < copied; ++i) {
      entries.push_back(batch[i]);
      cursor = batch[i].seq;
    }
    if (copied < 8) {
      break;
    }
  }

  JsonDocument doc;
  doc["seq"] = logger::event_seq();
  doc["count"] = entries.size();
  JsonArray arr = doc["entries"].to<JsonArray>();
  for (const logger::Event& entry : entries) {
    JsonObject o = arr.add<JsonObject>();
    o["seq"] = entry.seq;
    o["ts"] = entry.ms;
    o["severity"] = logger::severity_name(entry.severity);
    o["code"] = entry.code;
    o["detail"] = entry.detail;
    o["latched"] = entry.latched;
  }
  send_json(request, 200, doc);
}

void handle_log_slow(AsyncWebServerRequest* request) {
  uint32_t from = static_cast<uint32_t>(request->arg("from").toInt());
  uint32_t to = static_cast<uint32_t>(request->arg("to").toInt());
  if (to == 0) {
    to = 0xFFFFFFFFu;
  }

  std::vector<logger::SlowSample> samples;
  samples.reserve(logger::kSlowCapacity);

  uint32_t cursor = 0;
  logger::SlowSample batch[16];
  for (;;) {
    const size_t copied = logger::slow_after(cursor, batch, 16);
    for (size_t i = 0; i < copied; ++i) {
      cursor = batch[i].seq;
      if (batch[i].ms >= from && batch[i].ms <= to) {
        samples.push_back(batch[i]);
      }
    }
    if (copied < 16) {
      break;
    }
  }

  JsonDocument doc;
  doc["cadence_s"] = logger::kSlowSampleIntervalMs / 1000;
  doc["count"] = samples.size();
  JsonArray arr = doc["samples"].to<JsonArray>();
  for (const logger::SlowSample& sample : samples) {
    JsonObject o = arr.add<JsonObject>();
    o["seq"] = sample.seq;
    o["ts"] = sample.ms;
    if (isfinite(sample.boiler)) o["boiler"] = sample.boiler; else o["boiler"] = nullptr;
    if (isfinite(sample.mix)) o["mix"] = sample.mix; else o["mix"] = nullptr;
    if (isfinite(sample.ret)) o["ret"] = sample.ret; else o["ret"] = nullptr;
    o["pos"] = sample.pos;
    JsonArray relays = o["relays"].to<JsonArray>();
    for (uint8_t i = 0; i < 4; ++i) {
      relays.add(sample.relays[i]);
    }
  }
  send_json(request, 200, doc);
}

void handle_sensors_discover(AsyncWebServerRequest* request) {
  TemperatureSensorInfo found[8] = {};
  const uint8_t count = l0::discover_temperature_sensors(found, 8);

  JsonDocument doc;
  doc["ok"] = true;
  doc["count"] = count;
  JsonArray arr = doc["sensors"].to<JsonArray>();
  for (uint8_t i = 0; i < count; ++i) {
    JsonObject o = arr.add<JsonObject>();
    o["id"] = found[i].id;
    if (isfinite(found[i].celsius)) {
      o["temperature"] = found[i].celsius;
    } else {
      o["temperature"] = nullptr;
    }
    o["valid"] = found[i].valid;
  }

  char detail[64];
  snprintf(detail, sizeof(detail), "Sensor discovery: %u found", static_cast<unsigned>(count));
  log_event(0, 1250, detail);
  send_json(request, 200, doc);
}

void handle_sensor_roles(AsyncWebServerRequest* request) {
  if (g_config == nullptr || g_state == nullptr) {
    send_error(request, 503, "controller state not ready");
    return;
  }

  const SystemState state = published_state();
  const SensorRoleResult roles = verify_sensor_roles(*g_config, state);
  JsonDocument doc;
  doc["ok"] = true;
  doc["summary"] = sensor_role_summary(roles);
  doc["measurable"] = roles.measurable;
  doc["plausible"] = roles.plausible;
  doc["heating_active"] = roles.heating_active;
  doc["all_valid"] = roles.all_valid;
  doc["tolerance_c"] = roles.tolerance_c;

  JsonObject temps = doc["temperatures"].to<JsonObject>();
  if (roles.all_valid) {
    temps["boiler"] = roles.boiler_c;
    temps["mix"] = roles.mix_c;
    temps["return"] = roles.return_c;
  } else {
    temps["boiler"] = nullptr;
    temps["mix"] = nullptr;
    temps["return"] = nullptr;
  }

  JsonObject checks = doc["checks"].to<JsonObject>();
  checks["boiler_ge_mix"] = roles.boiler_ge_mix;
  checks["mix_ge_return"] = roles.mix_ge_return;
  checks["boiler_ge_return"] = roles.boiler_ge_return;

  send_json(request, 200, doc);
}

void handle_counters_get(AsyncWebServerRequest* request) {
  CounterEntry copy[CTR_COUNT] = {};
  taskENTER_CRITICAL(&g_counter_mux);
  memcpy(copy, g_counters, sizeof(copy));
  taskEXIT_CRITICAL(&g_counter_mux);

  static const char* ids[CTR_COUNT] = {"heater", "rad1", "rad2", "floor", "mix_open", "mix_close"};
  const bool open_close = g_config != nullptr && g_config->mixer_wiring == MIX_WIRING_OPEN_CLOSE;
  const char* labels[CTR_COUNT] = {
      "Heater relay",
      "Radiator 1 relay",
      "Radiator 2 relay",
      "Floor pump relay",
      open_close ? "Mixer open relay" : "Mixer direction relay",
      open_close ? "Mixer close relay" : "Mixer motion relay",
  };

  JsonDocument doc;
  doc["persistenceEnabled"] = counter_persistence_enabled();
  doc["saveIntervalS"] = kCounterSaveIntervalMs / 1000;
  JsonArray arr = doc["counters"].to<JsonArray>();
  for (uint8_t i = 0; i < CTR_COUNT; ++i) {
    JsonObject o = arr.add<JsonObject>();
    o["id"] = ids[i];
    o["label"] = labels[i];
    o["cycles"] = copy[i].cycles;
    o["runtimeHours"] = copy[i].runtime_s / 3600.0f;
    o["lastOperated"] = copy[i].last_change_ms;
    o["persisted"] = g_counters_persisted;
  }
  send_json(request, 200, doc);
}

void handle_counters_reset(AsyncWebServerRequest* request, JsonVariant& json) {
  JsonObject in = json.as<JsonObject>();
  const char* counter = in["counter"] | "all";

  if (strcmp(counter, "all") == 0) {
    reset_all_counters();
  } else {
    const int id = counter_index(counter);
    if (id < 0) {
      send_error(request, 400, "unknown counter");
      return;
    }
    taskENTER_CRITICAL(&g_counter_mux);
    reset_counter_locked(static_cast<CounterId>(id));
    taskEXIT_CRITICAL(&g_counter_mux);
  }

  char detail[96];
  snprintf(detail, sizeof(detail), "Relay counters reset: %s", counter);
  log_event(0, 1300, detail);

  JsonDocument doc;
  doc["ok"] = true;
  doc["counter"] = counter;
  send_json(request, 200, doc);
}

// ---------------------------------------------------------------------------
// Test Mode handlers
// ---------------------------------------------------------------------------

// Remaining Test Mode time, rounded up to whole seconds. Derived from the
// portal's own deadline instead of reading test_mode_rem_s back out of the
// shared struct, which the real-time task also publishes.
uint32_t test_remaining_s() {
  if (!g_test_active) {
    return 0;
  }
  const uint32_t now = millis();
  if (static_cast<int32_t>(now - g_test_end_ms) >= 0) {
    return 0;
  }
  return (g_test_end_ms - now + 999u) / 1000u;
}

void handle_test_activate(AsyncWebServerRequest* request) {
  if (g_config == nullptr || g_state == nullptr) {
    send_error(request, 503, "controller not ready");
    return;
  }

  if (!g_test_active) {
    activate_test();
  }

  JsonDocument doc;
  doc["ok"] = true;
  doc["active"] = g_test_active;
  doc["remaining"] = test_remaining_s();
  send_json(request, 200, doc);
}

void handle_test_deactivate(AsyncWebServerRequest* request) {
  if (!g_test_active) {
    JsonDocument doc;
    doc["ok"] = true;
    doc["active"] = false;
    send_json(request, 200, doc);
    return;
  }

  deactivate_test("Test Mode deactivated by user");

  JsonDocument doc;
  doc["ok"] = true;
  doc["active"] = false;
  send_json(request, 200, doc);
}

void handle_test_command(AsyncWebServerRequest* request, JsonVariant& json) {
  if (!g_test_active || g_state == nullptr) {
    send_error(request, 409, "test mode is not active");
    return;
  }
  if (!json.is<JsonObject>()) {
    send_error(request, 400, "request body must be a JSON object");
    return;
  }

  JsonObject in = json.as<JsonObject>();
  const char* output = in["output"] | "";
  const char* mixer = in["mixer"] | "";

#if defined(HPN_BENCH_LOOPBACK)
  const char* note = "bench_loopback";
#else
  const char* note = "arbitration_pending";
#endif

  if (output[0] != '\0') {
    bool on = in["on"] | false;
    bool* target = nullptr;

    if (strcmp(output, "heater") == 0) {
      target = &g_state->heater_intended;
    } else if (strcmp(output, "pump_floor") == 0) {
      target = &g_state->pump_floor_intended;
    } else if (strcmp(output, "pump_rad1") == 0) {
      target = &g_state->pump_rad1_intended;
    } else if (strcmp(output, "pump_rad2") == 0) {
      target = &g_state->pump_rad2_intended;
    } else {
      send_error(request, 400, "unknown output");
      return;
    }

    if (target == &g_state->heater_intended && on) {
      const bool any_pump =
          g_state->pump_floor_intended || g_state->pump_rad1_intended || g_state->pump_rad2_intended;
      if (!any_pump) {
        log_event(1, 1101, "Test command refused: heater requires an active pump");
        JsonDocument doc;
        doc["ok"] = false;
        doc["error"] = "heater requires an active pump";
        doc["reason"] = "heater_requires_pump";
        doc["output"] = output;
        send_json(request, 409, doc);
        return;
      }
    }

    *target = on;

    char detail[96];
    snprintf(detail, sizeof(detail), "Test command: %s %s", output, on ? "on" : "off");
    log_event(0, 1100, detail);

    JsonDocument doc;
    doc["ok"] = true;
    doc["output"] = output;
    doc["on"] = on;
    doc["note"] = note;
    send_json(request, 200, doc);
    return;
  }

  if (mixer[0] != '\0') {
    // Inputs owned by L1 come from the published snapshot; the command itself
    // is still written to the live struct below.
    const SystemState st = published_state();
    if (st.mix_emergency) {
      send_error(request, 409, "mixer is locked by an emergency close");
      return;
    }
    const float ceiling = isfinite(st.mix_ceiling) && st.mix_ceiling > 0.0f
                              ? st.mix_ceiling
                              : (g_config != nullptr ? mixer_ceiling(*g_config) : 1.0f);

    if (strcmp(mixer, "stop") == 0) {
      // Freeze the servo where the valve is now so it does not chase the old
      // target again.
      g_state->mix_target_pos = fminf(st.pos_est_dr, ceiling);
      log_event(0, 1102, "Test command: mixer stop");
      JsonDocument doc;
      doc["ok"] = true;
      doc["mixer"] = "stop";
      doc["max"] = ceiling;
      doc["note"] = "stop_requested";
      send_json(request, 200, doc);
      return;
    }

    if (strcmp(mixer, "open") == 0 || strcmp(mixer, "close") == 0) {
      // A nudge is just a target two percent away; the servo drives to it and
      // a later "stop" or another nudge interrupts it.
      constexpr float kNudge = 0.02f;
      const bool open = strcmp(mixer, "open") == 0;
      float target = st.pos_est_dr + (open ? kNudge : -kNudge);
      if (target < 0.0f) target = 0.0f;
      if (target > ceiling) target = ceiling;
      g_state->mix_target_pos = target;

      char detail[96];
      snprintf(detail, sizeof(detail), "Test command: mixer %s to %.1f%%", mixer,
               static_cast<double>(target * 100.0f));
      log_event(0, 1103, detail);

      JsonDocument doc;
      doc["ok"] = true;
      doc["mixer"] = mixer;
      doc["position"] = target;
      doc["max"] = ceiling;
      doc["note"] = "nudge_queued";
      send_json(request, 200, doc);
      return;
    }

    if (strcmp(mixer, "target") == 0) {
      const float position = in["position"] | 0.0f;
      if (!isfinite(position) || position < 0.0f || position > 1.0f) {
        send_error(request, 400, "mixer position must be between 0 and 1");
        return;
      }
      if (position > ceiling + 0.0001f) {
        send_error(request, 400,
                   "mixer position exceeds the configured maximum opening");
        return;
      }
      g_state->mix_target_pos = position;
      log_event(0, 1104, "Test command: mixer target position stored");
      JsonDocument doc;
      doc["ok"] = true;
      doc["mixer"] = "target";
      doc["position"] = position;
      doc["max"] = ceiling;
      doc["note"] = "target_stored";
      send_json(request, 200, doc);
      return;
    }

    send_error(request, 400, "unknown mixer command");
    return;
  }

  send_error(request, 400, "missing output or mixer command");
}

void handle_test_sequence(AsyncWebServerRequest* request, const char* id) {
  if (!g_test_active) {
    send_error(request, 409, "test mode is not active");
    return;
  }

  if (strcmp(id, "anti-seize") == 0) {
    g_sequence = Sequence::AntiSeize;
    g_sequence_step = 0;
    g_sequence_next_ms = millis();
    log_event(0, 1410, "Anti-seize sequence started");
    JsonDocument doc;
    doc["ok"] = true;
    doc["sequence"] = id;
    send_json(request, 200, doc);
    return;
  }

  if (strcmp(id, "mixer-sweep") == 0 || strcmp(id, "endstop-home") == 0) {
    g_sequence = strcmp(id, "mixer-sweep") == 0 ? Sequence::MixerSweep : Sequence::EndstopHome;
    g_sequence_step = 0;
    g_sequence_next_ms = millis();
    log_event(0, strcmp(id, "mixer-sweep") == 0 ? 1411 : 1412,
              strcmp(id, "mixer-sweep") == 0 ? "Mixer sweep sequence started"
                                             : "Endstop home sequence started");
    JsonDocument doc;
    doc["ok"] = true;
    doc["sequence"] = id;
    send_json(request, 200, doc);
    return;
  }

  if (strcmp(id, "heater-pump") == 0) {
    send_error(request, 501, "test sequence is not implemented yet");
    return;
  }

  send_error(request, 404, "unknown test sequence");
}

// ---------------------------------------------------------------------------
// Emulation handlers (only present in HPN_IO_EMULATION builds)
// ---------------------------------------------------------------------------

#if defined(HPN_IO_EMULATION)

void handle_sim_get(AsyncWebServerRequest* request) {
  const sim::Snapshot snapshot = sim::snapshot();

  JsonDocument doc;
  doc["enabled"] = true;
  doc["revision"] = snapshot.revision;

  JsonArray temperatures = doc["temperatures"].to<JsonArray>();
  const char* roles[] = {"boiler", "return", "mix"};
  for (uint8_t i = 0; i < kTempRoleCount; ++i) {
    JsonObject o = temperatures.add<JsonObject>();
    o["role"] = roles[i];
    o["value"] = snapshot.temperature_c[i];
    o["valid"] = snapshot.temperature_valid[i];
  }

  JsonObject power = doc["power"].to<JsonObject>();
  power["floor"] = snapshot.power_w[static_cast<size_t>(CurrentChannel::PumpFloor)];
  power["rad1"] = snapshot.power_w[static_cast<size_t>(CurrentChannel::PumpRad1)];
  power["rad2"] = snapshot.power_w[static_cast<size_t>(CurrentChannel::PumpRad2)];
  power["mixer"] = snapshot.power_w[static_cast<size_t>(CurrentChannel::Mixer)];

  JsonObject power_set = doc["powerSet"].to<JsonObject>();
  power_set["floor"] = snapshot.power_set_w[static_cast<size_t>(CurrentChannel::PumpFloor)];
  power_set["rad1"] = snapshot.power_set_w[static_cast<size_t>(CurrentChannel::PumpRad1)];
  power_set["rad2"] = snapshot.power_set_w[static_cast<size_t>(CurrentChannel::PumpRad2)];
  power_set["mixer"] = snapshot.power_set_w[static_cast<size_t>(CurrentChannel::Mixer)];

  JsonObject relays = doc["relays"].to<JsonObject>();
  relays["heater"] = snapshot.relay_on[static_cast<size_t>(Relay::Heater)];
  relays["rad1"] = snapshot.relay_on[static_cast<size_t>(Relay::PumpRad1)];
  relays["rad2"] = snapshot.relay_on[static_cast<size_t>(Relay::PumpRad2)];
  relays["floor"] = snapshot.relay_on[static_cast<size_t>(Relay::PumpFloor)];

  JsonObject relay_override = doc["relayOverride"].to<JsonObject>();
  relay_override["heater"] = snapshot.relay_override[static_cast<size_t>(Relay::Heater)];
  relay_override["rad1"] = snapshot.relay_override[static_cast<size_t>(Relay::PumpRad1)];
  relay_override["rad2"] = snapshot.relay_override[static_cast<size_t>(Relay::PumpRad2)];
  relay_override["floor"] = snapshot.relay_override[static_cast<size_t>(Relay::PumpFloor)];

  JsonObject relay_manual = doc["relayManual"].to<JsonObject>();
  relay_manual["heater"] = snapshot.relay_manual_on[static_cast<size_t>(Relay::Heater)];
  relay_manual["rad1"] = snapshot.relay_manual_on[static_cast<size_t>(Relay::PumpRad1)];
  relay_manual["rad2"] = snapshot.relay_manual_on[static_cast<size_t>(Relay::PumpRad2)];
  relay_manual["floor"] = snapshot.relay_manual_on[static_cast<size_t>(Relay::PumpFloor)];

  JsonObject mixer = doc["mixer"].to<JsonObject>();
  mixer["motion"] = snapshot.mixer_motion;
  mixer["open"] = snapshot.mixer_open;
  mixer["override"] = snapshot.mixer_override;
  mixer["manualMotion"] = snapshot.mixer_manual_motion;
  mixer["manualOpen"] = snapshot.mixer_manual_open;
  mixer["position"] = snapshot.mixer_position;

  JsonObject controller = doc["controller"].to<JsonObject>();
  const SystemState st = published_state();
  controller["estimatedPosition"] = st.pos_est_dr * 100.0f;
  controller["targetPosition"] = st.mix_target_pos * 100.0f;
  controller["motion"] = st.mix_motion;
  controller["direction"] = st.mix_dir;
  controller["interlockFault"] = st.mix_interlock_fault;

  doc["followController"] = snapshot.follow_controller;
  doc["realisticPower"] = snapshot.realistic_power;
  doc["realisticBoiler"] = snapshot.realistic_boiler;

  doc["muxChannel"] = snapshot.mux_channel;
  doc["outputByte"] = snapshot.output_byte;
  doc["outputEnabled"] = snapshot.output_enabled;
  doc["interlockFault"] = snapshot.interlock_fault;
  doc["ledCount"] = snapshot.led_count;

  JsonArray leds = doc["leds"].to<JsonArray>();
  for (uint16_t i = 0; i < snapshot.led_count && i < kMaxLeds; ++i) {
    JsonArray rgb = leds.add<JsonArray>();
    rgb.add(snapshot.led_rgb[i * 3 + 0]);
    rgb.add(snapshot.led_rgb[i * 3 + 1]);
    rgb.add(snapshot.led_rgb[i * 3 + 2]);
  }

  send_json(request, 200, doc);
}

void handle_sim_temperature(AsyncWebServerRequest* request, JsonVariant& json) {
  if (!json.is<JsonObject>()) {
    send_error(request, 400, "request body must be a JSON object");
    return;
  }
  JsonObject in = json.as<JsonObject>();
  const char* role_name = in["role"] | "";
  if (!in["value"].is<float>()) {
    send_error(request, 400, "value is required");
    return;
  }
  const float value = in["value"].as<float>();
  const bool valid = in["valid"] | true;

  TempRole role;
  if (strcmp(role_name, "boiler") == 0) {
    role = TempRole::Boiler;
  } else if (strcmp(role_name, "return") == 0) {
    role = TempRole::ReturnFloor;
  } else if (strcmp(role_name, "mix") == 0) {
    role = TempRole::MixOut;
  } else {
    send_error(request, 400, "unknown temperature role");
    return;
  }

  // Emulated sensors use one scale, 15-85 C, for every role.
  constexpr float kEmuTempMin = 15.0f;
  constexpr float kEmuTempMax = 85.0f;
  if (!isfinite(value) || value < kEmuTempMin || value > kEmuTempMax) {
    send_error(request, 400, "temperature out of range (15-85 C)");
    return;
  }

  sim::set_temperature(role, value, valid);

  char detail[96];
  snprintf(detail, sizeof(detail), "Sim temperature %s = %.1f C%s", role_name, value,
           valid ? "" : " (invalid)");
  log_event(0, 1500, detail);

  JsonDocument doc;
  doc["ok"] = true;
  doc["role"] = role_name;
  doc["value"] = value;
  doc["valid"] = valid;
  send_json(request, 200, doc);
}

void handle_sim_power(AsyncWebServerRequest* request, JsonVariant& json) {
  if (!json.is<JsonObject>()) {
    send_error(request, 400, "request body must be a JSON object");
    return;
  }
  JsonObject in = json.as<JsonObject>();
  const char* channel_name = in["channel"] | "";
  if (!in["watts"].is<float>()) {
    send_error(request, 400, "watts is required");
    return;
  }
  const float watts = in["watts"].as<float>();

  CurrentChannel channel;
  if (strcmp(channel_name, "floor") == 0) {
    channel = CurrentChannel::PumpFloor;
  } else if (strcmp(channel_name, "rad1") == 0) {
    channel = CurrentChannel::PumpRad1;
  } else if (strcmp(channel_name, "rad2") == 0) {
    channel = CurrentChannel::PumpRad2;
  } else if (strcmp(channel_name, "mixer") == 0) {
    channel = CurrentChannel::Mixer;
  } else {
    send_error(request, 400, "unknown power channel");
    return;
  }

  if (!isfinite(watts) || watts < 0.0f || watts > 40.0f) {
    send_error(request, 400, "watts out of range (0-40 W)");
    return;
  }

  sim::set_power_w(channel, watts);

  char detail[96];
  snprintf(detail, sizeof(detail), "Sim power %s = %.0f W", channel_name, watts);
  log_event(0, 1501, detail);

  JsonDocument doc;
  doc["ok"] = true;
  doc["channel"] = channel_name;
  doc["watts"] = watts;
  send_json(request, 200, doc);
}

void handle_sim_interlock(AsyncWebServerRequest* request, JsonVariant& json) {
  if (!json.is<JsonObject>() || !json["fault"].is<bool>()) {
    send_error(request, 400, "fault (true/false) is required");
    return;
  }
  sim::set_interlock_fault(json["fault"].as<bool>());
  JsonDocument doc;
  doc["ok"] = true;
  doc["interlockFault"] = json["fault"].as<bool>();
  send_json(request, 200, doc);
}

void handle_sim_reset(AsyncWebServerRequest* request) {
  sim::reset();
  log_event(0, 1502, "Simulation model reset");
  JsonDocument doc;
  doc["ok"] = true;
  send_json(request, 200, doc);
}

void handle_sim_relay(AsyncWebServerRequest* request, JsonVariant& json) {
  if (!json.is<JsonObject>()) {
    send_error(request, 400, "request body must be a JSON object");
    return;
  }
  JsonObject in = json.as<JsonObject>();
  const char* relay_name = in["relay"] | "";
  if (!in["on"].is<bool>()) {
    send_error(request, 400, "on is required");
    return;
  }
  const bool on = in["on"].as<bool>();

  Relay relay;
  if (strcmp(relay_name, "heater") == 0) {
    relay = Relay::Heater;
  } else if (strcmp(relay_name, "floor") == 0) {
    relay = Relay::PumpFloor;
  } else if (strcmp(relay_name, "rad1") == 0) {
    relay = Relay::PumpRad1;
  } else if (strcmp(relay_name, "rad2") == 0) {
    relay = Relay::PumpRad2;
  } else {
    send_error(request, 400, "unknown relay");
    return;
  }

  sim::set_relay_manual(relay, on);

  char detail[96];
  snprintf(detail, sizeof(detail), "Sim relay %s = %s (manual)", relay_name,
           on ? "ON" : "OFF");
  log_event(0, 1503, detail);

  JsonDocument doc;
  doc["ok"] = true;
  doc["relay"] = relay_name;
  doc["on"] = on;
  send_json(request, 200, doc);
}

void handle_sim_mixer(AsyncWebServerRequest* request, JsonVariant& json) {
  if (!json.is<JsonObject>()) {
    send_error(request, 400, "request body must be a JSON object");
    return;
  }
  JsonObject in = json.as<JsonObject>();
  if (!in["motion"].is<bool>()) {
    send_error(request, 400, "motion is required");
    return;
  }
  const bool motion = in["motion"].as<bool>();
  const bool open = in["open"] | false;

  sim::set_mixer_manual(motion, motion && open);

  char detail[96];
  snprintf(detail, sizeof(detail), "Sim mixer = %s (manual)",
           motion ? (open ? "open" : "close") : "stop");
  log_event(0, 1504, detail);

  JsonDocument doc;
  doc["ok"] = true;
  doc["motion"] = motion;
  doc["open"] = motion && open;
  send_json(request, 200, doc);
}

void handle_sim_mode(AsyncWebServerRequest* request, JsonVariant& json) {
  if (!json.is<JsonObject>()) {
    send_error(request, 400, "request body must be a JSON object");
    return;
  }
  JsonObject in = json.as<JsonObject>();
  bool changed = false;

  if (in["followController"].is<bool>()) {
    sim::set_follow_controller(in["followController"].as<bool>());
    changed = true;
  }
  if (in["realisticPower"].is<bool>()) {
    sim::set_realistic_power(in["realisticPower"].as<bool>());
    changed = true;
  }
  if (in["realisticBoiler"].is<bool>()) {
    sim::set_realistic_boiler(in["realisticBoiler"].as<bool>());
    changed = true;
  }
  if (!changed) {
    send_error(request, 400, "no emulation mode field provided");
    return;
  }

  const sim::Snapshot snapshot = sim::snapshot();
  char detail[128];
  snprintf(detail, sizeof(detail), "Sim mode: follow=%s realisticPower=%s realisticBoiler=%s",
           snapshot.follow_controller ? "on" : "off",
           snapshot.realistic_power ? "on" : "off",
           snapshot.realistic_boiler ? "on" : "off");
  log_event(0, 1505, detail);

  JsonDocument doc;
  doc["ok"] = true;
  doc["followController"] = snapshot.follow_controller;
  doc["realisticPower"] = snapshot.realistic_power;
  doc["realisticBoiler"] = snapshot.realistic_boiler;
  send_json(request, 200, doc);
}

void handle_sim_clear_overrides(AsyncWebServerRequest* request) {
  sim::clear_manual_overrides();
  log_event(0, 1506, "Sim manual overrides cleared");
  JsonDocument doc;
  doc["ok"] = true;
  send_json(request, 200, doc);
}

void handle_sim_intent(AsyncWebServerRequest* request, JsonVariant& json) {
  if (!json.is<JsonObject>() || g_state == nullptr) {
    send_error(request, 400, "request body must be a JSON object");
    return;
  }
  JsonObject in = json.as<JsonObject>();
  if (in["heating_loop_1"].is<bool>()) {
    g_state->pump_rad1_intended = in["heating_loop_1"].as<bool>();
    g_state->demand_age_s[CIRCUIT_RAD1] = 0;
  }
  if (in["heating_loop_2"].is<bool>()) {
    g_state->pump_rad2_intended = in["heating_loop_2"].as<bool>();
    g_state->demand_age_s[CIRCUIT_RAD2] = 0;
  }
  if (in["floor_heating"].is<bool>()) {
    g_state->pump_floor_intended = in["floor_heating"].as<bool>();
    g_state->demand_age_s[CIRCUIT_FLOOR] = 0;
  }
  if (in["dhw_active"].is<bool>()) {
    g_state->dhw_meter_active = in["dhw_active"].as<bool>();
    g_state->dhw_active = g_state->dhw_meter_active;
  }
  if (in["keepalive"].is<bool>() && in["keepalive"].as<bool>()) {
    for (uint8_t i = 0; i < CIRCUIT_COUNT; ++i) {
      g_state->demand_age_s[i] = 0;
    }
  }
  log_event(0, 1510, "Simulated HA intent update");
  JsonDocument doc;
  doc["ok"] = true;
  doc["heating_loop_1"] = g_state->pump_rad1_intended;
  doc["heating_loop_2"] = g_state->pump_rad2_intended;
  doc["floor_heating"] = g_state->pump_floor_intended;
  doc["dhw_active"] = g_state->dhw_active;
  send_json(request, 200, doc);
}

void handle_sim_clear_override(AsyncWebServerRequest* request, JsonVariant& json) {
  if (!json.is<JsonObject>()) {
    send_error(request, 400, "request body must be a JSON object");
    return;
  }
  JsonObject in = json.as<JsonObject>();
  const bool mixer = in["mixer"] | false;
  if (mixer) {
    sim::clear_mixer_override();
    log_event(0, 1507, "Sim mixer manual override cleared");
    JsonDocument doc;
    doc["ok"] = true;
    doc["mixer"] = true;
    send_json(request, 200, doc);
    return;
  }

  const char* relay_name = in["relay"] | "";
  Relay relay;
  if (strcmp(relay_name, "heater") == 0) {
    relay = Relay::Heater;
  } else if (strcmp(relay_name, "floor") == 0) {
    relay = Relay::PumpFloor;
  } else if (strcmp(relay_name, "rad1") == 0) {
    relay = Relay::PumpRad1;
  } else if (strcmp(relay_name, "rad2") == 0) {
    relay = Relay::PumpRad2;
  } else {
    send_error(request, 400, "relay or mixer is required");
    return;
  }

  sim::clear_relay_override(relay);
  char detail[96];
  snprintf(detail, sizeof(detail), "Sim relay %s manual override cleared", relay_name);
  log_event(0, 1508, detail);

  JsonDocument doc;
  doc["ok"] = true;
  doc["relay"] = relay_name;
  send_json(request, 200, doc);
}

#endif  // HPN_IO_EMULATION

// ---------------------------------------------------------------------------
// System, status, OTA and static-file handlers
// ---------------------------------------------------------------------------

void handle_system_reboot(AsyncWebServerRequest* request) {
  log_event(1, 1600, "Reboot requested from portal");
  g_reboot_requested = true;
  g_reboot_at_ms = millis() + 500u;

  JsonDocument doc;
  doc["ok"] = true;
  doc["reboot_in_ms"] = 500;
  send_json(request, 200, doc);
}

void handle_system_factory_reset(AsyncWebServerRequest* request) {
  log_event(1, 1601, "Factory reset requested from portal");
  reset_all_counters();
  config_clear();
  g_reboot_requested = true;
  g_reboot_at_ms = millis() + 500u;

  JsonDocument doc;
  doc["ok"] = true;
  doc["reboot_in_ms"] = 500;
  send_json(request, 200, doc);
}

void handle_status(AsyncWebServerRequest* request) {
  JsonDocument doc;
  doc["ok"] = true;
  doc["version"] = HPN_FW_VERSION;
  doc["build"] = String(__DATE__) + " " + __TIME__;
  doc["chip"] = ESP.getChipModel();
  doc["revision"] = ESP.getChipRevision();
  doc["cores"] = ESP.getChipCores();
  doc["sdk"] = ESP.getSdkVersion();
  doc["uptime"] = millis() / 1000;
  doc["freeHeap"] = ESP.getFreeHeap();
  doc["minFreeHeap"] = ESP.getMinFreeHeap();
  doc["taskCount"] = static_cast<uint32_t>(uxTaskGetNumberOfTasks());
  doc["cpuLoad"] = g_cpu_load_pct;
  doc["cpuTotalDelta"] = g_cpu_total_delta;
  doc["cpuIdleDelta"] = g_cpu_idle_delta;
  doc["resetReason"] = reset_reason_name();
  doc["hostname"] = WiFi.getHostname();

  const time_t epoch = time(nullptr);
  const bool ntp_synced = epoch > 1600000000;
  JsonObject ntp = doc["ntp"].to<JsonObject>();
  ntp["enabled"] = g_config != nullptr ? g_config->ntp_enabled : false;
  ntp["synced"] = ntp_synced;
  ntp["epoch"] = ntp_synced ? static_cast<uint64_t>(epoch) : 0;
  ntp["server"] = g_config != nullptr ? g_config->ntp_server : "";
  ntp["timezone"] = g_config != nullptr ? g_config->timezone : "";
  doc["antiSeizeNextDay"] = g_anti_seize_next_day;
  if (ntp_synced && g_anti_seize_next_day != 0) {
    const uint32_t today = static_cast<uint32_t>(static_cast<uint64_t>(epoch) / 86400ULL);
    doc["antiSeizeInDays"] =
        g_anti_seize_next_day > today ? g_anti_seize_next_day - today : 0;
  } else {
    doc["antiSeizeInDays"] = nullptr;
  }

  JsonObject wifi_obj = doc["wifi"].to<JsonObject>();
  const wifi::Status& ws = wifi::status();
  wifi_obj["configured"] = ws.configured;
  wifi_obj["connected"] = ws.connected;
  wifi_obj["rssi"] = ws.rssi;
  wifi_obj["ip"] = WiFi.localIP().toString();
  wifi_obj["state"] = wifi::state_name();

  const mqtt::Status& ms = mqtt::status();
  JsonObject mqtt_obj = doc["mqtt"].to<JsonObject>();
  mqtt_obj["enabled"] = ms.enabled;
  mqtt_obj["connected"] = ms.connected;
  mqtt_obj["state"] = ms.state;
  mqtt_obj["broker"] = ms.broker;
  mqtt_obj["port"] = ms.port;
  mqtt_obj["connectCount"] = ms.connect_count;
  mqtt_obj["disconnectCount"] = ms.disconnect_count;
  mqtt_obj["lastConnect"] = ms.last_connect_ms;
  mqtt_obj["lastDisconnect"] = ms.last_disconnect_ms;
  mqtt_obj["errors"] = ms.errors;
  mqtt_obj["lastError"] = ms.last_error_code;

  JsonObject fs = doc["filesystem"].to<JsonObject>();
  fs["total"] = LittleFS.totalBytes();
  fs["used"] = LittleFS.usedBytes();

  JsonObject test = doc["test"].to<JsonObject>();
  test["active"] = g_test_active;
  test["remaining"] = test_remaining_s();
  test["sequence"] = sequence_name();

  JsonObject portal = doc["portal"].to<JsonObject>();
  portal["accessLogSize"] = kAccessLogSize;
  portal["eventLogSize"] = logger::kEventCapacity;
  portal["slowLogSize"] = logger::kSlowCapacity;
  portal["slowCadenceS"] = logger::kSlowSampleIntervalMs / 1000;
#if defined(HPN_IO_EMULATION)
  portal["emulation"] = true;
#else
  portal["emulation"] = false;
#endif

  send_json(request, 200, doc);
}

const char* content_type_for(const String& path) {
  if (path.endsWith(".html")) return "text/html; charset=utf-8";
  if (path.endsWith(".css")) return "text/css; charset=utf-8";
  if (path.endsWith(".js")) return "application/javascript; charset=utf-8";
  if (path.endsWith(".json")) return "application/json; charset=utf-8";
  if (path.endsWith(".svg")) return "image/svg+xml";
  if (path.endsWith(".ico")) return "image/x-icon";
  if (path.endsWith(".png")) return "image/png";
  if (path.endsWith(".woff2")) return "font/woff2";
  return "application/octet-stream";
}

void handle_static(AsyncWebServerRequest* request) {
  String path = request->url();
  if (path.length() == 0 || path == "/") {
    path = "/index.html";
  }
  if (path == "/emulation") {
    path = "/emulation.html";
  }

#if !defined(HPN_IO_EMULATION)
  if (path == "/emulation.html") {
    send_error(request, 404, "not found");
    return;
  }
#endif

  if (path.indexOf("..") >= 0 || path.indexOf("//") >= 0) {
    send_error(request, 400, "invalid path");
    return;
  }
  if (path.startsWith("/api/") || path == "/ws" || path.endsWith(".md")) {
    send_error(request, 404, "not found");
    return;
  }

  String fs_path = path;
  bool gzip = false;
  if (request->hasHeader("Accept-Encoding")) {
    const String encoding = request->header("Accept-Encoding");
    if (encoding.indexOf("gzip") >= 0) {
      const String gz_path = path + ".gz";
      if (LittleFS.exists(gz_path)) {
        fs_path = gz_path;
        gzip = true;
      }
    }
  }

  if (!LittleFS.exists(fs_path)) {
    send_error(request, 404, "not found");
    return;
  }

  AsyncWebServerResponse* response =
      request->beginResponse(LittleFS, fs_path, content_type_for(path));
  if (response == nullptr) {
    send_error(request, 500, "failed to open asset");
    return;
  }
  if (gzip) {
    response->addHeader("Content-Encoding", "gzip");
  }
  response->addHeader("Cache-Control", "no-cache");
  log_access_request(request, 200);
  request->send(response);
}

void handle_ota_final(AsyncWebServerRequest* request) {
  if (g_ota_ok) {
    log_event(0, 1700, "OTA update successful; rebooting");
    g_reboot_requested = true;
    g_reboot_at_ms = millis() + 1500u;
  } else {
    log_event(2, 1701, g_ota_error.length() > 0 ? g_ota_error.c_str() : "OTA update failed");
  }

  JsonDocument doc;
  doc["ok"] = g_ota_ok;
  if (!g_ota_ok) {
    doc["error"] = g_ota_error.length() > 0 ? g_ota_error.c_str() : "OTA update failed";
  }
  send_json(request, g_ota_ok ? 200 : 500, doc);
}

void handle_ota_upload(AsyncWebServerRequest* request, const String& filename, size_t index,
                       uint8_t* data, size_t len, bool final) {
  (void)request;
  (void)filename;

  if (index == 0) {
    g_ota_ok = false;
    g_ota_error = "";
    if (!Update.begin(UPDATE_SIZE_UNKNOWN)) {
      g_ota_error = Update.errorString();
      return;
    }
  }

  if (g_ota_error.length() > 0) {
    return;
  }

  if (len > 0) {
    if (Update.write(data, len) != len) {
      g_ota_error = Update.errorString();
      return;
    }
  }

  if (final) {
    if (!Update.end(true)) {
      g_ota_error = Update.errorString();
    } else {
      g_ota_ok = true;
    }
  }
}

// ---------------------------------------------------------------------------
// Routes, WebSocket and lifecycle
// ---------------------------------------------------------------------------

void send_snapshot_to(AsyncWebSocketClient* client) {
  if (client == nullptr) {
    return;
  }
  Snapshot snapshot;
  get_snapshot(snapshot);
  JsonDocument doc;
  build_state_json(snapshot, doc);
  String json;
  serializeJson(doc, json);
  client->text(json);
}

void remember_ws_client_ip(uint32_t id, const char* ip) {
  for (size_t i = 0; i < kWsIpSlots; ++i) {
    if (g_ws_ips[i].id == id || g_ws_ips[i].id == 0) {
      g_ws_ips[i].id = id;
      copy_str(g_ws_ips[i].ip, sizeof(g_ws_ips[i].ip), ip);
      return;
    }
  }
  // All slots are occupied. Reuse the first one rather than dropping the
  // mapping; this is only used for access-log attribution.
  g_ws_ips[0].id = id;
  copy_str(g_ws_ips[0].ip, sizeof(g_ws_ips[0].ip), ip);
}

const char* ws_client_ip(uint32_t id) {
  for (size_t i = 0; i < kWsIpSlots; ++i) {
    if (g_ws_ips[i].id == id) {
      return g_ws_ips[i].ip;
    }
  }
  return "unknown";
}

void forget_ws_client_ip(uint32_t id) {
  for (size_t i = 0; i < kWsIpSlots; ++i) {
    if (g_ws_ips[i].id == id) {
      g_ws_ips[i].id = 0;
      g_ws_ips[i].ip[0] = '\0';
      return;
    }
  }
}

void on_ws_event(AsyncWebSocket* server, AsyncWebSocketClient* client, AwsEventType type, void* arg,
                 uint8_t* data, size_t len) {
  (void)server;
  (void)arg;
  (void)data;
  (void)len;

  if (client == nullptr) {
    return;
  }

  switch (type) {
    case WS_EVT_CONNECT: {
      const String ip = client->remoteIP().toString();
      remember_ws_client_ip(client->id(), ip.c_str());
      log_access_raw(ip.c_str(), "WS", "/ws", 101);
      send_snapshot_to(client);
      break;
    }
    case WS_EVT_DISCONNECT: {
      log_access_raw(ws_client_ip(client->id()), "WS", "/ws", 0);
      forget_ws_client_ip(client->id());
      break;
    }
    case WS_EVT_ERROR:
      Serial.printf("[web] WebSocket error id=%u\n", static_cast<unsigned>(client->id()));
      break;
    default:
      break;
  }
}

void register_routes() {
  // Configuration
  g_server.on(AsyncURIMatcher::exact("/api/config"), HTTP_GET,
              [](AsyncWebServerRequest* request) { handle_config_get(request, "all"); });
  g_server.on(AsyncURIMatcher::exact("/api/config/network"), HTTP_GET,
              [](AsyncWebServerRequest* request) { handle_config_get(request, "network"); });
  g_server.on(AsyncURIMatcher::exact("/api/config/safety"), HTTP_GET,
              [](AsyncWebServerRequest* request) { handle_config_get(request, "safety"); });
  g_server.on(AsyncURIMatcher::exact("/api/config/hardware"), HTTP_GET,
              [](AsyncWebServerRequest* request) { handle_config_get(request, "hardware"); });
  g_server.on(AsyncURIMatcher::exact("/api/config/system"), HTTP_GET,
              [](AsyncWebServerRequest* request) { handle_config_get(request, "system"); });
  g_server.on(AsyncURIMatcher::exact("/api/config/runtime"), HTTP_GET,
              [](AsyncWebServerRequest* request) { handle_config_get(request, "runtime"); });
  g_server.on(AsyncURIMatcher::exact("/api/config/export"), HTTP_GET, handle_config_export);
  g_server.on(AsyncURIMatcher::exact("/api/config/import"), HTTP_POST,
              [](AsyncWebServerRequest* request, JsonVariant& json) {
                handle_config_import(request, json);
              });

  g_server.on(AsyncURIMatcher::exact("/api/config/network"), HTTP_PUT,
              [](AsyncWebServerRequest* request, JsonVariant& json) {
                handle_config_put(request, json, "network");
              });
  g_server.on(AsyncURIMatcher::exact("/api/config/safety"), HTTP_PUT,
              [](AsyncWebServerRequest* request, JsonVariant& json) {
                handle_config_put(request, json, "safety");
              });
  g_server.on(AsyncURIMatcher::exact("/api/config/hardware"), HTTP_PUT,
              [](AsyncWebServerRequest* request, JsonVariant& json) {
                handle_config_put(request, json, "hardware");
              });
  g_server.on(AsyncURIMatcher::exact("/api/config/system"), HTTP_PUT,
              [](AsyncWebServerRequest* request, JsonVariant& json) {
                handle_config_put(request, json, "system");
              });

  // Live state, status and read-only runtime parameters
  g_server.on(AsyncURIMatcher::exact("/api/state"), HTTP_GET, [](AsyncWebServerRequest* request) {
    Snapshot snapshot;
    get_snapshot(snapshot);
    JsonDocument doc;
    build_state_json(snapshot, doc);
    send_json(request, 200, doc);
  });
  g_server.on(AsyncURIMatcher::exact("/api/status"), HTTP_GET, handle_status);
  g_server.on(AsyncURIMatcher::exact("/api/runtime"), HTTP_GET,
              [](AsyncWebServerRequest* request) { handle_config_get(request, "runtime"); });

  // Test Mode
  g_server.on(AsyncURIMatcher::exact("/api/test/activate"), HTTP_POST, handle_test_activate);
  g_server.on(AsyncURIMatcher::exact("/api/test/deactivate"), HTTP_POST, handle_test_deactivate);
  g_server.on(AsyncURIMatcher::exact("/api/test/command"), HTTP_POST,
              [](AsyncWebServerRequest* request, JsonVariant& json) {
                handle_test_command(request, json);
              });
  g_server.on(AsyncURIMatcher::exact("/api/test/sequence/anti-seize"), HTTP_POST,
              [](AsyncWebServerRequest* request) { handle_test_sequence(request, "anti-seize"); });
  g_server.on(AsyncURIMatcher::exact("/api/test/sequence/mixer-sweep"), HTTP_POST,
              [](AsyncWebServerRequest* request) { handle_test_sequence(request, "mixer-sweep"); });
  g_server.on(AsyncURIMatcher::exact("/api/test/sequence/endstop-home"), HTTP_POST,
              [](AsyncWebServerRequest* request) { handle_test_sequence(request, "endstop-home"); });
  g_server.on(AsyncURIMatcher::exact("/api/test/sequence/heater-pump"), HTTP_POST,
              [](AsyncWebServerRequest* request) { handle_test_sequence(request, "heater-pump"); });

  // Logs and counters
  g_server.on(AsyncURIMatcher::exact("/api/log/events"), HTTP_GET, handle_log_events);
  g_server.on(AsyncURIMatcher::exact("/api/log/access"), HTTP_GET, handle_log_access);
  g_server.on(AsyncURIMatcher::exact("/api/log/slow"), HTTP_GET, handle_log_slow);
  g_server.on(AsyncURIMatcher::exact("/api/sensors/discover"), HTTP_GET, handle_sensors_discover);
  g_server.on(AsyncURIMatcher::exact("/api/sensors/roles"), HTTP_GET, handle_sensor_roles);
  g_server.on(AsyncURIMatcher::exact("/api/counters"), HTTP_GET, handle_counters_get);
  g_server.on(AsyncURIMatcher::exact("/api/counters/reset"), HTTP_POST,
              [](AsyncWebServerRequest* request, JsonVariant& json) {
                handle_counters_reset(request, json);
              });

  // System actions
  g_server.on(AsyncURIMatcher::exact("/api/system/reboot"), HTTP_POST, handle_system_reboot);
  g_server.on(AsyncURIMatcher::exact("/api/system/factory-reset"), HTTP_POST,
              handle_system_factory_reset);
  g_server.on(AsyncURIMatcher::exact("/api/mqtt/test"), HTTP_POST, [](AsyncWebServerRequest* request) {
    const mqtt::Status& ms = mqtt::status();
    if (!ms.enabled) {
      send_error(request, 409, "MQTT is disabled; set a broker host first");
      return;
    }

    mqtt::request_test();

    JsonDocument doc;
    doc["ok"] = true;
    doc["queued"] = true;
    doc["connected"] = ms.connected;
    doc["state"] = ms.state;
    doc["broker"] = ms.broker;
    doc["port"] = ms.port;
    send_json(request, ms.connected ? 200 : 202, doc);
  });

  // OTA
  g_server.on(AsyncURIMatcher::exact("/api/update"), HTTP_POST, handle_ota_final, handle_ota_upload);

#if defined(HPN_IO_EMULATION)
  // Board emulation API. These routes do not exist in hardware builds.
  g_server.on(AsyncURIMatcher::exact("/api/sim"), HTTP_GET, handle_sim_get);
  g_server.on(AsyncURIMatcher::exact("/api/sim/temperature"), HTTP_POST,
              [](AsyncWebServerRequest* request, JsonVariant& json) {
                handle_sim_temperature(request, json);
              });
  g_server.on(AsyncURIMatcher::exact("/api/sim/power"), HTTP_POST,
              [](AsyncWebServerRequest* request, JsonVariant& json) {
                handle_sim_power(request, json);
              });
  g_server.on(AsyncURIMatcher::exact("/api/sim/relay"), HTTP_POST,
              [](AsyncWebServerRequest* request, JsonVariant& json) {
                handle_sim_relay(request, json);
              });
  g_server.on(AsyncURIMatcher::exact("/api/sim/mixer"), HTTP_POST,
              [](AsyncWebServerRequest* request, JsonVariant& json) {
                handle_sim_mixer(request, json);
              });
  g_server.on(AsyncURIMatcher::exact("/api/sim/mode"), HTTP_POST,
              [](AsyncWebServerRequest* request, JsonVariant& json) {
                handle_sim_mode(request, json);
              });
  g_server.on(AsyncURIMatcher::exact("/api/sim/intent"), HTTP_POST,
              [](AsyncWebServerRequest* request, JsonVariant& json) {
                handle_sim_intent(request, json);
              });
  g_server.on(AsyncURIMatcher::exact("/api/sim/interlock"), HTTP_POST,
              [](AsyncWebServerRequest* request, JsonVariant& json) {
                handle_sim_interlock(request, json);
              });
  g_server.on(AsyncURIMatcher::exact("/api/sim/overrides/clear"), HTTP_POST,
              handle_sim_clear_overrides);
  g_server.on(AsyncURIMatcher::exact("/api/sim/override/clear"), HTTP_POST,
              [](AsyncWebServerRequest* request, JsonVariant& json) {
                handle_sim_clear_override(request, json);
              });
  g_server.on(AsyncURIMatcher::exact("/api/sim/reset"), HTTP_POST, handle_sim_reset);
#endif

  // WebSocket and static files. The catch-all is registered last so the API
  // and WebSocket handlers above win.
  g_server.addHandler(&g_ws);
  g_server.on(AsyncURIMatcher::exact("/"), HTTP_GET, handle_static);
  g_server.on(AsyncURIMatcher::all(), HTTP_GET, handle_static);
  g_server.onNotFound([](AsyncWebServerRequest* request) { send_error(request, 404, "not found"); });
}

}  // namespace

void begin(const Config& config, SystemState& state) {
  g_config = &config;
  g_state = &state;
  state.mode = config.summer_mode ? MODE_SUMMER : MODE_HEAT;

  g_access_head = 0;
  g_access_count = 0;
  g_access_seq = 0;
  memset(g_counters, 0, sizeof(g_counters));
  g_counter_runtime_ms = 0;
  g_counters_dirty = false;
  g_counters_persisted = false;
  g_counter_save_requested = false;
  g_last_counter_save_ms = millis();
  counters_load();
  g_anti_seize_next_day = 0;
  g_anti_seize_dirty = false;
  g_anti_seize_loaded = false;
  g_anti_seize_last_write_ms = millis();
  g_cpu_load_pct = 0.0f;
  g_cpu_last_sample_ms = 0;
  g_cpu_last_total = 0;
  g_cpu_last_idle = 0;
  g_cpu_total_delta = 0;
  g_cpu_idle_delta = 0;
  g_last_broadcast_ms = 0;
  g_last_cleanup_ms = 0;
  g_last_wifi_connected = wifi::status().connected;
  g_test_active = false;
  g_test_end_ms = 0;
  g_sequence = Sequence::None;
  g_sequence_step = 0;
  g_sequence_next_ms = 0;
  g_reboot_requested = false;
  g_reboot_at_ms = 0;
  g_ota_ok = false;
  g_ota_error = "";

  if (!LittleFS.begin(true)) {
    Serial.println("[web] LittleFS mount failed; static assets unavailable");
  } else {
    Serial.printf("[web] LittleFS mounted: %u/%u bytes used\n",
                  static_cast<unsigned>(LittleFS.usedBytes()),
                  static_cast<unsigned>(LittleFS.totalBytes()));
  }

  g_ws.onEvent(on_ws_event);
  register_routes();
  g_server.begin();

  log_event(0, 1000, "Web portal started");
#if defined(HPN_IO_EMULATION)
  log_event(0, 1003, "Board emulation page served at /emulation.html");
#endif
  {
    char detail[48];
    snprintf(detail, sizeof(detail), "Reset reason: %s", reset_reason_name());
    log_event(esp_reset_reason() == ESP_RST_PANIC || esp_reset_reason() == ESP_RST_TASK_WDT ||
                  esp_reset_reason() == ESP_RST_INT_WDT || esp_reset_reason() == ESP_RST_WDT ||
                  esp_reset_reason() == ESP_RST_BROWNOUT
                  ? 2
                  : 0,
              1004, detail);
  }
  Serial.println("[web] HTTP server on port 80");
}

void tick(const Config& config, SystemState& state) {
  (void)config;
  if (g_config == nullptr || g_state == nullptr) {
    return;
  }

  const uint32_t now = millis();
  update_cpu_load(now);

  if (g_test_active) {
    if (static_cast<int32_t>(now - g_test_end_ms) >= 0) {
      deactivate_test("Test Mode expired");
    } else {
      state.test_mode_rem_s = (g_test_end_ms - now + 999u) / 1000u;
      state.mode = MODE_TEST_OVERRIDE;
    }
  } else {
    if (state.test_mode_rem_s != 0) {
      state.test_mode_rem_s = 0;
    }
    if (state.mode == MODE_TEST_OVERRIDE) {
      state.mode = g_config->summer_mode ? MODE_SUMMER : MODE_HEAT;
    }
  }

  // Reads of real-time-owned fields go through the published snapshot; only
  // the Test Mode writes below stay on the live struct.
  const SystemState published = published_state();
  anti_seize_service(config, published, now);
  tick_sequence(now);
  update_counters(published, now);

  if (counter_persistence_enabled()) {
    const bool hourly_due =
        g_counters_dirty && (now - g_last_counter_save_ms >= kCounterSaveIntervalMs);
    if (g_counter_save_requested || hourly_due) {
      g_counter_save_requested = false;
      counters_save();
    }
  }

  const bool wifi_connected = wifi::status().connected;
  if (wifi_connected != g_last_wifi_connected) {
    g_last_wifi_connected = wifi_connected;
    log_event(0, wifi_connected ? 1010 : 1011,
              wifi_connected ? "Wi-Fi connected" : "Wi-Fi link lost");
  }

  if (g_ws.count() > 0 && now - g_last_broadcast_ms >= kBroadcastIntervalMs) {
    g_last_broadcast_ms = now;
    Snapshot snapshot;
    get_snapshot(snapshot);
    JsonDocument doc;
    build_state_json(snapshot, doc);
    String json;
    serializeJson(doc, json);
    g_ws.textAll(json);
  }

  if (now - g_last_cleanup_ms >= 5000u) {
    g_last_cleanup_ms = now;
    g_ws.cleanupClients();
  }

  if (g_reboot_requested && static_cast<int32_t>(now - g_reboot_at_ms) >= 0) {
    Serial.println("[web] rebooting now");
    Serial.flush();
    ESP.restart();
  }
}

void log_event(uint8_t severity, uint16_t code, const char* detail) {
  logger::log_event(severity, code, detail);
}

}  // namespace web
}  // namespace hpn
