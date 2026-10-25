#pragma once

#include <stdint.h>

#include "hpn/config.h"
#include "hpn/state.h"

namespace hpn {
namespace web {

// HTTP/WebSocket portal. begin() mounts LittleFS, registers the REST/WS
// handlers and starts the server. tick() is called from T_comms and publishes
// a snapshot to connected WebSocket clients.
void begin(const Config& config, SystemState& state);
void tick(const Config& config, SystemState& state);

// Append an entry to the in-RAM event log and the serial log. Severity:
// 0 = info, 1 = warn, 2 = error, 3 = safety (latched in the UI).
void log_event(uint8_t severity, uint16_t code, const char* detail);

}  // namespace web
}  // namespace hpn
