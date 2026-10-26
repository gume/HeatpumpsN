#include "hpn/mqtt_client.h"

#include <Arduino.h>
#include <ArduinoJson.h>
#include <PubSubClient.h>
#include <WiFi.h>
#include <math.h>
#include <string.h>
#include <strings.h>

#include "hpn/board.h"
#include "hpn/build_target.h"
#include "hpn/logger.h"
#include "hpn/web_portal.h"
#include "hpn/wifi_client.h"

#if defined(HPN_IO_EMULATION)
#include "hpn/sim.h"
#endif

namespace hpn {
namespace mqtt {
namespace {

constexpr uint32_t kReconnectIntervalMs = 5000;
constexpr uint32_t kPublishIntervalMs = 1000;
constexpr uint32_t kErrorLogIntervalMs = 30000;
constexpr size_t kMqttBufferSize = 4096;
constexpr float kFloatPublishEpsilon = 0.05f;

WiFiClient g_wifi_client;
PubSubClient g_client(g_wifi_client);

const Config* g_config = nullptr;
SystemState* g_state = nullptr;

Status g_status{};
char g_base_topic[48] = "heatpumpsn/controller";
char g_mixer_config_topic[128] = {};
char g_mixer_state_topic[96] = {};
char g_dhw_config_topic[128] = {};
char g_dhw_state_topic[96] = {};
char g_meter_signature[512] = {};
char g_mqtt_payload[kMqttBufferSize] = {};
char g_mac_hex[16] = "000000000000";
char g_device_id[32] = "heatpumpsn_000000000000";
char g_node_id[32] = "hpn_000000000000";
char g_client_id[32] = "hpn-000000000000";

uint32_t g_last_connect_attempt_ms = 0;
uint32_t g_last_publish_ms = 0;
uint32_t g_last_error_log_ms = 0;
uint32_t g_log_event_cursor = 0;
uint32_t g_log_slow_cursor = 0;
volatile bool g_test_requested = false;

struct StateCache {
  bool have_heater = false;
  bool heater = false;
  bool have_heater_virtual = false;
  bool heater_virtual = false;
  bool have_heater_physical = false;
  bool heater_physical = false;

  bool have_pump_floor = false;
  bool pump_floor = false;
  bool have_pump_floor_virtual = false;
  bool pump_floor_virtual = false;
  bool have_pump_floor_physical = false;
  bool pump_floor_physical = false;

  bool have_pump_rad1 = false;
  bool pump_rad1 = false;
  bool have_pump_rad1_physical = false;
  bool pump_rad1_physical = false;

  bool have_pump_rad2 = false;
  bool pump_rad2 = false;
  bool have_pump_rad2_physical = false;
  bool pump_rad2_physical = false;

  bool have_power_floor = false;
  float power_floor = 0.0f;
  bool have_power_rad1 = false;
  float power_rad1 = 0.0f;
  bool have_power_rad2 = false;
  float power_rad2 = 0.0f;
  bool have_power_mixer = false;
  float power_mixer = 0.0f;

  char mixer_state[16] = {};

  char temp_boiler[16] = {};
  char temp_return[16] = {};
  char temp_mix[16] = {};

  bool have_setpoint = false;
  float setpoint = 0.0f;

