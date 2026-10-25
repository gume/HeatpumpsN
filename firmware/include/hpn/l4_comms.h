#pragma once

#include "hpn/config.h"
#include "hpn/state.h"

namespace hpn {
namespace l4 {

// L4 communications: Wi-Fi, MQTT/HA, web dashboard, command intake.
// The station-mode Wi-Fi client is implemented; the rest is still idle.
void begin(const Config& config, SystemState& state);
void tick(const Config& config, SystemState& state);

}  // namespace l4
}  // namespace hpn
