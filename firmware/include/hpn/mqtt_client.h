#pragma once

#include <stdint.h>

#include "hpn/config.h"
#include "hpn/state.h"

// MQTT / Home Assistant integration.
//
// The transport is knolleary/PubSubClient (already used by the project) and
// the discovery payloads follow Home Assistant's MQTT discovery schema:
// https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery
//
// The broker is enabled by setting a non-empty mqtt_host in the portal. The
// controller publishes retained discovery/config, availability and entity
// state; HA publishes commands to <base>/cmd/....
namespace hpn {
namespace mqtt {

struct Status {
  bool enabled;
  bool connected;
  char state[20];
  char broker[64];
  uint16_t port;
  uint32_t connect_count;
  uint32_t disconnect_count;
  uint32_t last_connect_ms;
  uint32_t last_disconnect_ms;
  uint32_t errors;
  int last_error_code;
};

void begin(const Config& config, SystemState& state);

// Called from T_comms. Non-blocking apart from the bounded PubSubClient
// connect attempt (socket timeout is capped at 2 s).
void tick(const Config& config, SystemState& state);

const Status& status();

// Queue a test publication. The next MQTT tick publishes it (or it is sent as
// soon as the broker becomes reachable).
void request_test();

}  // namespace mqtt
}  // namespace hpn
