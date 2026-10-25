#include "hpn/l2_control.h"

namespace hpn {
namespace l2 {

void begin(const Config& config, SystemState& state) {
  (void)config;
  (void)state;
}

void tick(const Config& config, SystemState& state) {
#if defined(HPN_BENCH_LOOPBACK)
  // Bench-only pass-through. The real L2 coordination matrix, mixer PI loop
  // and flush state machine are still TODO. This makes the portal's Test Mode
  // commands reach L1 on the emulated carrier; it is never compiled into a
  // hardware build.
  (void)config;
  state.heater_requested = state.heater_intended;
  state.pump_floor_requested = state.pump_floor_intended;
  state.pump_rad1_requested = state.pump_rad1_intended;
  state.pump_rad2_requested = state.pump_rad2_intended;
#else
  // TODO(L2): mixer PI loop, flush state machine, DHW handling, Delta-T
  // collapse pause, and coordination matrix. Writes only *_requested.
  (void)config;
  (void)state;
#endif
}

}  // namespace l2
}  // namespace hpn
