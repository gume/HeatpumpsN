#pragma once

#include "hpn/config.h"
#include "hpn/state.h"

namespace hpn {
namespace logger {

// Slow/event logging and NVS persistence. Idle for now.
void begin(const Config& config, SystemState& state);
void tick(const Config& config, SystemState& state);

}  // namespace logger
}  // namespace hpn
