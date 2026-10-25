#pragma once

#include "hpn/config.h"
#include "hpn/state.h"

namespace hpn {
namespace l4 {

// L4 communications: MQTT/HA, web dashboard, command intake. Idle for now.
void begin(const Config& config, SystemState& state);
void tick(const Config& config, SystemState& state);

}  // namespace l4
}  // namespace hpn
