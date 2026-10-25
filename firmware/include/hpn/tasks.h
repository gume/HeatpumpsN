#pragma once

#include "hpn/board.h"
#include "hpn/config.h"
#include "hpn/state.h"

namespace hpn {
namespace tasks {

// Create the controller task set. On dual-core targets the real-time tasks are
// pinned to Core 1 and comms/logging to Core 0. On single-core targets all
// tasks run on Core 0; no task is pinned to a non-existent Core 1.
bool start(const Config& config, const BoardConfig& board, SystemState& state);

// True while T_safety, T_io, and T_control have all checked in within their
// deadlines. T_comms and T_logger are deliberately excluded.
bool healthy();

}  // namespace tasks
}  // namespace hpn
