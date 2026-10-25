#pragma once

#include "hpn/config.h"
#include "hpn/state.h"

namespace hpn {
namespace l4 {

// L4 communications: Wi-Fi, MQTT/Home Assistant, web dashboard, command intake.
// MQTT commands are written to the *_intended fields; L2 owns the transition to
// *_requested and L1 owns the transition to physical outputs.
void begin(const Config& config, SystemState& state);
void tick(const Config& config, SystemState& state);

}  // namespace l4
}  // namespace hpn
