#pragma once

#include <ArduinoJson.h>
#include <stdint.h>

#include "hpn/config.h"

// Station-mode Wi-Fi client with a fallback access point.
//
// The credentials come from Config. If the station has not connected 2 minutes
// after boot, or its link has been down for 5 minutes (or no SSID is set), the
// controller also opens an access point (Config ap_ssid / ap_password, portal
// at 192.168.4.1) while it keeps retrying the station. The AP closes once the
// station is connected and no client has used the AP for a minute.
namespace hpn {
namespace wifi {

struct Status {
  bool configured;         // an SSID was supplied
  bool connected;          // currently associated and holding an IP
  int8_t rssi;             // dBm, valid while connected
  uint32_t connect_count;  // successful associations since boot
  uint32_t last_connect_ms;
  bool ap_active;          // fallback access point running
  uint8_t ap_clients;      // stations connected to it
};

// Start the station. Non-blocking: association completes in tick().
void begin(const Config& config);

// Called from T_comms. Watches the link and retries with a fixed backoff.
void tick(const Config& config);

const Status& status();

// Human-readable link state for logs and the future portal.
const char* state_name();

// On-demand network scan for the portal (any task). The scan itself runs in
// tick(). A connected station stays connected and AP clients stay associated;
// a failing association attempt is paused for the scan.
void request_scan();
// {"running","ageS","error","networks":[{"ssid","rssi","channel","secure"}]},
// strongest first, one entry per SSID.
void scan_json(JsonObject out);

}  // namespace wifi
}  // namespace hpn