  bool have_error_status = false;
  uint16_t error_code = 0;
  uint32_t error_alarms = 0;
  char error_code_text[8] = {};
  char error_text[160] = {};
};

StateCache g_cache{};

void set_state(const char* state) {
  snprintf(g_status.state, sizeof(g_status.state), "%s", state);
}

void copy_string(char* dst, size_t dst_size, const char* src) {
  snprintf(dst, dst_size, "%s", src != nullptr ? src : "");
}

void copy_base_topic(char* dst, size_t dst_size, const char* src) {
  if (src == nullptr || src[0] == '\0') {
    src = "heatpumpsn/controller";
  }
  copy_string(dst, dst_size, src);
  size_t len = strlen(dst);
  while (len > 0 && dst[len - 1] == '/') {
    dst[--len] = '\0';
  }
  if (len == 0) {
    copy_string(dst, dst_size, "heatpumpsn/controller");
  }
}

void make_topic(char* out, size_t out_size, const char* suffix) {
  snprintf(out, out_size, "%s/%s", g_base_topic, suffix);
}

void build_identity() {
  String mac = WiFi.macAddress();
  mac.replace(":", "");
  mac.toLowerCase();
  if (mac.length() == 0 || mac == "000000000000") {
    snprintf(g_mac_hex, sizeof(g_mac_hex), "%08lx", static_cast<unsigned long>(millis()));
  } else {
    copy_string(g_mac_hex, sizeof(g_mac_hex), mac.c_str());
  }

  snprintf(g_device_id, sizeof(g_device_id), "heatpumpsn_%s", g_mac_hex);
  snprintf(g_node_id, sizeof(g_node_id), "hpn_%s", g_mac_hex);
  snprintf(g_client_id, sizeof(g_client_id), "hpn-%s", g_mac_hex);
}

void reset_state_cache() {
  g_cache = StateCache{};
}

void publish_availability(const char* payload) {
  char topic[96];
  make_topic(topic, sizeof(topic), "status");
  if (!g_client.publish(topic, payload, true)) {
    Serial.printf("[mqtt] availability publish failed (%s)\n", payload);
  }
}

void publish_bool(const char* suffix, bool value, bool& have, bool& last) {
  if (have && last == value) {
    return;
  }
  char topic[96];
  make_topic(topic, sizeof(topic), suffix);
  const char* payload = value ? "ON" : "OFF";
  if (g_client.publish(topic, payload, true)) {
    have = true;
    last = value;
  }
}

void publish_float(const char* suffix, float value, bool& have, float& last) {
  if (!isfinite(value)) {
    return;
  }
  if (have && fabsf(value - last) < kFloatPublishEpsilon) {
    return;
  }
  char topic[96];
  char payload[24];
  make_topic(topic, sizeof(topic), suffix);
  snprintf(payload, sizeof(payload), "%.2f", value);
  if (g_client.publish(topic, payload, true)) {
    have = true;
    last = value;
  }
}

bool publish_text(const char* suffix, const char* payload, char* last, size_t last_size) {
  if (last[0] != '\0' && strcmp(last, payload) == 0) {
    return true;
  }
  char topic[96];
  make_topic(topic, sizeof(topic), suffix);
  if (g_client.publish(topic, payload, true)) {
    copy_string(last, last_size, payload);
    return true;
  }
  return false;
}

void format_temperature(const SensorReading& reading, char* out, size_t out_size) {
  if (reading.is_valid && isfinite(reading.value_c)) {
    snprintf(out, out_size, "%.2f", reading.value_c);
  } else {
    copy_string(out, out_size, "unknown");
  }
}

// Publish the numeric error code and its text only when the diagnostic posture
// changes. Relay and pump transitions do not touch these entities; warnings,
// alarms and error codes do.
void publish_error_status(const SystemState& st) {
  if (g_cache.have_error_status && g_cache.error_code == st.system_error_code &&
      g_cache.error_alarms == st.active_alarms) {
    return;
  }

  char code_payload[8];
  snprintf(code_payload, sizeof(code_payload), "%u", static_cast<unsigned>(st.system_error_code));
  char text_payload[160];
  format_error_text(st, text_payload, sizeof(text_payload));

  const bool code_ok = publish_text("state/system_error_code", code_payload,
                                    g_cache.error_code_text,
                                    sizeof(g_cache.error_code_text));
  const bool text_ok = publish_text("state/system_error_text", text_payload,
                                    g_cache.error_text, sizeof(g_cache.error_text));
  if (code_ok && text_ok) {
    g_cache.have_error_status = true;
    g_cache.error_code = st.system_error_code;
    g_cache.error_alarms = st.active_alarms;
  }
}

void publish_states() {
  if (g_state == nullptr || g_config == nullptr || !g_client.connected()) {
    return;
  }

  const SystemState& st = *g_state;

  // Switch states are the physical relay states. The virtual binary sensors
  // expose the accepted HA command (intended) separately.
  publish_bool("state/heater", st.heater_phys, g_cache.have_heater, g_cache.heater);
  publish_bool("state/pump_floor", st.pump_floor_phys, g_cache.have_pump_floor,
               g_cache.pump_floor);
  publish_bool("state/pump_rad1", st.pump_rad1_phys, g_cache.have_pump_rad1,
               g_cache.pump_rad1);
  publish_bool("state/pump_rad2", st.pump_rad2_phys, g_cache.have_pump_rad2,
               g_cache.pump_rad2);

  publish_bool("state/heater_virtual", st.heater_intended, g_cache.have_heater_virtual,
               g_cache.heater_virtual);
  publish_bool("state/heater_physical", st.heater_phys, g_cache.have_heater_physical,
               g_cache.heater_physical);
  publish_bool("state/pump_floor_virtual", st.pump_floor_intended,
               g_cache.have_pump_floor_virtual, g_cache.pump_floor_virtual);
  publish_bool("state/pump_floor_physical", st.pump_floor_phys,
               g_cache.have_pump_floor_physical, g_cache.pump_floor_physical);
  publish_bool("state/pump_rad1_physical", st.pump_rad1_phys,
               g_cache.have_pump_rad1_physical, g_cache.pump_rad1_physical);
  publish_bool("state/pump_rad2_physical", st.pump_rad2_phys,
               g_cache.have_pump_rad2_physical, g_cache.pump_rad2_physical);

  publish_float("state/power_floor", st.pump_power_w[0], g_cache.have_power_floor,
                g_cache.power_floor);
  publish_float("state/power_rad1", st.pump_power_w[1], g_cache.have_power_rad1,
                g_cache.power_rad1);
  publish_float("state/power_rad2", st.pump_power_w[2], g_cache.have_power_rad2,
                g_cache.power_rad2);
  publish_float("state/power_mixer", st.mixer_power_w, g_cache.have_power_mixer,
                g_cache.power_mixer);

#if defined(HPN_IO_EMULATION)
  // The bench page can drive the mixer directly, bypassing L1. Reflect the
  // emulated carrier output rather than only the L1 pulse state.
  const sim::Snapshot sim_state = sim::snapshot();
  const bool mixer_motion = sim_state.mixer_motion;
  const bool mixer_open = sim_state.mixer_open;
#else
  const bool mixer_motion = st.mix_motion;
  const bool mixer_open = st.mix_dir > 0;
#endif
  const char* mixer_state =
      !mixer_motion ? "idle" : (mixer_open ? "opening" : "closing");
  publish_text("state/mixer_state", mixer_state, g_cache.mixer_state,
               sizeof(g_cache.mixer_state));

  char temp_payload[16];
  format_temperature(st.T_boiler, temp_payload, sizeof(temp_payload));
  publish_text("state/temp_boiler", temp_payload, g_cache.temp_boiler,
               sizeof(g_cache.temp_boiler));
  format_temperature(st.T_return_floor, temp_payload, sizeof(temp_payload));
  publish_text("state/temp_return", temp_payload, g_cache.temp_return,
               sizeof(g_cache.temp_return));
  format_temperature(st.T_mix_out, temp_payload, sizeof(temp_payload));
  publish_text("state/temp_mix", temp_payload, g_cache.temp_mix,
               sizeof(g_cache.temp_mix));

  publish_float("state/floor_setpoint", g_config->floor_setpoint_shadow,
                g_cache.have_setpoint, g_cache.setpoint);

  publish_error_status(st);
}

void publish_config(const char* component, const char* object_id,
                    const JsonDocument& doc) {
  char topic[160];
  snprintf(topic, sizeof(topic), "homeassistant/%s/%s/%s/config", component,
           g_node_id, object_id);

  char payload[kMqttBufferSize];
  const size_t len = serializeJson(doc, payload, sizeof(payload));
  if (len == 0) {
    Serial.printf("[mqtt] discovery payload too large for %s/%s\n", component,
                  object_id);
    return;
  }
  if (!g_client.publish(topic, payload, true)) {
    Serial.printf("[mqtt] discovery publish failed for %s/%s\n", component,
                  object_id);
  }
}

void add_device(JsonObject entity) {
  JsonObject device = entity["device"].to<JsonObject>();
  device["identifiers"].to<JsonArray>().add(g_device_id);
  device["name"] = "HeatpumpsN Controller";
  device["manufacturer"] = "HeatpumpsN";
  device["model"] = board_config().name;
  device["sw_version"] = HPN_FW_VERSION;
  device["configuration_url"] = String("http://") + WiFi.localIP().toString() + "/";
}

void add_availability(JsonObject entity) {
  entity["availability_topic"] = String(g_base_topic) + "/status";
  entity["payload_available"] = "online";
  entity["payload_not_available"] = "offline";
}

void add_entity(JsonObject entity, const char* name, const char* object_id) {
  entity["name"] = name;
  entity["unique_id"] = String(g_device_id) + "_" + object_id;
  entity["object_id"] = object_id;
  add_availability(entity);
  add_device(entity);
}

void publish_switch(const char* object_id, const char* name, const char* state_topic,
                    const char* command_topic, const char* icon) {
  JsonDocument doc;
  JsonObject entity = doc.to<JsonObject>();
  add_entity(entity, name, object_id);
  entity["state_topic"] = String(g_base_topic) + "/" + state_topic;
  entity["command_topic"] = String(g_base_topic) + "/" + command_topic;
  entity["payload_on"] = "ON";
  entity["payload_off"] = "OFF";
  entity["state_on"] = "ON";
  entity["state_off"] = "OFF";
  entity["icon"] = icon;
  publish_config("switch", object_id, doc);
}

void publish_binary_sensor(const char* object_id, const char* name,
                           const char* state_topic, const char* device_class) {
  JsonDocument doc;
  JsonObject entity = doc.to<JsonObject>();
  add_entity(entity, name, object_id);
  entity["state_topic"] = String(g_base_topic) + "/" + state_topic;
  entity["payload_on"] = "ON";
  entity["payload_off"] = "OFF";
  entity["device_class"] = device_class;
  publish_config("binary_sensor", object_id, doc);
}

void publish_power_sensor(const char* object_id, const char* name,
                          const char* state_topic) {
  JsonDocument doc;
  JsonObject entity = doc.to<JsonObject>();
  add_entity(entity, name, object_id);
  entity["state_topic"] = String(g_base_topic) + "/" + state_topic;
  entity["device_class"] = "power";
  entity["unit_of_measurement"] = "W";
  entity["state_class"] = "measurement";
  entity["suggested_display_precision"] = 1;
  entity["icon"] = "mdi:flash";
  publish_config("sensor", object_id, doc);
}

void publish_temperature_sensor(const char* object_id, const char* name,
                                const char* state_topic) {
  JsonDocument doc;
  JsonObject entity = doc.to<JsonObject>();
  add_entity(entity, name, object_id);
  entity["state_topic"] = String(g_base_topic) + "/" + state_topic;
  entity["device_class"] = "temperature";
  entity["unit_of_measurement"] = "°C";
  entity["state_class"] = "measurement";
  entity["suggested_display_precision"] = 1;
  publish_config("sensor", object_id, doc);
}

void publish_mixer_sensor() {
  JsonDocument doc;
  JsonObject entity = doc.to<JsonObject>();
  add_entity(entity, "Mixer state", "mixer_state");
  entity["state_topic"] = String(g_base_topic) + "/state/mixer_state";
  entity["device_class"] = "enum";
  JsonArray options = entity["options"].to<JsonArray>();
  options.add("idle");
  options.add("opening");
  options.add("closing");
  entity["icon"] = "mdi:valve";
  publish_config("sensor", "mixer_state", doc);
}

void publish_setpoint_number() {
  JsonDocument doc;
  JsonObject entity = doc.to<JsonObject>();
  add_entity(entity, "Floor setpoint", "floor_setpoint");
  entity["state_topic"] = String(g_base_topic) + "/state/floor_setpoint";
  entity["command_topic"] = String(g_base_topic) + "/cmd/floor_setpoint";
  entity["min"] = 5.0f;
  entity["max"] = 80.0f;
  entity["step"] = 0.5f;
  entity["unit_of_measurement"] = "°C";
  entity["mode"] = "box";
  entity["icon"] = "mdi:thermostat";
  publish_config("number", "floor_setpoint", doc);
}

void publish_error_sensors() {
  {
    JsonDocument doc;
    JsonObject entity = doc.to<JsonObject>();
    add_entity(entity, "System error code", "system_error_code");
    entity["state_topic"] = String(g_base_topic) + "/state/system_error_code";
    entity["icon"] = "mdi:alert-circle-outline";
    entity["entity_category"] = "diagnostic";
    publish_config("sensor", "system_error_code", doc);
  }
  {
    JsonDocument doc;
    JsonObject entity = doc.to<JsonObject>();
    add_entity(entity, "System error text", "system_error_text");
    entity["state_topic"] = String(g_base_topic) + "/state/system_error_text";
    entity["icon"] = "mdi:message-alert-outline";
    entity["entity_category"] = "diagnostic";
    publish_config("sensor", "system_error_text", doc);
  }
}

void publish_discovery() {
  publish_switch("heater", "Heater", "state/heater", "cmd/heater", "mdi:fire");
  publish_switch("pump_floor", "Pump floor", "state/pump_floor", "cmd/pump_floor",
                 "mdi:pump");
  publish_switch("pump_rad1", "Pump radiator 1", "state/pump_rad1", "cmd/pump_rad1",
                 "mdi:pump");
  publish_switch("pump_rad2", "Pump radiator 2", "state/pump_rad2", "cmd/pump_rad2",
                 "mdi:pump");

  publish_binary_sensor("heater_virtual", "Heater virtual", "state/heater_virtual",
                        "heat");
  publish_binary_sensor("heater_physical", "Heater physical", "state/heater_physical",
                        "heat");
  publish_binary_sensor("pump_floor_virtual", "Pump floor virtual",
                        "state/pump_floor_virtual", "running");
  publish_binary_sensor("pump_floor_physical", "Pump floor physical",
                        "state/pump_floor_physical", "running");
  publish_binary_sensor("pump_rad1_physical", "Pump radiator 1 physical",
                        "state/pump_rad1_physical", "running");
  publish_binary_sensor("pump_rad2_physical", "Pump radiator 2 physical",
                        "state/pump_rad2_physical", "running");

  publish_power_sensor("power_floor", "Floor pump power", "state/power_floor");
  publish_power_sensor("power_rad1", "Radiator 1 pump power", "state/power_rad1");
  publish_power_sensor("power_rad2", "Radiator 2 pump power", "state/power_rad2");
  publish_power_sensor("power_mixer", "Mixer motor power", "state/power_mixer");

  publish_mixer_sensor();

  publish_temperature_sensor("temp_boiler", "Boiler supply temperature",
                             "state/temp_boiler");
  publish_temperature_sensor("temp_return", "Floor return temperature",
                             "state/temp_return");
  publish_temperature_sensor("temp_mix", "Mixer forward temperature",
                             "state/temp_mix");

  publish_setpoint_number();
  publish_error_sensors();

  web::log_event(0, 1806, "MQTT Home Assistant discovery published");
}

void publish_test() {
  if (!g_client.connected()) {
    return;
  }
  char topic[96];
  char payload[96];
  make_topic(topic, sizeof(topic), "test");
  snprintf(payload, sizeof(payload), "{\"uptime\":%lu,\"ip\":\"%s\"}",
           static_cast<unsigned long>(millis() / 1000),
           WiFi.localIP().toString().c_str());
  const bool ok = g_client.publish(topic, payload, false);
  web::log_event(0, 1805, ok ? "MQTT test message published"
                              : "MQTT test message publish failed");
}

void resolve_log_topic(const char* override_topic, const char* suffix, char* out,
                       size_t out_size) {
  if (override_topic != nullptr && override_topic[0] != '\0') {
    copy_string(out, out_size, override_topic);
  } else {
    make_topic(out, out_size, suffix);
  }
}

// Streams new RAM log records to MQTT. The ring buffers remain the owner; this
// only advances a per-stream cursor so a broker outage never blocks a task.
void publish_logs(const Config& config) {
  if (!config.mqtt_log_enabled) {
    g_log_event_cursor = logger::event_seq();
    g_log_slow_cursor = logger::slow_seq();
    return;
  }
  if (g_state == nullptr || !g_client.connected()) {
    return;
  }

  char topic[128];
  char payload[768];

  logger::Event events[8];
  for (;;) {
    const size_t count = logger::events_after(g_log_event_cursor, events, 8);
    for (size_t i = 0; i < count; ++i) {
      JsonDocument doc;
      doc["seq"] = events[i].seq;
      doc["ts"] = events[i].ms;
      if (g_state->wall_time_epoch_s > 1600000000ULL) {
        doc["wall"] = static_cast<uint32_t>(g_state->wall_time_epoch_s);
      }
      doc["severity"] = logger::severity_name(events[i].severity);
      doc["code"] = events[i].code;
      doc["detail"] = events[i].detail;
      doc["latched"] = events[i].latched;

      const size_t len = serializeJson(doc, payload, sizeof(payload));
      resolve_log_topic(config.mqtt_log_event_topic, "log/event", topic,
                        sizeof(topic));
      if (len == 0) {
        // A record that cannot fit must not stall the stream forever.
        g_log_event_cursor = events[i].seq;
        continue;
      }
      if (!g_client.publish(topic, payload, false)) {
        return;
      }
      g_log_event_cursor = events[i].seq;
    }
    if (count < 8) {
      break;
    }
  }

  logger::SlowSample samples[8];
  for (;;) {
    const size_t count = logger::slow_after(g_log_slow_cursor, samples, 8);
    for (size_t i = 0; i < count; ++i) {
      JsonDocument doc;
      doc["seq"] = samples[i].seq;
      doc["ts"] = samples[i].ms;
      if (g_state->wall_time_epoch_s > 1600000000ULL) {
        doc["wall"] = static_cast<uint32_t>(g_state->wall_time_epoch_s);
      }
      if (isfinite(samples[i].boiler)) doc["boiler"] = samples[i].boiler;
      else doc["boiler"] = nullptr;
      if (isfinite(samples[i].mix)) doc["mix"] = samples[i].mix;
      else doc["mix"] = nullptr;
      if (isfinite(samples[i].ret)) doc["ret"] = samples[i].ret;
      else doc["ret"] = nullptr;
      doc["pos"] = samples[i].pos;
      JsonArray relays = doc["relays"].to<JsonArray>();
      for (uint8_t r = 0; r < 4; ++r) {
        relays.add(samples[i].relays[r]);
      }

      const size_t len = serializeJson(doc, payload, sizeof(payload));
      resolve_log_topic(config.mqtt_log_slow_topic, "log/slow", topic,
                        sizeof(topic));
      if (len == 0) {
        g_log_slow_cursor = samples[i].seq;
        continue;
      }
      if (!g_client.publish(topic, payload, false)) {
        return;
      }
      g_log_slow_cursor = samples[i].seq;
    }
    if (count < 8) {
      break;
    }
  }
}

bool parse_on_off(const char* value, bool& out) {
  if (strcasecmp(value, "ON") == 0 || strcmp(value, "1") == 0 ||
      strcasecmp(value, "true") == 0) {
    out = true;
    return true;
  }
  if (strcasecmp(value, "OFF") == 0 || strcmp(value, "0") == 0 ||
      strcasecmp(value, "false") == 0) {
    out = false;
    return true;
  }
  return false;
}

void handle_command(const char* topic, const char* value) {
  if (g_state == nullptr) {
    return;
  }

  const size_t base_len = strlen(g_base_topic);
  if (strlen(topic) <= base_len || strncmp(topic, g_base_topic, base_len) != 0 ||
      topic[base_len] != '/') {
    return;
  }
  const char* suffix = topic + base_len + 1;

  if (strcmp(suffix, "cmd/heater") == 0 || strcmp(suffix, "cmd/pump_floor") == 0 ||
      strcmp(suffix, "cmd/pump_rad1") == 0 || strcmp(suffix, "cmd/pump_rad2") == 0) {
    bool on = false;
    if (!parse_on_off(value, on)) {
      web::log_event(1, 1803, "MQTT command rejected: expected ON/OFF");
      return;
    }

    if (on && g_config != nullptr && g_config->summer_mode) {
      web::log_event(1, 1808, "MQTT command rejected in summer mode");
      return;
    }

    if (strcmp(suffix, "cmd/heater") == 0) {
      g_state->heater_intended = on;
      if (on) {
        g_state->keepalive_age_s = 0;
      }
    } else if (strcmp(suffix, "cmd/pump_floor") == 0) {
      g_state->pump_floor_intended = on;
    } else if (strcmp(suffix, "cmd/pump_rad1") == 0) {
      g_state->pump_rad1_intended = on;
    } else {
      g_state->pump_rad2_intended = on;
    }

    char detail[80];
    snprintf(detail, sizeof(detail), "MQTT %s = %s", suffix + 4, on ? "ON" : "OFF");
    web::log_event(0, 1803, detail);
    g_last_publish_ms = 0;
    return;
  }

  if (strcmp(suffix, "cmd/floor_setpoint") == 0) {
    char* end = nullptr;
    const float value_c = strtof(value, &end);
    if (end == value || *end != '\0' || !isfinite(value_c) || value_c < 5.0f ||
        value_c > 80.0f) {
      web::log_event(1, 1804, "MQTT setpoint rejected: out of range");
      return;
    }
    config_mutable().floor_setpoint_shadow = value_c;
    char detail[80];
    snprintf(detail, sizeof(detail), "MQTT floor setpoint = %.1f C",
             static_cast<double>(value_c));
    web::log_event(0, 1804, detail);
    g_last_publish_ms = 0;
  }
}

bool handle_discovery_config(const char* topic, const char* payload);
void handle_meter_message(const char* topic, const char* payload);

void on_message(char* topic, uint8_t* payload, unsigned int length) {
  if (topic == nullptr || payload == nullptr) {
    return;
  }

  const size_t copy_len =
      length < sizeof(g_mqtt_payload) - 1 ? length : sizeof(g_mqtt_payload) - 1;
  memcpy(g_mqtt_payload, payload, copy_len);
  g_mqtt_payload[copy_len] = '\0';

  char* start = g_mqtt_payload;
  while (*start == ' ' || *start == '\t' || *start == '\r' || *start == '\n') {
    ++start;
  }
  size_t len = strlen(start);
  while (len > 0 && (start[len - 1] == ' ' || start[len - 1] == '\t' ||
                     start[len - 1] == '\r' || start[len - 1] == '\n')) {
    start[--len] = '\0';
  }

  if (handle_discovery_config(topic, start)) {
    return;
  }
  handle_meter_message(topic, start);
  handle_command(topic, start);
}

bool parse_float_payload(const char* text, float& out) {
  if (text == nullptr || text[0] == '\0') {
    return false;
  }
  char* end = nullptr;
  const float value = strtof(text, &end);
  if (end == text || !isfinite(value)) {
    return false;
  }
  while (*end == ' ' || *end == '\t' || *end == '\r' || *end == '\n') {
    ++end;
  }
  if (*end != '\0') {
    return false;
  }
  out = value;
  return true;
}

void copy_trimmed(char* dst, size_t dst_size, const char* src, size_t len) {
  while (len > 0 && (src[0] == ' ' || src[0] == '\t')) {
    ++src;
    --len;
  }
  while (len > 0 && (src[len - 1] == ' ' || src[len - 1] == '\t' || src[len - 1] == '\r' ||
                     src[len - 1] == '\n')) {
    --len;
  }
  if (len >= dst_size) {
    len = dst_size - 1;
  }
  memcpy(dst, src, len);
  dst[len] = '\0';
}

void strip_quotes(char* text) {
  const size_t len = strlen(text);
  if (len >= 2 && ((text[0] == '\'' && text[len - 1] == '\'') ||
                   (text[0] == '"' && text[len - 1] == '"'))) {
    text[len - 1] = '\0';
    memmove(text, text + 1, len - 1);
  }
}

// Parse the EMS-ESP style state payload for one configured field. Supports
// top-level fields ("tapwateractive") and nested fields ("dhw.charging").
bool extract_payload_field(const char* payload, const char* field, char* out, size_t out_size) {
  if (payload == nullptr || field == nullptr || field[0] == '\0') {
    return false;
  }

  static char line[192];
  static char key[64];
  static char value[96];
  static char qualified[96];
  char section[32] = {};
  const char* cursor = payload;
  while (*cursor != '\0') {
    const char* line_end = strchr(cursor, '\n');
    const size_t raw_len = line_end != nullptr ? static_cast<size_t>(line_end - cursor)
                                               : strlen(cursor);
    copy_trimmed(line, sizeof(line), cursor, raw_len);

    const size_t len = strlen(line);
    if (len == 0) {
      if (line_end == nullptr) break;
      cursor = line_end + 1;
      continue;
    }

    if (line[len - 1] == ':') {
      line[len - 1] = '\0';
      copy_trimmed(section, sizeof(section), line, strlen(line));
    } else {
      char* colon = strchr(line, ':');
      if (colon != nullptr) {
        *colon = '\0';
        copy_trimmed(key, sizeof(key), line, strlen(line));
        copy_trimmed(value, sizeof(value), colon + 1, strlen(colon + 1));
        strip_quotes(value);

        if (section[0] != '\0') {
          snprintf(qualified, sizeof(qualified), "%s.%s", section, key);
        } else {
          copy_string(qualified, sizeof(qualified), key);
        }

        if (strcmp(field, key) == 0 || strcmp(field, qualified) == 0) {
          copy_string(out, out_size, value);
          return true;
        }
      }
    }

    if (line_end == nullptr) {
      break;
    }
    cursor = line_end + 1;
  }
  return false;
}

void handle_meter_message(const char* topic, const char* payload) {
  if (g_state == nullptr || g_config == nullptr) {
    return;
  }

  if (g_mixer_state_topic[0] != '\0' && strcmp(topic, g_mixer_state_topic) == 0) {
    float value = 0.0f;
    if (!parse_float_payload(payload, value)) {
      return;
    }
    if (g_config->mixer_meter_scale == 0) {
      value /= 100.0f;
    }
    if (value < 0.0f) value = 0.0f;
    if (value > 1.0f) value = 1.0f;
    g_state->pos_encoder = value;
    g_state->pos_encoder_ms = millis();
    g_state->pos_encoder_valid = true;
    return;
  }

  if (g_dhw_state_topic[0] != '\0' && strcmp(topic, g_dhw_state_topic) == 0) {
    bool active = false;
    char value[32];
    if (extract_payload_field(payload, g_config->dhw_meter_field, value, sizeof(value))) {
      if (parse_on_off(value, active)) {
        g_state->dhw_active = active;
        return;
      }
      char* end = nullptr;
      const long numeric = strtol(value, &end, 10);
      if (end != value) {
        g_state->dhw_active = numeric != 0;
        return;
      }
    }
    // Some HA discovery entries use a value_template that already extracts the
    // boolean, so the state payload is just ON/OFF.
    if (parse_on_off(payload, active)) {
      g_state->dhw_active = active;
    }
  }
}

bool handle_discovery_config(const char* topic, const char* payload) {
  if (g_mixer_config_topic[0] != '\0' && strcmp(topic, g_mixer_config_topic) == 0) {
    JsonDocument doc;
    if (deserializeJson(doc, payload) == DeserializationError::Ok) {
      const char* state_topic = doc["state_topic"];
      if (state_topic != nullptr && state_topic[0] != '\0') {
        copy_string(g_mixer_state_topic, sizeof(g_mixer_state_topic), state_topic);
        if (g_client.connected()) {
          g_client.subscribe(g_mixer_state_topic, 0);
        }
      }
    }
    return true;
  }
  if (g_dhw_config_topic[0] != '\0' && strcmp(topic, g_dhw_config_topic) == 0) {
    JsonDocument doc;
    if (deserializeJson(doc, payload) == DeserializationError::Ok) {
      const char* state_topic = doc["state_topic"];
      if (state_topic != nullptr && state_topic[0] != '\0') {
        copy_string(g_dhw_state_topic, sizeof(g_dhw_state_topic), state_topic);
        if (g_client.connected()) {
          g_client.subscribe(g_dhw_state_topic, 0);
        }
      }
    }
    return true;
  }
  return false;
}

void make_discovery_topic(char* out, size_t out_size, const char* component, const char* device,
                          const char* field) {
  snprintf(out, out_size, "homeassistant/%s/%s/%s/config", component, device, field);
}

void subscribe_meter_topics(const Config& config) {
  g_mixer_config_topic[0] = '\0';
  g_mixer_state_topic[0] = '\0';
  g_dhw_config_topic[0] = '\0';
  g_dhw_state_topic[0] = '\0';

  if (config.mixer_meter_topic[0] != '\0') {
    copy_string(g_mixer_state_topic, sizeof(g_mixer_state_topic), config.mixer_meter_topic);
  } else if (config.mixer_meter_device[0] != '\0' && config.mixer_meter_field[0] != '\0') {
    make_discovery_topic(g_mixer_config_topic, sizeof(g_mixer_config_topic), "sensor",
                         config.mixer_meter_device, config.mixer_meter_field);
  }

  if (config.dhw_meter_topic[0] != '\0') {
    copy_string(g_dhw_state_topic, sizeof(g_dhw_state_topic), config.dhw_meter_topic);
  } else if (config.dhw_meter_device[0] != '\0' && config.dhw_meter_entity[0] != '\0') {
    make_discovery_topic(g_dhw_config_topic, sizeof(g_dhw_config_topic), "binary_sensor",
                         config.dhw_meter_device, config.dhw_meter_entity);
  }

  if (!g_client.connected()) {
    return;
  }
  if (g_mixer_config_topic[0] != '\0') {
    g_client.subscribe(g_mixer_config_topic, 1);
  }
  if (g_mixer_state_topic[0] != '\0') {
    g_client.subscribe(g_mixer_state_topic, 0);
  }
  if (g_dhw_config_topic[0] != '\0') {
    g_client.subscribe(g_dhw_config_topic, 1);
  }
  if (g_dhw_state_topic[0] != '\0') {
    g_client.subscribe(g_dhw_state_topic, 0);
  }
}

void refresh_meter_config(const Config& config) {
  static char signature[512];
  snprintf(signature, sizeof(signature), "%s|%s|%u|%s|%s|%s|%s|%s",
           config.mixer_meter_device, config.mixer_meter_field,
           static_cast<unsigned>(config.mixer_meter_scale), config.mixer_meter_topic,
           config.dhw_meter_device, config.dhw_meter_entity, config.dhw_meter_field,
           config.dhw_meter_topic);
  if (strcmp(signature, g_meter_signature) == 0) {
    return;
  }
  copy_string(g_meter_signature, sizeof(g_meter_signature), signature);
  if (g_client.connected()) {
    g_client.disconnect();
    g_status.connected = false;
    g_status.disconnect_count++;
    g_status.last_disconnect_ms = millis();
    set_state("disconnected");
  }
  g_last_connect_attempt_ms = 0;
  subscribe_meter_topics(config);
}

void attempt_connect() {
  if (g_config == nullptr) {
    return;
  }

  g_client.setServer(g_config->mqtt_host, g_config->mqtt_port);

  char will_topic[96];
  make_topic(will_topic, sizeof(will_topic), "status");

  const char* user = g_config->mqtt_user[0] != '\0' ? g_config->mqtt_user : nullptr;
  const char* pass = g_config->mqtt_pass[0] != '\0' ? g_config->mqtt_pass : nullptr;

  Serial.printf("[mqtt] connecting to %s:%u as %s\n", g_config->mqtt_host,
                static_cast<unsigned>(g_config->mqtt_port), g_client_id);

  const bool ok = g_client.connect(g_client_id, user, pass, will_topic, 1, true,
                                   "offline", true);
  if (!ok) {
    g_status.connected = false;
    g_status.errors++;
    g_status.last_error_code = g_client.state();
    set_state("connect_failed");

    const uint32_t now = millis();
    if (g_last_error_log_ms == 0 || now - g_last_error_log_ms >= kErrorLogIntervalMs) {
      g_last_error_log_ms = now;
      Serial.printf("[mqtt] connect failed, state=%d\n", g_status.last_error_code);
      char detail[96];
      snprintf(detail, sizeof(detail), "MQTT connect failed (state %d)",
               g_status.last_error_code);
      web::log_event(1, 1802, detail);
    }
    return;
  }

  g_status.connected = true;
  g_status.connect_count++;
  g_status.last_connect_ms = millis();
  g_status.last_error_code = 0;
  set_state("connected");
  // Keep the log cursors where they are so records produced while the broker
  // was down are published from the RAM ring buffer after reconnection.
  Serial.printf("[mqtt] connected to %s:%u\n", g_config->mqtt_host,
                static_cast<unsigned>(g_config->mqtt_port));
  web::log_event(0, 1800, "MQTT connected");

  char cmd_filter[96];
  make_topic(cmd_filter, sizeof(cmd_filter), "cmd/#");
  if (!g_client.subscribe(cmd_filter, 1)) {
    g_status.errors++;
    Serial.printf("[mqtt] command subscription failed for %s\n", cmd_filter);
    web::log_event(1, 1807, "MQTT command subscription failed");
  }
  subscribe_meter_topics(*g_config);

  publish_availability("online");
  publish_discovery();
  reset_state_cache();
  publish_states();
  g_last_publish_ms = millis();
}

void mark_disconnected(const char* state) {
  if (g_status.connected) {
    g_status.connected = false;
    g_status.disconnect_count++;
    g_status.last_disconnect_ms = millis();
    web::log_event(1, 1801, "MQTT disconnected");
  }
  set_state(state);
}

void refresh_broker_config(const Config& config) {
  char desired_base[sizeof(g_base_topic)];
  copy_base_topic(desired_base, sizeof(desired_base), config.mqtt_base_topic);

  const bool broker_changed = strcmp(g_status.broker, config.mqtt_host) != 0 ||
                              g_status.port != config.mqtt_port;
  const bool base_changed = strcmp(g_base_topic, desired_base) != 0;

  if (broker_changed || base_changed) {
    // Publish the offline marker to the old base topic/broker before moving.
    if (g_client.connected()) {
      publish_availability("offline");
      g_client.disconnect();
      mark_disconnected("disconnected");
    }
  }

  if (broker_changed) {
    copy_string(g_status.broker, sizeof(g_status.broker), config.mqtt_host);
    g_status.port = config.mqtt_port;
  }
  if (base_changed) {
    copy_string(g_base_topic, sizeof(g_base_topic), desired_base);
  }

  if (broker_changed || base_changed) {
    g_last_connect_attempt_ms = 0;
    g_last_publish_ms = 0;
    reset_state_cache();
  }
}

}  // namespace

void begin(const Config& config, SystemState& state) {
  g_config = &config;
  g_state = &state;

  g_status = Status{};
  g_status.enabled = config.mqtt_host[0] != '\0';
  g_status.port = config.mqtt_port;
  copy_string(g_status.broker, sizeof(g_status.broker), config.mqtt_host);
  set_state(g_status.enabled ? "disconnected" : "disabled");
  copy_base_topic(g_base_topic, sizeof(g_base_topic), config.mqtt_base_topic);

  build_identity();
  g_client.setCallback(on_message);
  g_client.setBufferSize(kMqttBufferSize);
  g_client.setKeepAlive(30);
  g_client.setSocketTimeout(2);

  g_last_connect_attempt_ms = 0;
  g_last_publish_ms = 0;
  g_last_error_log_ms = 0;
  g_log_event_cursor = logger::event_seq();
  g_log_slow_cursor = logger::slow_seq();
  g_test_requested = false;
  g_meter_signature[0] = '\0';
  g_mixer_config_topic[0] = '\0';
  g_mixer_state_topic[0] = '\0';
  g_dhw_config_topic[0] = '\0';
  g_dhw_state_topic[0] = '\0';
  reset_state_cache();

  Serial.printf("[mqtt] broker %s:%u, base %s, id %s\n",
                g_status.enabled ? config.mqtt_host : "(disabled)",
                static_cast<unsigned>(config.mqtt_port), g_base_topic, g_client_id);
}

void tick(const Config& config, SystemState& state) {
  g_config = &config;
  g_state = &state;

  g_status.enabled = config.mqtt_host[0] != '\0';
  refresh_broker_config(config);
  refresh_meter_config(config);

  if (!g_status.enabled) {
    if (g_client.connected()) {
      publish_availability("offline");
      g_client.disconnect();
    }
    mark_disconnected("disabled");
    return;
  }

  if (WiFi.status() != WL_CONNECTED) {
    if (g_client.connected()) {
      publish_availability("offline");
      g_client.disconnect();
    }
    mark_disconnected("waiting_wifi");
    return;
  }

  if (!g_client.connected()) {
    mark_disconnected("disconnected");
    const uint32_t now = millis();
    if (g_last_connect_attempt_ms == 0 ||
        now - g_last_connect_attempt_ms >= kReconnectIntervalMs) {
      g_last_connect_attempt_ms = now;
      attempt_connect();
    }
    return;
  }

  g_client.loop();

  if (!g_client.connected()) {
    mark_disconnected("disconnected");
    return;
  }

  if (!g_status.connected) {
    g_status.connected = true;
    set_state("connected");
  }

  const uint32_t now = millis();
  if (g_last_publish_ms == 0 || now - g_last_publish_ms >= kPublishIntervalMs) {
    g_last_publish_ms = now;
    publish_states();
  }

  if (g_test_requested) {
    g_test_requested = false;
    publish_test();
  }

  publish_logs(config);
}

const Status& status() { return g_status; }

void request_test() { g_test_requested = true; }

}  // namespace mqtt
}  // namespace hpn
