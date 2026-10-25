#include "hpn/l2_control.h"

namespace hpn {
namespace l2 {

void begin(const Config& config, SystemState& state) {
  (void)config;
  (void)state;
}

void tick(const Config& config, SystemState& state) {
  // TODO(L2): mixer PI loop, flush state machine, DHW handling, Delta-T
  // collapse pause, and coordination matrix. Writes only *_requested.
  (void)config;
  (void)state;
}

}  // namespace l2
}  // namespace hpn
