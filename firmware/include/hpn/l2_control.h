#pragma once

#include "hpn/config.h"
#include "hpn/state.h"

namespace hpn {
namespace l2 {

// L2 control and coordination. The proportional mixer position loop is live;
// the full coordination matrix and flush state machine from plan 04 are TODO.
void begin(const Config& config, SystemState& state);
void tick(const Config& config, SystemState& state);

}  // namespace l2
}  // namespace hpn
