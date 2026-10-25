#include "hpn/l1_safety.h"

namespace hpn {
namespace l1 {

void begin(const Config& config, SystemState& state) {
  (void)config;
  (void)state;
}

void tick(const Config& config, SystemState& state) {
  // TODO(L1): evaluate over-temperature, gradient, sensor postures,
  // demand/after-heating timeouts, and drive l0::set_relay/set_mixer_motor.
  (void)config;
  (void)state;
}

}  // namespace l1
}  // namespace hpn
