#pragma once

#include <stddef.h>
#include <stdint.h>

#include "hpn/config.h"
#include "hpn/state.h"

// Telemetry logging.
//
// IMPORTANT: log records are never persisted to flash. Both streams live in
// fixed-size circular buffers in RAM and are lost on reboot. They are exposed
// to the web portal and, when enabled in Config, streamed to MQTT.
namespace hpn {
namespace logger {

constexpr size_t kEventCapacity = 96;
constexpr size_t kSlowCapacity = 180;
constexpr size_t kEventDetailSize = 96;
constexpr uint32_t kSlowSampleIntervalMs = 10000;

// Severity values match the portal: 0 info, 1 warn, 2 error, 3 safety.
struct Event {
  uint32_t seq;
  uint32_t ms;
  uint8_t severity;
  uint16_t code;
  bool latched;
  char detail[kEventDetailSize];
};

struct SlowSample {
  uint32_t seq;
  uint32_t ms;
  float boiler;
  float mix;
  float ret;
  float pos;
  bool relays[4];
};

void begin(const Config& config, SystemState& state);

// Called from T_logger with a published state snapshot (T_logger runs on the
// protocol core and must not read the live struct). Samples the slow log and
// emits diagnostic events when the error code or active warning/alarm mask
// changes.
void tick(const Config& config, const SystemState& state);

// Append an entry to the in-RAM event log and the serial console.
void log_event(uint8_t severity, uint16_t code, const char* detail);

const char* severity_name(uint8_t severity);

// Copy up to `max` records with seq > after into `out` (oldest first). Returns
// the number copied. A caller drains a backlog by feeding the last copied seq
// back in. These helpers copy under a short critical section; they never call
// back into application code while the lock is held.
uint32_t event_seq();
size_t events_after(uint32_t after, Event* out, size_t max);

uint32_t slow_seq();
size_t slow_after(uint32_t after, SlowSample* out, size_t max);

}  // namespace logger
}  // namespace hpn
