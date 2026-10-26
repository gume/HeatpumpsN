#include "hpn/logger.h"

#include <Arduino.h>
#include <stdio.h>
#include <string.h>

namespace hpn {
namespace logger {
namespace {

portMUX_TYPE g_event_mux = portMUX_INITIALIZER_UNLOCKED;
Event g_events[kEventCapacity] = {};
uint32_t g_event_head = 0;
uint32_t g_event_count = 0;
uint32_t g_event_seq = 0;

portMUX_TYPE g_slow_mux = portMUX_INITIALIZER_UNLOCKED;
SlowSample g_slow[kSlowCapacity] = {};
uint32_t g_slow_head = 0;
uint32_t g_slow_count = 0;
uint32_t g_slow_seq = 0;

uint32_t g_last_slow_ms = 0;
uint16_t g_last_error_code = ERR_OK;
uint32_t g_last_alarms = 0;

uint8_t severity_for_state(const SystemState& state) {
  const uint32_t critical =
      ALARM_OVERTEMP_HARD | ALARM_SENSOR_FAULT_MIX | ALARM_WELDED_RELAY;
  if ((state.active_alarms & critical) != 0) {
    return 3;
  }
  if (state.system_error_code != ERR_OK) {
    return 2;
  }
  if (state.active_alarms != 0) {
    return 1;
  }
  return 0;
}

void sample_slow(const SystemState& state, uint32_t now_ms) {
  SlowSample sample{};
  sample.ms = now_ms;
  sample.boiler = state.T_boiler.value_c;
  sample.mix = state.T_mix_out.value_c;
  sample.ret = state.T_return_floor.value_c;
  sample.pos = state.pos_est_dr;
  sample.relays[0] = state.heater_phys;
  sample.relays[1] = state.pump_rad1_phys;
  sample.relays[2] = state.pump_rad2_phys;
  sample.relays[3] = state.pump_floor_phys;

  taskENTER_CRITICAL(&g_slow_mux);
  sample.seq = ++g_slow_seq;
  g_slow[g_slow_head] = sample;
  g_slow_head = (g_slow_head + 1) % kSlowCapacity;
  if (g_slow_count < kSlowCapacity) {
    g_slow_count++;
  }
  taskEXIT_CRITICAL(&g_slow_mux);
}

void publish_diagnostic_event(const SystemState& state) {
  if (state.system_error_code == g_last_error_code &&
      state.active_alarms == g_last_alarms) {
    return;
  }

  char detail[160];
  format_error_text(state, detail, sizeof(detail));
  log_event(severity_for_state(state), state.system_error_code, detail);

  g_last_error_code = state.system_error_code;
  g_last_alarms = state.active_alarms;
}

}  // namespace

void begin(const Config& config, SystemState& state) {
  (void)config;

  taskENTER_CRITICAL(&g_event_mux);
  memset(g_events, 0, sizeof(g_events));
  g_event_head = 0;
  g_event_count = 0;
  g_event_seq = 0;
  taskEXIT_CRITICAL(&g_event_mux);

  taskENTER_CRITICAL(&g_slow_mux);
  memset(g_slow, 0, sizeof(g_slow));
  g_slow_head = 0;
  g_slow_count = 0;
  g_slow_seq = 0;
  taskEXIT_CRITICAL(&g_slow_mux);

  g_last_slow_ms = millis();
  g_last_error_code = ERR_OK;
  g_last_alarms = 0;

  // A configuration CRC failure is latched in SystemState before the logger
  // starts. Seed the comparison with ERR_OK so the first tick logs it.
  (void)state;
}

void tick(const Config& config, SystemState& state) {
  (void)config;

  publish_diagnostic_event(state);

  const uint32_t now = millis();
  if (now - g_last_slow_ms >= kSlowSampleIntervalMs) {
    g_last_slow_ms = now;
    sample_slow(state, now);
  }
}

void log_event(uint8_t severity, uint16_t code, const char* detail) {
  Event entry{};
  entry.ms = millis();
  entry.severity = severity > 3 ? 3 : severity;
  entry.code = code;
  entry.latched = entry.severity >= 3;
  snprintf(entry.detail, sizeof(entry.detail), "%s", detail != nullptr ? detail : "");

  taskENTER_CRITICAL(&g_event_mux);
  entry.seq = ++g_event_seq;
  g_events[g_event_head] = entry;
  g_event_head = (g_event_head + 1) % kEventCapacity;
  if (g_event_count < kEventCapacity) {
    g_event_count++;
  }
  taskEXIT_CRITICAL(&g_event_mux);

  Serial.printf("[event] %s %u %s\n", severity_name(entry.severity),
                static_cast<unsigned>(code), entry.detail);
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

uint32_t event_seq() {
  uint32_t seq = 0;
  taskENTER_CRITICAL(&g_event_mux);
  seq = g_event_seq;
  taskEXIT_CRITICAL(&g_event_mux);
  return seq;
}

size_t events_after(uint32_t after, Event* out, size_t max) {
  if (out == nullptr || max == 0) {
    return 0;
  }

  size_t copied = 0;
  taskENTER_CRITICAL(&g_event_mux);
  const uint32_t start = (g_event_head + kEventCapacity - g_event_count) % kEventCapacity;
  for (uint32_t i = 0; i < g_event_count && copied < max; ++i) {
    const Event& entry = g_events[(start + i) % kEventCapacity];
    if (entry.seq > after) {
      out[copied++] = entry;
    }
  }
  taskEXIT_CRITICAL(&g_event_mux);
  return copied;
}

uint32_t slow_seq() {
  uint32_t seq = 0;
  taskENTER_CRITICAL(&g_slow_mux);
  seq = g_slow_seq;
  taskEXIT_CRITICAL(&g_slow_mux);
  return seq;
}

size_t slow_after(uint32_t after, SlowSample* out, size_t max) {
  if (out == nullptr || max == 0) {
    return 0;
  }

  size_t copied = 0;
  taskENTER_CRITICAL(&g_slow_mux);
  const uint32_t start = (g_slow_head + kSlowCapacity - g_slow_count) % kSlowCapacity;
  for (uint32_t i = 0; i < g_slow_count && copied < max; ++i) {
    const SlowSample& sample = g_slow[(start + i) % kSlowCapacity];
    if (sample.seq > after) {
      out[copied++] = sample;
    }
  }
  taskEXIT_CRITICAL(&g_slow_mux);
  return copied;
}

}  // namespace logger
}  // namespace hpn
