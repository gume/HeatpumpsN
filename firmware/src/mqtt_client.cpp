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
constexpr size_t kMqttBufferSize = 2048;
constexpr float kFloatPublishEpsilon = 0.05f;

WiFiClient g_wifi_client;
PubSubClient g_client(g_wifi_client);

const Config* g_config = nullptr;
SystemState* g_state = nullptr;

Status g_status{};
char g_base_topic[48] = "heatpumpsn/controller";
char g_mac_hex[16] = "000000000000";
char g_device_id[32] = "heatpumpsn_000000000000";
char g_node_id[32] = "hpn_000000000000";
char g_client_id[32] = "hpn-000000000000";

uint32_t g_last_connect_attempt_ms = 0;
uint32_t g_last_publish_ms = 0;
uint32_t g_last_error_log_ms = 0;
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

void publish_text(const char* suffix, const char* payload, char* last, size_t last_size) {
  if (last[0] != '\0' && strcmp(last, payload) == 0) {
    return;
  }
  char topic[96];
  make_topic(topic, sizeof(topic), suffix);
  if (g_client.publish(topic, payload, true)) {
    copy_string(last, last_size, payload);
  }
}

void format_temperature(const SensorReading& reading, char* out, size_t out_size) {
  if (reading.is_valid && isfinite(reading.value_c)) {
    snprintf(out, out_size, "%.2f", reading.value_c);
  } else {
    copy_string(out, out_size, "unknown");
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
  const bool mixer_motion = st.mix_pulse_ms_phys > 0;
  const bool mixer_open = st.mix_pulse_dir_phys > 0;
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

    if (strcmp(suffix, "cmd/heater") == 0) {
      g_state->heater_intended = on;
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

void on_message(char* topic, uint8_t* payload, unsigned int length) {
  if (topic == nullptr || payload == nullptr) {
    return;
  }

  char value[48];
  const size_t copy_len = length < sizeof(value) - 1 ? length : sizeof(value) - 1;
  memcpy(value, payload, copy_len);
  value[copy_len] = '\0';

  char* start = value;
  while (*start == ' ' || *start == '\t' || *start == '\r' || *start == '\n') {
    ++start;
  }
  size_t len = strlen(start);
  while (len > 0 && (start[len - 1] == ' ' || start[len - 1] == '\t' ||
                     start[len - 1] == '\r' || start[len - 1] == '\n')) {
    start[--len] = '\0';
  }

  handle_command(topic, start);
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
  g_test_requested = false;
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
}

const Status& status() { return g_status; }

void request_test() { g_test_requested = true; }

}  // namespace mqtt
}  // namespace hpn
