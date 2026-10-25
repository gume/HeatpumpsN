#pragma once

#include "hpn/config.h"
#include "hpn/state.h"

namespace hpn {
namespace l1 {

// L1 safety arbitration. The mixer pulse executor and dead-reckoned position
// estimate are live; the full safety rules from plan 03 are still TODO.
void begin(const Config& config, SystemState& state);
void tick(const Config& config, SystemState& state);

}  // namespace l1
}  // namespace hpn
