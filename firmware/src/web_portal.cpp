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
#include <math.h>
#include <stdio.h>
#include <string.h>
#include <vector>

#include "hpn/web_portal.h"
#include "hpn/config_store.h"
#include "hpn/mqtt_client.h"
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
constexpr uint32_t kSlowSampleIntervalMs = 10000;
constexpr size_t kAccessLogSize = 64;
constexpr size_t kEventLogSize = 64;
constexpr size_t kSlowLogSize = 180;

struct AccessEntry {
  uint32_t seq;
  uint32_t ms;
  char ip[16];
  char method[8];
  char path[64];
  int status;
};

struct EventEntry {
  uint32_t seq;
  uint32_t ms;
  uint8_t severity;
  uint16_t code;
  char detail[96];
  bool latched;
};

struct SlowSample {
  uint32_t ms;
  float boiler;
  float mix;
  float ret;
  float pos;
  bool relays[4];
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
constexpr uint16_t kCounterVersion = 1;
constexpr uint32_t kCounterSaveIntervalMs = 3600000u;

struct CounterBlob {
  uint32_t magic;
  uint16_t version;
  uint16_t size;
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

enum class Sequence : uint8_t { None = 0, AntiSeize };

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

portMUX_TYPE g_event_mux = portMUX_INITIALIZER_UNLOCKED;
EventEntry g_events[kEventLogSize] = {};
uint32_t g_event_head = 0;
uint32_t g_event_count = 0;
uint32_t g_event_seq = 0;

portMUX_TYPE g_slow_mux = portMUX_INITIALIZER_UNLOCKED;
SlowSample g_slow[kSlowLogSize] = {};
uint32_t g_slow_head = 0;
uint32_t g_slow_count = 0;
uint32_t g_last_slow_ms = 0;

portMUX_TYPE g_counter_mux = portMUX_INITIALIZER_UNLOCKED;
CounterEntry g_counters[CTR_COUNT] = {};
uint32_t g_counter_runtime_ms = 0;
bool g_counters_dirty = false;
bool g_counters_persisted = false;
bool g_counter_save_requested = false;
uint32_t g_last_counter_save_ms = 0;

portMUX_TYPE g_snapshot_mux = portMUX_INITIALIZER_UNLOCKED;
Snapshot g_snapshot = {};

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

const char* severity_name(uint8_t severity) {
  switch (severity) {
    case 1:
      return "warn";
    case 2:
      return "error";
    case 3:
      return "safety";
    default:
      return "info";
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
    case DIV_AFTER_HEATING_TIMEOUT:
      return "after_heating_timeout";
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

JsonObject add_circuit_json(JsonObject circuits, const char* key, bool cmd, bool phys,
                            uint32_t after_run_s) {
  JsonObject c = circuits[key].to<JsonObject>();
  c["cmd"] = cmd ? 1 : 0;
  c["phys"] = phys ? 1 : 0;
  c["afterRun"] = after_run_s;
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
  String ip = request->client() != nullptr ? request->client()->remoteIP().toString() : String("?");
  log_access_raw(ip.c_str(), request->methodToString(), request->url().c_str(), status);
}

// ---------------------------------------------------------------------------
// Snapshot and live state JSON
// ---------------------------------------------------------------------------

void capture_snapshot() {
  Snapshot s{};
  if (g_state != nullptr) {
    s.state = *g_state;
  }
  s.captured_ms = millis();
  const wifi::Status& ws = wifi::status();
  s.wifi_connected = ws.connected;
  s.wifi_rssi = ws.rssi;
  copy_str(s.wifi_ip, sizeof(s.wifi_ip), WiFi.localIP().toString().c_str());
  copy_str(s.wifi_state, sizeof(s.wifi_state), wifi::state_name());

  const mqtt::Status& ms = mqtt::status();
  s.mqtt_enabled = ms.enabled;
  s.mqtt_connected = ms.connected;
  copy_str(s.mqtt_state, sizeof(s.mqtt_state), ms.state);
  copy_str(s.mqtt_broker, sizeof(s.mqtt_broker), ms.broker);
  s.mqtt_port = ms.port;
  s.mqtt_connect_count = ms.connect_count;
  s.mqtt_disconnect_count = ms.disconnect_count;
  s.mqtt_last_disconnect_ms = ms.last_disconnect_ms;
  s.mqtt_errors = ms.errors;

  taskENTER_CRITICAL(&g_snapshot_mux);
  g_snapshot = s;
  taskEXIT_CRITICAL(&g_snapshot_mux);
}

void get_snapshot(Snapshot& out) {
  taskENTER_CRITICAL(&g_snapshot_mux);
  out = g_snapshot;
  taskEXIT_CRITICAL(&g_snapshot_mux);
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
  add_circuit_json(circuits, "rad1", st.pump_rad1_intended, st.pump_rad1_phys, st.after_heating_timer_s);
  add_circuit_json(circuits, "rad2", st.pump_rad2_intended, st.pump_rad2_phys, st.after_heating_timer_s);
  JsonObject floor =
      add_circuit_json(circuits, "floor", st.pump_floor_intended, st.pump_floor_phys, st.after_heating_timer_s);
  floor["mixerPos"] = st.pos_est_dr * 100.0f;
  floor["mixerTarget"] = st.mix_target_pos * 100.0f;
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
  heater["cmd"] = st.heater_intended ? 1 : 0;
  heater["phys"] = st.heater_phys ? 1 : 0;
  heater["demandAge"] = st.keepalive_age_s;
  const bool any_pump = st.pump_floor_intended || st.pump_rad1_intended || st.pump_rad2_intended;
  if (!st.heater_intended) {
    heater["reason"] = "off";
  } else if (st.heater_phys) {
    heater["reason"] = "pump_active";
  } else if (!any_pump || (st.active_alarms & WARN_HEATER_NO_PUMP)) {
    heater["reason"] = "pump_required";
  } else {
    heater["reason"] = "arbitration_pending";
  }

  JsonObject safety = doc["safety"].to<JsonObject>();
  JsonArray flags = safety["flags"].to<JsonArray>();
  add_alarm_strings(flags, st.active_alarms);
  safety["latched"].to<JsonArray>();
  safety["errorCode"] = st.system_error_code;
  safety["alarmCount"] = flags.size();

  JsonArray sensors = doc["sensors"].to<JsonArray>();
  add_sensor_json(sensors, cfg.sensor_id_boiler[0] != '\0' ? cfg.sensor_id_boiler : "boiler",
                  "Boiler supply", st.T_boiler, cfg.sensor_error_threshold, now_ms);
  add_sensor_json(sensors, cfg.sensor_id_ret[0] != '\0' ? cfg.sensor_id_ret : "return",
                  "Floor return", st.T_return_floor, cfg.sensor_error_threshold, now_ms);
  add_sensor_json(sensors, cfg.sensor_id_mix[0] != '\0' ? cfg.sensor_id_mix : "mix",
                  "Mixer forward", st.T_mix_out, cfg.sensor_error_threshold, now_ms);

  JsonObject test = doc["test"].to<JsonObject>();
  test["active"] = g_test_active;
  test["remaining"] = st.test_mode_rem_s;
  test["sequence"] = sequence_name();

  JsonObject system = doc["system"].to<JsonObject>();
  system["freeHeap"] = ESP.getFreeHeap();
  system["errorCode"] = st.system_error_code;
  system["divergence"] = divergence_name(st.divergence_reason);
  system["flush"] = flush_name(st.flush_state);
  system["rssi"] = s.wifi_rssi;

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

void fill_network(JsonObject o, const Config& c) {
  o["wifi_ssid"] = c.wifi_ssid;
  o["has_wifi_password"] = c.wifi_password[0] != '\0';
  o["ap_ssid"] = c.ap_ssid;
  o["has_ap_password"] = c.ap_password[0] != '\0';
  o["mqtt_host"] = c.mqtt_host;
  o["mqtt_port"] = c.mqtt_port;
  o["mqtt_user"] = c.mqtt_user;
  o["has_mqtt_password"] = c.mqtt_pass[0] != '\0';
  o["mqtt_base_topic"] = c.mqtt_base_topic;
}

void fill_safety(JsonObject o, const Config& c) {
  o["t_max_hard"] = c.t_max_hard;
  o["t_max_warn"] = c.t_max_warn;
  o["t_overtemp_hysteresis"] = c.t_overtemp_hysteresis;
  o["grad_limit"] = c.grad_limit;
  o["mix_safe_pos"] = c.mix_safe_pos;
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
  o["min_pulse_ms"] = c.min_pulse_ms;
  o["max_pulse_ms"] = c.max_pulse_ms;
  o["pulse_cooldown_ms"] = c.pulse_cooldown_ms;
  o["travel_time_ms"] = c.travel_time_ms;
  o["tick_ms"] = c.tick_ms;
  o["temp_poll_interval_ms"] = c.temp_poll_interval_ms;
  o["recalc_threshold"] = c.recalc_threshold;
  o["encoder_check_interval_s"] = c.encoder_check_interval_s;
  o["encoder_check_pulses"] = c.encoder_check_pulses;
  o["encoder_staleness_s"] = c.encoder_staleness_s;
  o["pos_source"] = c.pos_source;

  o["gpio_pump_floor"] = c.gpio_pump_floor;
  o["gpio_pump_rad1"] = c.gpio_pump_rad1;
  o["gpio_pump_rad2"] = c.gpio_pump_rad2;
  o["gpio_heater"] = c.gpio_heater;
  o["gpio_mix_open"] = c.gpio_mix_open;
  o["gpio_mix_close"] = c.gpio_mix_close;
  o["gpio_onewire"] = c.gpio_onewire;
  o["gpio_led_data"] = c.gpio_led_data;
  o["num_leds"] = c.num_leds;
  JsonArray pump_power = o["has_pump_power"].to<JsonArray>();
  for (uint8_t i = 0; i < 3; ++i) {
    pump_power.add(c.has_pump_power[i]);
  }
  o["i2c_sda"] = c.i2c_sda;
  o["i2c_scl"] = c.i2c_scl;
  o["sensor_id_boiler"] = c.sensor_id_boiler;
  o["sensor_id_ret"] = c.sensor_id_ret;
  o["sensor_id_mix"] = c.sensor_id_mix;
}

void fill_system(JsonObject o, const Config& c) {
  o["summer_mode"] = c.summer_mode;
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
  return true;
}

bool apply_safety(JsonObject in, Config& c, String& err) {
  if (!json_float(in, "t_max_hard", c.t_max_hard, 20.0f, 90.0f, err)) return false;
  if (!json_float(in, "t_max_warn", c.t_max_warn, 20.0f, 90.0f, err)) return false;
  if (!json_float(in, "t_overtemp_hysteresis", c.t_overtemp_hysteresis, 0.5f, 10.0f, err))
    return false;
  if (!json_float(in, "grad_limit", c.grad_limit, 0.05f, 5.0f, err)) return false;
  if (!json_float(in, "mix_safe_pos", c.mix_safe_pos, 0.0f, 1.0f, err)) return false;
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
  return true;
}

bool apply_hardware(JsonObject in, Config& c, String& err) {
  if (!json_float(in, "setpoint_default", c.setpoint_default, 5.0f, 80.0f, err)) return false;
  if (!json_float(in, "deadband_pos", c.deadband_pos, 0.0f, 0.2f, err)) return false;
  if (!json_float(in, "deadband_temp", c.deadband_temp, 0.1f, 5.0f, err)) return false;
  if (!json_uint(in, "min_pulse_ms", c.min_pulse_ms, 10, 1000, err)) return false;
  if (!json_uint(in, "max_pulse_ms", c.max_pulse_ms, 10, 10000, err)) return false;
  if (!json_uint(in, "pulse_cooldown_ms", c.pulse_cooldown_ms, 0, 60000, err)) return false;
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

  if (!json_pin(in, "gpio_pump_floor", c.gpio_pump_floor, err)) return false;
  if (!json_pin(in, "gpio_pump_rad1", c.gpio_pump_rad1, err)) return false;
  if (!json_pin(in, "gpio_pump_rad2", c.gpio_pump_rad2, err)) return false;
  if (!json_pin(in, "gpio_heater", c.gpio_heater, err)) return false;
  if (!json_pin(in, "gpio_mix_open", c.gpio_mix_open, err)) return false;
  if (!json_pin(in, "gpio_mix_close", c.gpio_mix_close, err)) return false;
  if (!json_pin(in, "gpio_onewire", c.gpio_onewire, err)) return false;
  if (!json_pin(in, "gpio_led_data", c.gpio_led_data, err)) return false;
  if (!json_uint8(in, "num_leds", c.num_leds, 0, 8, err)) return false;
  if (!json_pin(in, "i2c_sda", c.i2c_sda, err)) return false;
  if (!json_pin(in, "i2c_scl", c.i2c_scl, err)) return false;

  if (in["has_pump_power"].is<JsonArray>()) {
    JsonArray arr = in["has_pump_power"].as<JsonArray>();
    if (arr.size() != 3) {
      err = "has_pump_power must contain three booleans";
      return false;
    }
    for (uint8_t i = 0; i < 3; ++i) {
      if (!arr[i].is<bool>()) {
        err = "has_pump_power must contain three booleans";
        return false;
      }
      c.has_pump_power[i] = arr[i].as<bool>();
    }
  }

  if (!json_string(in, "sensor_id_boiler", c.sensor_id_boiler, sizeof(c.sensor_id_boiler), true, err))
    return false;
  if (!json_string(in, "sensor_id_ret", c.sensor_id_ret, sizeof(c.sensor_id_ret), true, err))
    return false;
  if (!json_string(in, "sensor_id_mix", c.sensor_id_mix, sizeof(c.sensor_id_mix), true, err))
    return false;

  if (c.min_pulse_ms > c.max_pulse_ms) {
    err = "min_pulse_ms must not exceed max_pulse_ms";
    return false;
  }
  return true;
}

bool apply_system(JsonObject in, Config& c, String& err) {
  if (!json_bool(in, "summer_mode", c.summer_mode, err)) return false;
  return true;
}

// ---------------------------------------------------------------------------
// HTTP response helpers
// ---------------------------------------------------------------------------

void send_json(AsyncWebServerRequest* request, int code, const JsonDocument& doc) {
  AsyncResponseStream* response = request->beginResponseStream("application/json");
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
  if (stored == sizeof(blob) &&
      prefs.getBytes(kCounterKey, &blob, sizeof(blob)) == sizeof(blob) &&
      blob.magic == kCounterMagic && blob.version == kCounterVersion &&
      blob.size == sizeof(blob)) {
    ok = true;
  }
  prefs.end();

  if (!ok) {
    if (stored != 0) {
      Serial.printf("[counters] ignoring invalid NVS blob (%u bytes)\n",
                    static_cast<unsigned>(stored));
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
  update_counter_locked(CTR_MIX_OPEN, st.mix_pulse_ms_phys > 0 && st.mix_pulse_dir_phys > 0, now_ms);
  update_counter_locked(CTR_MIX_CLOSE, st.mix_pulse_ms_phys > 0 && st.mix_pulse_dir_phys < 0, now_ms);

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

void sample_slow(const SystemState& st, uint32_t now_ms) {
  SlowSample sample{};
  sample.ms = now_ms;
  sample.boiler = st.T_boiler.value_c;
  sample.mix = st.T_mix_out.value_c;
  sample.ret = st.T_return_floor.value_c;
  sample.pos = st.pos_est_dr;
  sample.relays[0] = st.heater_phys;
  sample.relays[1] = st.pump_rad1_phys;
  sample.relays[2] = st.pump_rad2_phys;
  sample.relays[3] = st.pump_floor_phys;

  taskENTER_CRITICAL(&g_slow_mux);
  g_slow[g_slow_head] = sample;
  g_slow_head = (g_slow_head + 1) % kSlowLogSize;
  if (g_slow_count < kSlowLogSize) {
    g_slow_count++;
  }
  taskEXIT_CRITICAL(&g_slow_mux);
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
    g_state->mix_pulse_ms_req = 0;
    g_state->mix_pulse_dir_req = 0;
  }

  log_event(0, 1002, reason);
}

void tick_sequence(uint32_t now_ms) {
  if (g_sequence != Sequence::AntiSeize || g_state == nullptr) {
    return;
  }
  if (static_cast<int32_t>(now_ms - g_sequence_next_ms) < 0) {
    return;
  }

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
    reboot_required = true;
  } else if (strcmp(section, "safety") == 0) {
    ok = apply_safety(in, next, err);
  } else if (strcmp(section, "hardware") == 0) {
    ok = apply_hardware(in, next, err);
    reboot_required = true;
  } else if (strcmp(section, "system") == 0) {
    ok = apply_system(in, next, err);
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
  std::vector<EventEntry> entries;
  entries.reserve(kEventLogSize);
  uint32_t seq = 0;

  taskENTER_CRITICAL(&g_event_mux);
  seq = g_event_seq;
  const uint32_t start = (g_event_head + kEventLogSize - g_event_count) % kEventLogSize;
  for (uint32_t i = 0; i < g_event_count; ++i) {
    const EventEntry& entry = g_events[(start + i) % kEventLogSize];
    if (entry.seq > after) {
      entries.push_back(entry);
    }
  }
  taskEXIT_CRITICAL(&g_event_mux);

  JsonDocument doc;
  doc["seq"] = seq;
  doc["count"] = entries.size();
  JsonArray arr = doc["entries"].to<JsonArray>();
  for (const EventEntry& entry : entries) {
    JsonObject o = arr.add<JsonObject>();
    o["seq"] = entry.seq;
    o["ts"] = entry.ms;
    o["severity"] = severity_name(entry.severity);
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

  std::vector<SlowSample> samples;
  samples.reserve(kSlowLogSize);

  taskENTER_CRITICAL(&g_slow_mux);
  const uint32_t start = (g_slow_head + kSlowLogSize - g_slow_count) % kSlowLogSize;
  for (uint32_t i = 0; i < g_slow_count; ++i) {
    const SlowSample& sample = g_slow[(start + i) % kSlowLogSize];
    if (sample.ms >= from && sample.ms <= to) {
      samples.push_back(sample);
    }
  }
  taskEXIT_CRITICAL(&g_slow_mux);

  JsonDocument doc;
  doc["cadence_s"] = kSlowSampleIntervalMs / 1000;
  doc["count"] = samples.size();
  JsonArray arr = doc["samples"].to<JsonArray>();
  for (const SlowSample& sample : samples) {
    JsonObject o = arr.add<JsonObject>();
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

void handle_counters_get(AsyncWebServerRequest* request) {
  CounterEntry copy[CTR_COUNT] = {};
  taskENTER_CRITICAL(&g_counter_mux);
  memcpy(copy, g_counters, sizeof(copy));
  taskEXIT_CRITICAL(&g_counter_mux);

  static const char* ids[CTR_COUNT] = {"heater", "rad1", "rad2", "floor", "mix_open", "mix_close"};
  static const char* labels[CTR_COUNT] = {"Heater", "Radiator 1 pump", "Radiator 2 pump",
                                          "Floor pump", "Mixer open", "Mixer close"};

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
  doc["remaining"] = g_state->test_mode_rem_s;
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
    if (strcmp(mixer, "stop") == 0) {
      g_state->mix_pulse_ms_req = 0;
      log_event(0, 1102, "Test command: mixer stop");
      JsonDocument doc;
      doc["ok"] = true;
      doc["mixer"] = "stop";
      doc["note"] = "stop_requested";
      send_json(request, 200, doc);
      return;
    }

    if (strcmp(mixer, "open") == 0 || strcmp(mixer, "close") == 0) {
      uint32_t pulse_ms = in["pulse_ms"] | 1000;
      uint32_t max_pulse = g_config != nullptr && g_config->max_pulse_ms > 0 ? g_config->max_pulse_ms : 1000;
      if (max_pulse < 500) {
        max_pulse = 500;
      }
      if (pulse_ms < 500) {
        pulse_ms = 500;
      }
      if (pulse_ms > max_pulse) {
        pulse_ms = max_pulse;
      }

      g_state->mix_pulse_dir_req = strcmp(mixer, "open") == 0 ? 1 : -1;
      g_state->mix_pulse_ms_req = pulse_ms;

      char detail[96];
      snprintf(detail, sizeof(detail), "Test command: mixer %s for %ums", mixer,
               static_cast<unsigned>(pulse_ms));
      log_event(0, 1103, detail);

      JsonDocument doc;
      doc["ok"] = true;
      doc["mixer"] = mixer;
      doc["pulse_ms"] = pulse_ms;
      doc["note"] = "pulse_queued";
      send_json(request, 200, doc);
      return;
    }

    if (strcmp(mixer, "target") == 0) {
      const float position = in["position"] | 0.0f;
      if (!isfinite(position) || position < 0.0f || position > 1.0f) {
        send_error(request, 400, "mixer position must be between 0 and 1");
        return;
      }
      g_state->mix_target_pos = position;
      log_event(0, 1104, "Test command: mixer target position stored");
      JsonDocument doc;
      doc["ok"] = true;
      doc["mixer"] = "target";
      doc["position"] = position;
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

  if (strcmp(id, "mixer-sweep") == 0 || strcmp(id, "endstop-home") == 0 ||
      strcmp(id, "heater-pump") == 0) {
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

  doc["followController"] = snapshot.follow_controller;
  doc["realisticPower"] = snapshot.realistic_power;
  doc["realisticBoiler"] = snapshot.realistic_boiler;

  doc["muxChannel"] = snapshot.mux_channel;
  doc["outputByte"] = snapshot.output_byte;
  doc["outputEnabled"] = snapshot.output_enabled;
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

  if (!isfinite(value) || value < -20.0f || value > 120.0f) {
    send_error(request, 400, "temperature out of range");
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
  test["remaining"] = g_state != nullptr ? g_state->test_mode_rem_s : 0;
  test["sequence"] = sequence_name();

  JsonObject portal = doc["portal"].to<JsonObject>();
  portal["accessLogSize"] = kAccessLogSize;
  portal["eventLogSize"] = kEventLogSize;
  portal["slowLogSize"] = kSlowLogSize;
  portal["slowCadenceS"] = kSlowSampleIntervalMs / 1000;
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
  g_server.on(AsyncURIMatcher::exact("/api/sim/overrides/clear"), HTTP_POST,
              handle_sim_clear_overrides);
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

  g_access_head = 0;
  g_access_count = 0;
  g_access_seq = 0;
  g_event_head = 0;
  g_event_count = 0;
  g_event_seq = 0;
  g_slow_head = 0;
  g_slow_count = 0;
  g_last_slow_ms = 0;
  memset(g_counters, 0, sizeof(g_counters));
  g_counter_runtime_ms = 0;
  g_counters_dirty = false;
  g_counters_persisted = false;
  g_counter_save_requested = false;
  g_last_counter_save_ms = millis();
  counters_load();
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
  capture_snapshot();

  log_event(0, 1000, "Web portal started");
#if defined(HPN_IO_EMULATION)
  log_event(0, 1003, "Board emulation page served at /emulation.html");
#endif
  Serial.println("[web] HTTP server on port 80");
}

void tick(const Config& config, SystemState& state) {
  (void)config;
  if (g_config == nullptr || g_state == nullptr) {
    return;
  }

  const uint32_t now = millis();

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

  tick_sequence(now);
  update_counters(state, now);

  if (counter_persistence_enabled()) {
    const bool hourly_due =
        g_counters_dirty && (now - g_last_counter_save_ms >= kCounterSaveIntervalMs);
    if (g_counter_save_requested || hourly_due) {
      g_counter_save_requested = false;
      counters_save();
    }
  }

  if (now - g_last_slow_ms >= kSlowSampleIntervalMs) {
    g_last_slow_ms = now;
    sample_slow(state, now);
  }

  const bool wifi_connected = wifi::status().connected;
  if (wifi_connected != g_last_wifi_connected) {
    g_last_wifi_connected = wifi_connected;
    log_event(0, wifi_connected ? 1010 : 1011,
              wifi_connected ? "Wi-Fi connected" : "Wi-Fi link lost");
  }

  capture_snapshot();

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
  EventEntry entry{};
  entry.ms = millis();
  entry.severity = severity;
  entry.code = code;
  entry.latched = severity >= 3;
  copy_str(entry.detail, sizeof(entry.detail), detail);

  taskENTER_CRITICAL(&g_event_mux);
  entry.seq = ++g_event_seq;
  g_events[g_event_head] = entry;
  g_event_head = (g_event_head + 1) % kEventLogSize;
  if (g_event_count < kEventLogSize) {
    g_event_count++;
  }
  taskEXIT_CRITICAL(&g_event_mux);

  Serial.printf("[event] %s %u %s\n", severity_name(severity), static_cast<unsigned>(code), entry.detail);
}

}  // namespace web
}  // namespace hpn
