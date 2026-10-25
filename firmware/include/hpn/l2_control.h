#pragma once

#include "hpn/config.h"
#include "hpn/state.h"

namespace hpn {
namespace l2 {

// L2 control and coordination. Idle until the mixer loop and coordination
// rules from plan 04 are implemented.
void begin(const Config& config, SystemState& state);
void tick(const Config& config, SystemState& state);

}  // namespace l2
}  // namespace hpn
