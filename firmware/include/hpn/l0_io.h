#pragma once

#include "hpn/config.h"

// L0: the only layer allowed to touch hardware pins (plan 02 §2.1).
//
// This scaffold implements boot-safe output initialization only. 1-Wire
// polling, mixer pulse scheduling, encoder input, and pump-power supervision
// will be added here.
namespace hpn {
namespace l0 {

void begin(const Config& config);

// Force every assigned actuator to its defined-safe state.
void apply_safe_state();

// Called from the IO task/loop. Currently a placeholder.
void tick();

}  // namespace l0
}  // namespace hpn
