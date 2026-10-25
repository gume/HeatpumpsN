#pragma once

#include "hpn/config.h"
#include "hpn/state.h"

namespace hpn {
namespace l1 {

// L1 safety arbitration. Idle until the rules from plan 03 are implemented.
void begin(const Config& config, SystemState& state);
void tick(const Config& config, SystemState& state);

}  // namespace l1
}  // namespace hpn
