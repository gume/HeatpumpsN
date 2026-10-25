#pragma once

#include <stdint.h>

#include "hpn/config.h"

// Minimal station-mode Wi-Fi client.
//
// This is deliberately a *client* only. Provisioning (captive portal, AP
// fallback, NVS-backed credentials) belongs to the future Wi-Fi manager; for
// now the credentials come from Config, which already carries the
// provisioning fields from plan 01 §1.
namespace hpn {
namespace wifi {

struct Status {
  bool configured;         // an SSID was supplied
  bool connected;          // currently associated and holding an IP
  int8_t rssi;             // dBm, valid while connected
  uint32_t connect_count;  // successful associations since boot
  uint32_t last_connect_ms;
};

// Start the station. Non-blocking: association completes in tick().
void begin(const Config& config);

// Called from T_comms. Watches the link and retries with a fixed backoff.
void tick(const Config& config);

const Status& status();

// Human-readable link state for logs and the future portal.
const char* state_name();

}  // namespace wifi
}  // namespace hpn
