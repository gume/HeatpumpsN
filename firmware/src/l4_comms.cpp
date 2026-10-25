#include "hpn/l4_comms.h"

namespace hpn {
namespace l4 {

void begin(const Config& config, SystemState& state) {
  (void)config;
  (void)state;
}

void tick(const Config& config, SystemState& state) {
  // TODO(L4): Wi-Fi, MQTT discovery/state publishing, command validation,
  // HTTP dashboard, and test-mode intake.
  (void)config;
  (void)state;
}

}  // namespace l4
}  // namespace hpn
